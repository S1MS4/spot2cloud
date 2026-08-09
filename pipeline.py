"""Concurrent orchestration of a playlist download.

:func:`run_job` takes one :class:`~models.PlaylistJob`, works out its folder,
consults the resume ledger, and fans the remaining tracks out over a
:class:`~concurrent.futures.ThreadPoolExecutor`.

Three invariants this module exists to hold:

1. **No shared yt-dlp state.** Every worker task builds its *own* options dict
   via ``downloader.build_ydl_opts``. yt-dlp's ``YoutubeDL`` instances and the
   opts dict they are constructed from are not safe to share across threads, so
   a single dict built once and reused would be a data race.
2. **Deterministic report order.** Futures complete in whatever order the
   network allows; results are mapped back onto their original track index and
   returned in playlist order, so the summary never looks shuffled.
3. **No lost work.** A worker exception becomes a ``STATUS_FAILED`` result
   rather than propagating, and the history ledger is flushed in a ``finally``,
   including on Ctrl-C.

``downloader`` is imported lazily inside the functions so that importing this
module stays cheap and does not hard-fail when yt-dlp is absent.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, List, Optional, Sequence

from history import HistoryStore
from models import (
    BITRATE_DEFAULT,
    MAX_WORKERS_DEFAULT,
    MAX_WORKERS_LIMIT,
    STATUS_FAILED,
    DownloadResult,
    PlaylistJob,
    Track,
)

__all__ = [
    "FALLBACK_PLAYLIST_NAME",
    "ProgressCallback",
    "clamp_workers",
    "run_job",
]

logger = logging.getLogger(__name__)

#: Used when a job arrives with a blank name, so the folder is never "".
FALLBACK_PLAYLIST_NAME = "Unknown Playlist"

#: ``callback(result, done, total)``, called from the main thread.
ProgressCallback = Callable[[DownloadResult, int, int], None]


def clamp_workers(workers: int) -> int:
    """Clamp a worker count into ``1..MAX_WORKERS_LIMIT``.

    Args:
        workers: Requested thread count; may be nonsense (0, negative, huge, or
            a non-integer that happens to be int-able).

    Returns:
        A usable thread count between 1 and :data:`models.MAX_WORKERS_LIMIT`.
    """
    try:
        requested = int(workers)
    except (TypeError, ValueError):
        logger.warning(
            "pipeline: worker count %r is not a number; using %d",
            workers,
            MAX_WORKERS_DEFAULT,
        )
        return MAX_WORKERS_DEFAULT
    clamped = max(1, min(MAX_WORKERS_LIMIT, requested))
    if clamped != requested:
        logger.warning(
            "pipeline: clamping workers %d -> %d (allowed range 1..%d)",
            requested,
            clamped,
            MAX_WORKERS_LIMIT,
        )
    return clamped


def _emit(
    on_progress: Optional[ProgressCallback],
    result: DownloadResult,
    done: int,
    total: int,
) -> None:
    """Invoke the progress callback, swallowing anything it throws.

    Always called from the main thread, so the callback needs no locking of its
    own. A broken callback must not take the whole run down with it.

    Args:
        on_progress: Callback, or ``None`` to do nothing.
        result: The outcome that just landed.
        done: How many tracks have been accounted for, including this one.
        total: Total tracks in the job.

    Returns:
        None.
    """
    if on_progress is None:
        return
    try:
        on_progress(result, done, total)
    except Exception:  # noqa: BLE001 - a bad UI callback is never fatal
        logger.warning("pipeline: progress callback raised; ignoring", exc_info=True)


def _as_results(outcome, track: Track) -> List[DownloadResult]:
    """Normalise whatever a worker returned into a non-empty list of results.

    Workers return a list because a chaptered video yields one result per
    chapter; anything else is a bug in the downloader, reported as a failure
    rather than crashing the harvest loop.
    """
    candidates = outcome if isinstance(outcome, list) else [outcome]
    items = [item for item in candidates if isinstance(item, DownloadResult)]
    if items:
        return items
    return [
        DownloadResult.failed(
            track.query,
            "downloader returned %r instead of a DownloadResult" % (outcome,),
            title=track.title,
        )
    ]


def _flatten(results: Sequence[Optional[List[DownloadResult]]]) -> List[DownloadResult]:
    """Track-ordered results, with the tracks that never ran left out."""
    return [item for group in results if group for item in group]


def _record(history: HistoryStore, track: Track, items: Sequence[DownloadResult]) -> None:
    """Add the successful results of one track to the resume ledger.

    A split track is recorded per chapter file instead of under the track's own
    key: the pieces are what ended up on disk, so that is what a later run has
    to be able to recognise.
    """
    split = len(items) > 1
    for item in items:
        if not item.succeeded or item.path is None:
            continue
        path = Path(item.path)
        history.add("chapter:%s" % path.name if split else track.history_key(), path, item.title)


def run_job(
    job: PlaylistJob,
    output_dir,
    quality: str = BITRATE_DEFAULT,
    workers: int = MAX_WORKERS_DEFAULT,
    overwrite: bool = False,
    verbose: bool = False,
    split_chapters: bool = False,
    audio_format: str = "mp3",
    on_progress: Optional[ProgressCallback] = None,
) -> List[DownloadResult]:
    """Download every track of ``job`` concurrently into its own folder.

    The job's folder is ``<output_dir>/<sanitised job name>/``. Tracks already
    recorded in that folder's ledger *and* still present on disk are skipped
    (unless ``overwrite``); a recorded track whose file has since been deleted
    has its stale entry dropped and is downloaded again.

    Args:
        job: Playlist to process. ``job.tracks`` defines the result order.
        output_dir: Root download directory; the playlist folder is created
            beneath it.
        quality: MP3 bitrate in kbps, passed through to ``build_ydl_opts``.
        workers: Desired thread count, clamped to ``1..MAX_WORKERS_LIMIT``.
        overwrite: Ignore the ledger and re-download everything.
        verbose: Let yt-dlp log verbosely.
        split_chapters: Split a track whose video carries chapter markers into
            one MP3 per chapter, so a full-album stream becomes an album.
        on_progress: Optional ``callback(result, done, total)`` invoked from the
            main thread once per *result* (skips included). Exceptions from it
            are logged and ignored.

    Returns:
        One :class:`~models.DownloadResult` per track -- per chapter for a track
        that got split -- in the job's original track order. On
        ``KeyboardInterrupt`` only the tracks that finished are returned, still
        in order.

    Raises:
        Nothing for per-track problems: a worker raising anything becomes a
        ``STATUS_FAILED`` result. ``KeyboardInterrupt`` is caught, the pool is
        cancelled and the partial results are returned.
    """
    from album_art import save_folder_cover
    from downloader import (
        build_ydl_opts,
        download_chapters,
        download_one,
        ensure_output_dir,
        probe_chapters,
        sanitize_filename,
    )

    raw_name = (job.name or "").strip()
    if not raw_name:
        logger.warning("pipeline: job has no name; using %r", FALLBACK_PLAYLIST_NAME)
        raw_name = FALLBACK_PLAYLIST_NAME
    folder_name = sanitize_filename(raw_name) or FALLBACK_PLAYLIST_NAME
    folder = Path(ensure_output_dir(Path(output_dir) / folder_name))

    tracks: List[Track] = list(job.tracks)
    total = len(tracks)
    logger.info("pipeline: '%s' -> %s (%d track(s))", raw_name, folder, total)

    # One cover.jpg per album folder. Embedded tags are for players; this file
    # is what file browsers and the SoundCloud uploader can point at.
    if tracks:
        save_folder_cover(folder, cover_url=tracks[0].cover_url, overwrite=overwrite)

    # One slot per track, each holding that track's results: a split track
    # contributes several, and flattening at the end keeps them in track order.
    results: List[Optional[List[DownloadResult]]] = [None] * total
    if total == 0:
        return []

    workers = clamp_workers(workers)
    history = HistoryStore(folder)
    done = 0

    def _task(track: Track) -> List[DownloadResult]:
        """Run one download on a worker thread and return its result(s).

        Builds a private opts dict per call. yt-dlp state is not thread-safe,
        so this must never be hoisted out of the worker.

        Search-query tracks (Spotify-sourced) are matched to a concrete video
        first, so the duration and live/cover guards in ``pick_best_match``
        decide which upload gets downloaded rather than blindly taking the top
        hit.

        Under ``split_chapters`` a video that carries chapter markers becomes
        one result per chapter; a video without them takes the normal path, so
        the flag is safe to leave on for a mixed playlist.
        """
        from playlist_parser import match_track

        resolved, reason = match_track(track)
        if resolved is None:
            return [DownloadResult.failed(track.query, reason or "no match", title=track.title)]

        opts = build_ydl_opts(folder, quality=quality, verbose=verbose, audio_format=audio_format)
        if split_chapters and probe_chapters(resolved.query, opts):
            return download_chapters(resolved, folder, opts, overwrite=overwrite)
        return [download_one(resolved, folder, opts, overwrite=overwrite)]

    try:
        # --- resume pass (main thread, cheap, keeps ledger reads serial) ---
        pending: List[int] = []
        for index, track in enumerate(tracks):
            key = track.history_key()
            if overwrite:
                pending.append(index)
                continue
            entry = history.get(key)
            if entry is None:
                pending.append(index)
                continue
            recorded = history.resolve_path(key)
            if recorded is not None and recorded.exists():
                done += 1
                result = DownloadResult.skipped(
                    track.query,
                    title=entry.get("title") or track.title,
                    path=recorded,
                )
                results[index] = [result]
                logger.debug("pipeline: skipping %s (already downloaded)", track.display())
                _emit(on_progress, result, done, total)
                continue
            logger.info(
                "pipeline: %s was in history but its file is gone; re-downloading",
                track.display(),
            )
            history.remove(key)
            pending.append(index)

        if not pending:
            logger.info("pipeline: '%s' fully up to date, nothing to fetch", raw_name)
            return _flatten(results)

        # --- concurrent pass ------------------------------------------------
        executor = ThreadPoolExecutor(max_workers=workers)
        future_to_index = {}
        try:
            for index in pending:
                future_to_index[executor.submit(_task, tracks[index])] = index

            for future in as_completed(future_to_index):
                index = future_to_index[future]
                track = tracks[index]
                try:
                    outcome = future.result()
                except Exception as exc:  # noqa: BLE001 - defence in depth
                    logger.error(
                        "pipeline: worker blew up on %s: %s", track.display(), exc,
                        exc_info=True,
                    )
                    outcome = DownloadResult.failed(
                        track.query,
                        "unexpected worker error: %s" % exc,
                        title=track.title,
                    )
                items = _as_results(outcome, track)
                results[index] = items
                _record(history, track, items)
                # One callback per result, so a ten-chapter video reports ten.
                for item in items:
                    done += 1
                    _emit(on_progress, item, done, total)
        except KeyboardInterrupt:
            # Python 3.9's shutdown(wait=False) does NOT cancel queued futures,
            # so cancel them by hand, then leave without joining the pool.
            cancelled = sum(1 for future in future_to_index if future.cancel())
            # Salvage: a worker may have finished a download in the instant
            # before the interrupt landed, without the main loop harvesting it
            # yet. Those MP3s are already on disk, so record them - otherwise
            # the next run re-downloads a file that is sitting right there.
            salvaged = 0
            for future, index in future_to_index.items():
                if results[index] is not None or future.cancelled() or not future.done():
                    continue
                try:
                    outcome = future.result()
                except Exception:  # noqa: BLE001 - a dying run swallows failures
                    continue
                candidates = outcome if isinstance(outcome, list) else [outcome]
                items = [item for item in candidates if isinstance(item, DownloadResult)]
                if not items:
                    continue
                results[index] = items
                salvaged += 1
                _record(history, tracks[index], items)
            logger.warning(
                "pipeline: interrupted; cancelled %d queued track(s), salvaged %d "
                "already-finished one(s), returning %d completed result(s)",
                cancelled,
                salvaged,
                len(_flatten(results)),
            )
            executor.shutdown(wait=False)
            return _flatten(results)
        finally:
            executor.shutdown(wait=False)

        collected = _flatten(results)
        failures = sum(1 for item in collected if item.status == STATUS_FAILED)
        if failures:
            logger.warning("pipeline: '%s' finished with %d failure(s)", raw_name, failures)
        return collected
    finally:
        history.flush()
