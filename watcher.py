"""Watch mode: turn a plain-text list of playlists into a download queue.

The user keeps a file (``playlists.txt`` by default) open in an editor and
pastes playlist references into it, one per line. This module polls that file
and downloads anything it has not seen before.

Why polling instead of ``watchdog``
-----------------------------------
Filesystem-notification libraries are a third-party dependency and, on macOS,
report the *editor's* atomic-rename dance rather than a single tidy "file
changed" event. A five-second ``stat()`` is free by comparison: the file is
re-read only when ``st_mtime`` **or** ``st_size`` differs from the last look,
so the steady state costs one syscall per tick. That keeps the whole project
stdlib-only.

File format
-----------
One entry per line. Blank lines and lines whose first non-space character is
``#`` are ignored, so the file doubles as a scratchpad::

    # weekly
    spotify:playlist:EXAMPLEPLAYLISTID00000
    https://www.youtube.com/playlist?list=PLxxxxxxxx

    lo-fi beats to relax to

Everything else is passed verbatim to :func:`playlist_parser.resolve_input`,
so Spotify URIs/URLs, YouTube URLs and bare playlist names all work.

The processed sidecar
---------------------
Entries already handled are recorded in a JSON sidecar named
``.<watch file>.processed`` next to the watch file (e.g.
``.playlists.txt.processed``). It is keyed on the **raw stripped line text**,
which is what the user actually controls: re-pasting the same URL is a no-op,
while a genuinely different URL for the same playlist is a new entry. Without
it, restarting the watcher would re-walk every line in the file and re-check
hundreds of tracks against the network.

Failure policy: permanent vs. retryable
---------------------------------------
One bad line must never kill the loop, but "mark everything as done" and
"retry everything forever" are both wrong, so failures are split in two by
:func:`is_retryable`:

**Permanent** (recorded as ``failed``, never retried)
    Problems with the *entry itself* -- a malformed Spotify reference
    (``ValueError``), a name that matches no playlist, a bug in the resolver.
    Re-running these produces the same failure and only burns API quota, so
    they are written to the sidecar and skipped from then on. Fixing them means
    editing the line, which changes the key and makes it new again.

**Retryable** (not recorded, retried on the next pass)
    Problems with the *environment* -- :class:`SpotifyCredentialsError`
    (the user has not exported their API keys yet) and network faults
    (DNS, connection refused/reset, timeouts, HTTP 429/5xx). The user fixes
    these outside the file, and would be rightly annoyed if exporting the
    credentials still left the playlist stuck. Because a retryable failure
    leaves nothing on disk, the cached stat signature is invalidated so the
    next poll re-reads the file even though it has not changed.

Nothing in here creates the watch file: a missing file is logged once and then
waited on, because auto-creating it would hide a typo in ``--watch-file``.

Logging goes to this module's logger only; configuring handlers is the CLI's
job (never call ``basicConfig`` from a library module).
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import socket
import time
import urllib.error
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

from models import (
    BITRATE_DEFAULT,
    DEFAULT_OUTPUT_DIR,
    MAX_WORKERS_DEFAULT,
    STATUS_FAILED,
    STATUS_OK,
    WATCH_FILE_NAME,
)
from playlist_parser import SpotifyCredentialsError

__all__ = [
    "SIDECAR_PREFIX",
    "SIDECAR_SUFFIX",
    "SIDECAR_VERSION",
    "ENTRY_DONE",
    "ENTRY_FAILED",
    "MIN_INTERVAL",
    "COMMENT_PREFIX",
    "RETRYABLE_EXCEPTIONS",
    "RETRYABLE_MESSAGE_MARKERS",
    "PassResult",
    "sidecar_path",
    "parse_entries",
    "load_processed",
    "save_processed",
    "is_retryable",
    "run_once",
    "watch_loop",
]

logger = logging.getLogger(__name__)

#: The sidecar is ``.<watch file name>.processed``: a dotfile so it stays out
#: of the way, next to the watch file so moving the file moves its state.
SIDECAR_PREFIX = "."
SIDECAR_SUFFIX = ".processed"

#: Bumped if the record shape ever changes; unknown versions are read
#: best-effort rather than discarded.
SIDECAR_VERSION = 1

#: Sidecar record statuses (distinct from :mod:`models` track statuses).
ENTRY_DONE = "done"
ENTRY_FAILED = "failed"

#: Floor on the poll interval. A zero/negative interval would spin the CPU.
MIN_INTERVAL = 0.1

#: Comment marker for the watch file.
COMMENT_PREFIX = "#"


class PassResult(NamedTuple):
    """Outcome of a single sweep of the watch file.

    Attributes:
        missing: The watch file did not exist (nothing else is meaningful).
        entries: Count of usable (non-blank, non-comment) lines seen.
        attempted: How many of those were new and therefore processed.
        ok: Entries that resolved and downloaded without a job-level error.
        failed: Entries that failed permanently and were marked processed.
        retryable: Entries that failed transiently and were left unmarked.
    """

    missing: bool = False
    entries: int = 0
    attempted: int = 0
    ok: int = 0
    failed: int = 0
    retryable: int = 0


# --- sidecar --------------------------------------------------------------


def sidecar_path(watch_file) -> Path:
    """Return the path of the processed-entries sidecar for ``watch_file``.

    Args:
        watch_file: Path to the watch file (``~`` is expanded).

    Returns:
        ``<dir>/.<name>.processed`` as an absolute-ish :class:`~pathlib.Path`
        alongside the watch file, so the state travels with it.
    """
    path = Path(watch_file).expanduser()
    return path.parent / ("%s%s%s" % (SIDECAR_PREFIX, path.name, SIDECAR_SUFFIX))


def load_processed(path) -> Dict[str, Dict[str, Any]]:
    """Read the sidecar into a ``{entry line: record}`` mapping.

    A missing sidecar is simply an empty history. A corrupt or truncated one
    (interrupted write, hand-editing) is logged and treated as empty rather
    than raising: losing the resume state costs a re-check, whereas crashing
    the watcher on startup costs the user their download queue. The bad file is
    left on disk untouched until the next successful save overwrites it, so it
    can still be inspected.

    Args:
        path: Sidecar path.

    Returns:
        Mapping of raw entry text to its record dict. Always a fresh dict.
    """
    file_path = Path(path)
    try:
        with file_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return {}
    except (ValueError, OSError, UnicodeDecodeError) as exc:
        logger.warning(
            "watcher: ignoring unreadable state file %s (%s); "
            "already-downloaded playlists may be re-checked",
            file_path, exc,
        )
        return {}

    raw_entries: Any = None
    if isinstance(payload, dict):
        raw_entries = payload.get("entries", payload)
    elif isinstance(payload, list):
        # Tolerate a bare list of processed lines from a hand-written file.
        raw_entries = {str(item): {"status": ENTRY_DONE} for item in payload}

    if not isinstance(raw_entries, dict):
        logger.warning("watcher: state file %s has an unexpected shape; ignoring",
                       file_path)
        return {}

    processed: Dict[str, Dict[str, Any]] = {}
    for key, value in raw_entries.items():
        if not isinstance(key, str):
            continue
        processed[key] = dict(value) if isinstance(value, dict) else {"status": ENTRY_DONE}
    return processed


def save_processed(path, processed: Dict[str, Dict[str, Any]]) -> bool:
    """Write the sidecar atomically.

    The write goes to a temporary file in the same directory and is then
    :func:`os.replace`-d over the target, so a crash mid-write can never leave
    a half-written JSON document that the next start would refuse to read.

    Args:
        path: Sidecar path.
        processed: Mapping to persist.

    Returns:
        ``True`` on success, ``False`` if the write failed (logged, never
        raised -- an unwritable state file must not stop downloads).
    """
    file_path = Path(path)
    payload = {
        "version": SIDECAR_VERSION,
        "updated": datetime.now().astimezone().isoformat(timespec="seconds"),
        "entries": processed,
    }
    temp_path = file_path.with_name(file_path.name + ".tmp")
    try:
        with temp_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temp_path), str(file_path))
        return True
    except OSError as exc:
        logger.error("watcher: could not write state file %s: %s", file_path, exc)
        try:
            temp_path.unlink()
        except OSError:
            pass
        return False


# --- watch file -----------------------------------------------------------


def parse_entries(text: str) -> List[str]:
    """Split watch-file text into playlist entries.

    Args:
        text: Whole file contents.

    Returns:
        Stripped lines in file order, with blanks, ``#`` comments and
        duplicates removed. De-duplication matters because the sidecar is keyed
        on the line text: two identical lines are one entry, not a repeat
        download.
    """
    entries: List[str] = []
    seen = set()
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith(COMMENT_PREFIX):
            continue
        if line in seen:
            logger.debug("watcher: duplicate entry %r ignored", line)
            continue
        seen.add(line)
        entries.append(line)
    return entries


def _stat_signature(path: Path) -> Optional[Tuple[float, int]]:
    """Return ``(mtime, size)`` for ``path``, or ``None`` if it is missing.

    Both fields are used because a same-size edit within one filesystem
    timestamp tick would otherwise slip through, and because some editors
    preserve mtime on save.
    """
    try:
        info = path.stat()
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as exc:
        logger.warning("watcher: cannot stat %s: %s", path, exc)
        return None
    return (info.st_mtime, info.st_size)


def _read_watch_file(path: Path) -> Optional[str]:
    """Read the watch file, returning ``None`` when it is missing/unreadable."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("watcher: cannot read %s: %s", path, exc)
        return None


