"""YouTube URL resolution and error classification for the playlist pipeline.

This module owns everything that turns a user-supplied URL into concrete work
items, plus the translation of yt-dlp's raw exceptions into short sentences a
human can act on:

* :func:`expand_urls` -- flatten playlist URLs into their individual video URLs,
* :func:`playlist_title` -- best-effort lookup of a playlist's real name,
* :func:`expand_playlist` -- one flat extraction yielding both the name and the
  :class:`~models.Track` list,
* :func:`classify_error` -- exception -> friendly reason string.

Every public function here isolates failures: a dead link becomes a failed
:class:`~models.DownloadResult`, never an exception that aborts a batch.

The module never configures logging itself; it only emits records through
``logging.getLogger(__name__)``. Configuring handlers is the caller's job
(see ``main.py``).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from yt_dlp import YoutubeDL
from yt_dlp.utils import GeoRestrictedError

from models import youtube_extractor_args, DownloadResult, Track

__all__ = [
    "classify_error",
    "expand_urls",
    "playlist_title",
    "expand_playlist",
]

logger = logging.getLogger(__name__)

# Collapses newlines/runs of spaces in exception text before it is shown.
_WHITESPACE_RE = re.compile(r"\s+")

#: Options for a metadata-only, one-request-per-playlist extraction. Reused for
#: every probing helper in this module.
_FLAT_OPTS = {
    "extract_flat": "in_playlist",
    "extractor_args": youtube_extractor_args(),
    "skip_download": True,
    "quiet": True,
    "no_warnings": True,
}


class _YDLLogger:
    """Adapter that routes yt-dlp's own output into this module's logger.

    yt-dlp writes progress and diagnostics through a duck-typed logger object
    exposing ``debug``/``info``/``warning``/``error``. Routing it here keeps all
    output on stderr under the caller's logging configuration instead of yt-dlp
    printing straight to the console.
    """

    def debug(self, msg: str) -> None:
        """Handle a yt-dlp debug/verbose line (also used for ordinary output)."""
        logger.debug("%s", msg)

    def info(self, msg: str) -> None:
        """Handle a yt-dlp informational line (demoted to DEBUG)."""
        logger.debug("%s", msg)

    def warning(self, msg: str) -> None:
        """Handle a yt-dlp warning line."""
        logger.warning("%s", msg)

    def error(self, msg: str) -> None:
        """Handle a yt-dlp error line."""
        logger.error("%s", msg)


def _flat_opts() -> Dict[str, Any]:
    """Return a fresh copy of the flat-extraction options dict.

    A fresh dict per ``YoutubeDL`` instance avoids handing the same mutable
    mapping to several extractors, which mutate the options they are given.

    Returns:
        A new options dict including this module's yt-dlp logger adapter.
    """
    opts = dict(_FLAT_OPTS)
    # Regenerated per call: dict() is a shallow copy, so the nested
    # extractor_args mapping would otherwise be shared between instances.
    opts["extractor_args"] = youtube_extractor_args()
    opts["logger"] = _YDLLogger()
    return opts


def classify_error(exc: BaseException) -> str:
    """Map an exception raised by yt-dlp (or the filesystem) to a friendly reason.

    The check order is deliberate. Region-blocking is tested *before* the
    generic "unavailable" branch because YouTube's geo message begins with
    "Video unavailable" and would otherwise be mis-reported as a removed video.

    Args:
        exc: The caught exception.

    Returns:
        A short, human-readable explanation suitable for the console and for
        the skipped/failed report.
    """
    message = str(exc) or exc.__class__.__name__
    lowered = message.lower()

    if isinstance(exc, GeoRestrictedError):
        return "Region-blocked: not available in your country"

    if "no video results" in lowered or "no results" in lowered:
        return "No search results for this track"
    if "unable to extract" in lowered and ("ytsearch" in lowered or "search" in lowered):
        return "No search results for this track"

    if "sign in to confirm your age" in lowered or "age-restricted" in lowered or "age restricted" in lowered:
        return "Age-restricted: YouTube requires a signed-in account"
    if "confirm you" in lowered and "bot" in lowered:
        return "Sign-in required: YouTube asked to verify you are not a bot"
    # Checked before the generic sign-in branch: YouTube's private/members-only
    # messages both end with "Sign in ..." and would otherwise be swallowed by it.
    if "private video" in lowered or "is private" in lowered:
        return "Private video"
    if "members-only" in lowered or "member's only" in lowered or "join this channel" in lowered:
        return "Members-only video"
    if "sign in" in lowered or "login required" in lowered or "cookies" in lowered:
        return "Sign-in required"
    if (
        "in your country" in lowered
        or "in your location" in lowered
        or "geo-restricted" in lowered
        or "geo restricted" in lowered
        or ("geo" in lowered and "block" in lowered)
    ):
        return "Region-blocked: not available in your country"
    if "copyright" in lowered:
        return "Blocked on copyright grounds"
    if "removed" in lowered or "terminated" in lowered or "deleted" in lowered:
        return "Video removed or channel terminated"
    if "unavailable" in lowered or "does not exist" in lowered or "not exist" in lowered or "404" in lowered:
        return "Video unavailable or removed"
    if (
        "ffmpeg" in lowered
        or "postprocessing" in lowered
        or "post-processing" in lowered
        or "avconv" in lowered
    ):
        return "Audio conversion failed (ffmpeg/post-processing error)"
    if (
        "timed out" in lowered
        or "timeout" in lowered
        or "connection" in lowered
        or "network" in lowered
        or "temporary failure in name resolution" in lowered
        or "unable to download" in lowered
        or "getaddrinfo" in lowered
        or "urlopen error" in lowered
        or "ssl" in lowered
    ):
        return "Network error (check your connection, then retry)"
    if isinstance(exc, PermissionError):
        return "Permission denied writing to the output directory"
    if isinstance(exc, OSError):
        errno_name = getattr(exc, "strerror", None) or message
        return "Filesystem error: %s" % errno_name

    collapsed = _WHITESPACE_RE.sub(" ", message).strip()
    if len(collapsed) > 200:
        collapsed = collapsed[:197] + "..."
    return collapsed or "Unknown error"


def _entry_to_url(entry: Dict[str, Any]) -> Optional[str]:
    """Best-effort extraction of a watchable URL from a flat playlist entry.

    Args:
        entry: One element of a yt-dlp playlist ``entries`` list.

    Returns:
        A URL string, or ``None`` if the entry carries nothing usable.
    """
    if not isinstance(entry, dict):
        return None
    for key in ("url", "webpage_url"):
        value = entry.get(key)
        if value:
            return str(value)
    video_id = entry.get("id")
    if video_id:
        return "https://www.youtube.com/watch?v=%s" % video_id
    return None


def expand_urls(urls: Sequence[str]) -> Tuple[List[str], List[DownloadResult]]:
    """Resolve playlists into their individual video URLs.

    A single flat-extraction ``YoutubeDL`` instance is reused for the whole
    batch: ``extract_flat="in_playlist"`` costs one cheap request per playlist
    rather than one per video. Non-playlist URLs pass through untouched.
    Unresolvable URLs become failure results instead of raising, so one dead
    link never aborts the batch.

    Args:
        urls: Input URLs, in the order the user supplied them.

    Returns:
        A ``(resolved_urls, failures)`` tuple. ``resolved_urls`` is
        de-duplicated and order-preserving; ``failures`` holds one
        ``DownloadResult`` with status ``"failed"`` per URL that could not be
        resolved.

    Raises:
        KeyboardInterrupt: Propagated so the caller can stop cleanly.
    """
    resolved: List[str] = []
    failures: List[DownloadResult] = []
    seen = set()

    if not urls:
        return resolved, failures

    def _add(candidate: Optional[str]) -> None:
        if not candidate or candidate in seen:
            return
        seen.add(candidate)
        resolved.append(candidate)

    with YoutubeDL(_flat_opts()) as ydl:
        for url in urls:
            try:
                info = ydl.extract_info(url, download=False)
                if not info:
                    raise ValueError("no metadata returned")
                if info.get("_type") == "playlist":
                    entries = info.get("entries") or []
                    count = 0
                    for entry in entries:
                        if entry is None:
                            continue
                        entry_url = _entry_to_url(entry)
                        if entry_url:
                            _add(entry_url)
                            count += 1
                    logger.info(
                        "Playlist '%s' expanded to %d item(s)",
                        info.get("title") or url,
                        count,
                    )
                else:
                    _add(url)
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # noqa: BLE001 - resolution must never raise
                reason = classify_error(exc)
                logger.warning("Could not resolve %s - %s", url, reason)
                failures.append(DownloadResult.failed(url, reason))

    return resolved, failures


def playlist_title(url: str) -> Optional[str]:
    """Look up the real name of a playlist URL.

    Used to name the download folder after the playlist instead of falling back
    to a generic label. One flat extraction, no download.

    Best-effort by design: any failure (network, private playlist, malformed
    URL) yields ``None`` rather than an exception, because a missing folder name
    is never a reason to abort a job.

    Args:
        url: The URL to inspect. May be a playlist or a single video.

    Returns:
        The playlist title, or ``None`` when the URL is not a playlist, carries
        no title, or could not be extracted.

    Raises:
        KeyboardInterrupt: Propagated so the caller can stop cleanly.
    """
    try:
        with YoutubeDL(_flat_opts()) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            return None
        if info.get("_type") != "playlist":
            return None
        title = info.get("title")
        return str(title) if title else None
    except KeyboardInterrupt:
        raise
    except Exception as exc:  # noqa: BLE001 - naming must never break a job
        logger.debug("Could not read playlist title for %s: %s", url, exc)
        return None


def expand_playlist(url: str) -> Tuple[Optional[str], List[Track], List[DownloadResult]]:
    """Expand one URL into its playlist name and the tracks it contains.

    A single flat extraction supplies both the playlist title and every entry,
    so a 200-item playlist costs one request rather than 201.

    The returned tracks carry only ``query`` and ``title``. Artist, album, cover
    art and duration are deliberately left ``None``: YouTube-sourced tracks get
    their tags from yt-dlp's own metadata post-processors at download time,
    unlike Spotify-sourced tracks whose metadata is authoritative up front.

    Args:
        url: A YouTube playlist URL, or any other URL yt-dlp understands.

    Returns:
        A ``(title, tracks, failures)`` tuple.

        * ``title`` is the playlist name, or ``None`` for a non-playlist URL or
          a playlist without a title.
        * ``tracks`` are in playlist order; entry titles are used when the flat
          extraction provides them, otherwise the video URL stands in.
        * ``failures`` holds a single failed :class:`~models.DownloadResult`
          when the URL could not be extracted at all, else it is empty.

        A URL that is not a playlist yields ``(None, [Track(url, url)], [])``.

    Raises:
        KeyboardInterrupt: Propagated so the caller can stop cleanly.
    """
    tracks: List[Track] = []
    failures: List[DownloadResult] = []

    try:
        with YoutubeDL(_flat_opts()) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            raise ValueError("no metadata returned")
    except KeyboardInterrupt:
        raise
    except Exception as exc:  # noqa: BLE001 - expansion must never raise
        reason = classify_error(exc)
        logger.warning("Could not expand %s - %s", url, reason)
        return None, tracks, [DownloadResult.failed(url, reason)]

    if info.get("_type") != "playlist":
        # Prefer the real video title so progress output and the eventual
        # filename read as a song name rather than a raw URL.
        single_title = info.get("title")
        if not isinstance(single_title, str) or not single_title.strip():
            single_title = url
        return None, [Track(query=url, title=single_title.strip())], failures

    raw_title = info.get("title")
    title = str(raw_title) if raw_title else None

    for entry in info.get("entries") or []:
        if entry is None:
            continue
        entry_url = _entry_to_url(entry)
        if not entry_url:
            continue
        entry_title = entry.get("title") if isinstance(entry, dict) else None
        tracks.append(Track(query=entry_url, title=str(entry_title) if entry_title else entry_url))

    logger.info("Playlist '%s' expanded to %d track(s)", title or url, len(tracks))
    return title, tracks, failures