# --- failure classification ----------------------------------------------

#: Exception types that mean "the environment is wrong, not the line".
#:
#: ``SpotifyCredentialsError`` is the headline case: the user pastes a Spotify
#: playlist before exporting ``SPOTIPY_CLIENT_ID``/``SPOTIPY_CLIENT_SECRET``.
#: The rest are the stdlib's network faults. ``ConnectionError`` covers
#: refused/reset/aborted, ``TimeoutError`` covers ``socket.timeout`` (an alias
#: since 3.10, a subclass before it), ``socket.gaierror``/``herror`` cover DNS,
#: ``urllib.error.URLError`` wraps most of the above for HTTP clients, and
#: ``http.client.HTTPException`` covers protocol-level breakage.
#:
#: Deliberately **not** here: bare ``OSError``. "Permission denied" and "no
#: space left" are not transient in the way a dropped TCP connection is, and
#: retrying them every five seconds forever is just a log-spam machine.
RETRYABLE_EXCEPTIONS = (
    SpotifyCredentialsError,
    ConnectionError,
    TimeoutError,
    socket.gaierror,
    socket.herror,
    socket.timeout,
    urllib.error.URLError,
    http.client.HTTPException,
)

#: Lower-cased substrings that mark a *message* as transient. Needed because
#: :mod:`playlist_parser` flattens Spotify API faults into plain
#: ``RuntimeError`` -- an HTTP 429 or 503 there is emphatically retryable, and
#: telling them apart from "playlist not found" means reading the text.
RETRYABLE_MESSAGE_MARKERS = (
    "http 429",
    "http 500",
    "http 502",
    "http 503",
    "http 504",
    "rate limit",
    "rate limited",
    "temporarily",
    "try again",
    "timed out",
    "timeout",
    "connection reset",
    "connection refused",
    "connection aborted",
    "network is unreachable",
    "name resolution",
    "getaddrinfo",
    "nodename nor servname",
)

#: Cap on how far the ``__cause__``/``__context__`` chain is walked.
_CAUSE_DEPTH = 10


def is_retryable(exc: BaseException) -> bool:
    """Report whether ``exc`` should leave its entry unmarked for a retry.

    The exception and its ``__cause__``/``__context__`` chain are inspected, so
    a network fault wrapped in a ``RuntimeError`` by an upper layer is still
    recognised.

    Args:
        exc: The exception raised while processing one entry.

    Returns:
        ``True`` for credential and network problems -- environmental faults
        the user fixes outside the watch file, which must therefore be tried
        again. ``False`` for everything else, which is recorded as a permanent
        failure so the loop stops re-attempting it.
    """
    current: Optional[BaseException] = exc
    for _ in range(_CAUSE_DEPTH):
        if current is None:
            break
        if isinstance(current, RETRYABLE_EXCEPTIONS):
            return True
        message = str(current).lower()
        for marker in RETRYABLE_MESSAGE_MARKERS:
            if marker in message:
                return True
        current = current.__cause__ or current.__context__
    return False


# --- one entry ------------------------------------------------------------


def _process_entry(
    entry: str,
    output_dir,
    quality: str,
    workers: int,
    overwrite: bool,
    verbose: bool,
) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """Resolve and download a single watch-file entry.

    Args:
        entry: The raw stripped line.
        output_dir: Root download directory.
        quality: MP3 bitrate in kbps.
        workers: Thread count for the pipeline.
        overwrite: Re-download tracks already in the ledger.
        verbose: Verbose yt-dlp output.

    Returns:
        A ``(succeeded, record)`` pair. ``record`` is the sidecar entry to
        store, or ``None`` when the failure was retryable and the entry must
        stay pending. This function never raises for a job-level problem.
    """
    # Imported here, not at module scope: ``downloader`` hard-imports yt-dlp,
    # and the watcher must stay importable (and testable) without it.
    from pipeline import run_job
    from playlist_parser import resolve_input

    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    logger.info("watcher: new entry %r", entry)

    try:
        job = resolve_input(entry)
        track_count = len(job.tracks or [])
        if track_count == 0:
            # Not an exception, but nothing to download either. Recorded as a
            # permanent failure so a typo'd playlist name is not re-searched
            # every five seconds.
            logger.warning("watcher: %r resolved to 0 tracks; marking as failed", entry)
            return False, {
                "status": ENTRY_FAILED,
                "timestamp": stamp,
                "tracks": 0,
                "playlist": job.name,
                "reason": "resolved to 0 tracks",
            }

        logger.info("watcher: %r -> '%s' (%d track(s))", entry, job.name, track_count)
        results = run_job(
            job,
            output_dir,
            quality=quality,
            workers=workers,
            overwrite=overwrite,
            verbose=verbose,
        )
    except KeyboardInterrupt:
        raise
    except Exception as exc:  # noqa: BLE001 - one bad line must not kill the loop
        if is_retryable(exc):
            logger.error(
                "watcher: %r failed transiently (%s: %s); leaving it queued for retry",
                entry, type(exc).__name__, exc,
            )
            return False, None
        logger.error(
            "watcher: %r failed permanently (%s: %s); it will not be retried "
            "unless the line is edited",
            entry, type(exc).__name__, exc, exc_info=verbose,
        )
        return False, {
            "status": ENTRY_FAILED,
            "timestamp": stamp,
            "tracks": 0,
            "reason": "%s: %s" % (type(exc).__name__, exc),
        }

    results = list(results or [])
    downloaded = sum(1 for item in results if getattr(item, "status", None) == STATUS_OK)
    failed = sum(1 for item in results if getattr(item, "status", None) == STATUS_FAILED)
    logger.info(
        "watcher: %r done -- %d downloaded, %d failed, %d total",
        entry, downloaded, failed, len(results),
    )
    return True, {
        "status": ENTRY_DONE,
        "timestamp": stamp,
        "tracks": len(results),
        "downloaded": downloaded,
        "failed": failed,
        "playlist": job.name,
    }


# --- one pass -------------------------------------------------------------


def run_once(
    watch_file=WATCH_FILE_NAME,
    output_dir=DEFAULT_OUTPUT_DIR,
    quality: str = BITRATE_DEFAULT,
    workers: int = MAX_WORKERS_DEFAULT,
    overwrite: bool = False,
    verbose: bool = False,
) -> PassResult:
    """Read the watch file once and process every entry not already recorded.

    This is the whole of the watcher's behaviour minus the sleeping, which is
    what makes it testable without threads or timing games.

    Args:
        watch_file: Path to the playlist list.
        output_dir: Root download directory.
        quality: MP3 bitrate in kbps.
        workers: Thread count per playlist.
        overwrite: Re-download tracks already in the ledger.
        verbose: Verbose yt-dlp output.

    Returns:
        A :class:`PassResult` describing what happened. ``missing`` is ``True``
        when the file does not exist; nothing is created in that case.

    Raises:
        KeyboardInterrupt: Propagated so the caller can shut down cleanly.
    """
    path = Path(watch_file).expanduser()
    text = _read_watch_file(path)
    if text is None:
        return PassResult(missing=True)

    entries = parse_entries(text)
    state_path = sidecar_path(path)
    processed = load_processed(state_path)

    pending = [entry for entry in entries if entry not in processed]
    if not pending:
        logger.debug("watcher: %d entr(ies), nothing new", len(entries))
        return PassResult(entries=len(entries))

    logger.info("watcher: %d new entr(ies) of %d in %s",
                len(pending), len(entries), path)

    ok_count = 0
    failed_count = 0
    retry_count = 0
    dirty = False
    try:
        for entry in pending:
            succeeded, record = _process_entry(
                entry, output_dir, quality, workers, overwrite, verbose
            )
            if record is None:
                retry_count += 1
                continue
            processed[entry] = record
            dirty = True
            if succeeded:
                ok_count += 1
            else:
                failed_count += 1
    finally:
        # Flush in a ``finally`` so a Ctrl-C halfway through a batch still
        # records the playlists that already completed.
        if dirty:
            save_processed(state_path, processed)

    return PassResult(
        entries=len(entries),
        attempted=len(pending),
        ok=ok_count,
        failed=failed_count,
        retryable=retry_count,
    )


# --- the loop -------------------------------------------------------------


def watch_loop(
    watch_file=WATCH_FILE_NAME,
    output_dir=DEFAULT_OUTPUT_DIR,
    interval: float = 5.0,
    quality: str = BITRATE_DEFAULT,
    workers: int = MAX_WORKERS_DEFAULT,
    overwrite: bool = False,
    verbose: bool = False,
    once: bool = False,
) -> int:
    """Poll ``watch_file`` and download every new entry until interrupted.

    The file is ``stat()``-ed each tick and only re-read when ``st_mtime`` or
    ``st_size`` changed, so an idle watcher does no I/O beyond one syscall. Two
    things deliberately override that cache: the very first pass (nothing is
    known yet), and any pass that left retryable failures behind -- those must
    be re-attempted once the user fixes their credentials or their network,
    which does not touch the file.

    Args:
        watch_file: Path to the playlist list. Not created if missing.
        output_dir: Root download directory.
        interval: Seconds between polls, floored at :data:`MIN_INTERVAL`.
        quality: MP3 bitrate in kbps.
        workers: Thread count per playlist.
        overwrite: Re-download tracks already in the ledger.
        verbose: Verbose yt-dlp output.
        once: Run a single pass and return instead of looping.

    Returns:
        ``0`` when nothing failed permanently (including a clean Ctrl-C), ``1``
        if at least one entry failed permanently during this run. Retryable
        failures do **not** set the exit code, because they have not been
        given up on.
    """
    path = Path(watch_file).expanduser()
    try:
        delay = float(interval)
    except (TypeError, ValueError):
        delay = 5.0
    if delay < MIN_INTERVAL:
        logger.warning("watcher: interval %r too small; using %.2fs", interval, MIN_INTERVAL)
        delay = MIN_INTERVAL

    logger.info("watcher: watching %s every %.1fs -> %s", path, delay, output_dir)
    logger.info("watcher: state file is %s", sidecar_path(path))

    failures = 0
    signature: Optional[Tuple[float, int]] = None
    first_pass = True
    warned_missing = False

    try:
        while True:
            current = _stat_signature(path)

            if current is None:
                if not warned_missing:
                    logger.info(
                        "watcher: %s does not exist yet -- waiting for it to appear "
                        "(create it and add one playlist per line)",
                        path,
                    )
                    warned_missing = True
                signature = None
                first_pass = False
                if once:
                    return 1 if failures else 0
                time.sleep(delay)
                continue

            if warned_missing:
                logger.info("watcher: %s appeared", path)
                warned_missing = False

            changed = first_pass or current != signature
            first_pass = False

            if changed:
                signature = current
                result = run_once(
                    path,
                    output_dir,
                    quality=quality,
                    workers=workers,
                    overwrite=overwrite,
                    verbose=verbose,
                )
                failures += result.failed
                if result.retryable:
                    # Nothing was written for these, and the file will not
                    # change on its own. Drop the cache so the next tick looks
                    # again instead of stalling until the user edits the file.
                    logger.info(
                        "watcher: %d entr(ies) will be retried on the next poll",
                        result.retryable,
                    )
                    signature = None
                # NOTE: ``signature`` is deliberately the stat taken *before*
                # the pass, not after it. A batch can take minutes, and edits
                # made while it ran must show up as a change on the next tick
                # rather than being absorbed into a fresh post-run signature.

            if once:
                return 1 if failures else 0
            time.sleep(delay)
    except KeyboardInterrupt:
        logger.info("Stopped.")
        return 0
