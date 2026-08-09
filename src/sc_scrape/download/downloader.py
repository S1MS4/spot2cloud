"""Audio engine: everything that touches yt-dlp and the resulting MP3.

Downloading, filename sanitisation, ID3 tagging and the ``skipped.txt`` report
live here. Playlist/URL expansion and error classification live in
``resolver.py``; concurrency lives in ``pipeline.py``.

Logging handlers are the caller's job (see ``main.py``); this module only emits
through ``logging.getLogger(__name__)``.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
import unicodedata
import urllib.request
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError, ExtractorError, GeoRestrictedError

from sc_scrape.download.resolver import YDLLogger, classify_error
from sc_scrape.models import (
    AUDIO_FORMAT_DEFAULT,
    BITRATE_DEFAULT,
    DEFAULT_OUTPUT_DIR,
    MAX_FILENAME_BYTES,
    SKIPPED_LOG_NAME,
    STATUS_FAILED,
    DownloadResult,
    Track,
    youtube_extractor_args,
)

__all__ = [
    # Re-exported from models; callers already import these from here.
    "DownloadResult",
    "DEFAULT_OUTPUT_DIR",
    "FFmpegNotFoundError",
    "check_ffmpeg",
    "sanitize_filename",
    "ensure_output_dir",
    "build_ydl_opts",
    "probe_existing_download",
    "rename_to_title",
    "tag_mp3",
    "download_one",
    "probe_chapters",
    "download_chapters",
    "write_failed_log",
]

logger = logging.getLogger(__name__)

# Illegal in filenames on Windows, awkward on POSIX.
_ILLEGAL_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WHITESPACE_RE = re.compile(r"\s+")

# Windows reserves these device names regardless of extension.
_WINDOWS_RESERVED_STEMS = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + ["COM%d" % i for i in range(1, 10)]
    + ["LPT%d" % i for i in range(1, 10)]
)

_COVER_TIMEOUT = 10

_FFMPEG_INSTALL_HINT = (
    "ffmpeg was not found on your PATH, but it is required to convert audio to "
    "MP3, write ID3 tags and embed cover art.\n"
    "\n"
    "Install it with one of:\n"
    "  macOS (Homebrew):  brew install ffmpeg\n"
    "  Debian / Ubuntu:   sudo apt install ffmpeg\n"
    "  Fedora:            sudo dnf install ffmpeg\n"
    "  Windows (winget):  winget install Gyan.FFmpeg\n"
    "\n"
    "On Windows you may need to open a new terminal (or add ffmpeg's bin\n"
    "directory to PATH manually) before it becomes visible."
)


class FFmpegNotFoundError(RuntimeError):
    """Raised when the ``ffmpeg`` executable cannot be located on ``PATH``."""


def check_ffmpeg() -> str:
    """Return the path to ``ffmpeg``, or raise :exc:`FFmpegNotFoundError`.

    Called up front so a missing ffmpeg fails fast with install instructions,
    instead of dying half-way through yt-dlp post-processing.
    """
    path = shutil.which("ffmpeg")
    if not path:
        raise FFmpegNotFoundError(_FFMPEG_INSTALL_HINT)
    logger.debug("Found ffmpeg at %s", path)
    return path


def sanitize_filename(name: str, max_bytes: int = MAX_FILENAME_BYTES) -> str:
    """Turn an arbitrary title into a safe, portable filename stem.

    Currency (``Sc``) and math (``Sm``) symbols such as ``$``, ``+`` and ``=`` are
    deliberately kept so titles stay readable; emoji and control characters are
    not. Falls back to ``"untitled"`` when nothing of the input survives.
    """
    if not name:
        return "untitled"
    if not isinstance(name, str):
        name = str(name)

    normalized = unicodedata.normalize("NFKC", name)

    kept_chars: List[str] = []
    for char in normalized:
        category = unicodedata.category(char)
        if category.startswith("C"):  # control, format, surrogate, unassigned
            continue
        if category in ("So", "Sk"):  # emoji, pictographs, (tm), modifier symbols
            continue
        kept_chars.append(char)
    cleaned = "".join(kept_chars)

    cleaned = _ILLEGAL_CHARS_RE.sub("", cleaned)
    cleaned = _WHITESPACE_RE.sub(" ", cleaned)
    cleaned = cleaned.strip().strip(".").strip()

    if not cleaned:
        return "untitled"

    encoded = cleaned.encode("utf-8")
    if len(encoded) > max_bytes:
        # Filesystems cap names in bytes, not characters, so the cut happens on
        # the encoded form; errors="ignore" discards the half-cut code point.
        cleaned = encoded[:max_bytes].decode("utf-8", errors="ignore").rstrip()
        cleaned = cleaned.rstrip(".").rstrip()

    if not cleaned:
        return "untitled"

    device_candidate = cleaned.split(".", 1)[0].strip().upper()
    if device_candidate in _WINDOWS_RESERVED_STEMS:
        cleaned = "_" + cleaned

    return cleaned or "untitled"


def ensure_output_dir(path: Union[str, os.PathLike]) -> Path:
    """Create the output directory if needed and return its resolved path."""
    resolved = Path(path).expanduser().resolve()
    if resolved.exists() and not resolved.is_dir():
        raise NotADirectoryError(
            "Output path '%s' already exists and is not a directory. "
            "Choose a different --output-dir." % resolved
        )
    resolved.mkdir(parents=True, exist_ok=True)
    logger.debug("Using output directory %s", resolved)
    return resolved


def build_ydl_opts(
    output_dir: Path,
    quality: str = BITRATE_DEFAULT,
    verbose: bool = False,
    audio_format: str = AUDIO_FORMAT_DEFAULT,
) -> Dict[str, Any]:
    """Build a fresh yt-dlp options dict for real downloads.

    The output template is id-based because ids are always filesystem-safe;
    :func:`download_one` renames the finished file to the sanitised title.

    Post-processor order is preserved by yt-dlp and matters: extract audio, then
    write ID3 tags, then embed the cover art ``writethumbnail`` fetched. Those
    last two are the metadata *fallback* for YouTube-only tracks --
    :func:`tag_mp3` overrides them when the track has authoritative metadata.

    Fan-out is disabled: ``noplaylist`` holds a video+playlist URL to the single
    video, and ``playlistend`` holds a ``ytsearchN:`` query to its first hit,
    which would otherwise download all N candidates. Expanding playlists is
    ``resolver.expand_playlist``' job, not this one's.

    ``quality`` is the MP3 bitrate in kbps; empty or ``None`` falls back to
    :data:`BITRATE_DEFAULT`.
    """
    preferred_quality = str(quality).strip() if quality is not None else ""
    if not preferred_quality:
        preferred_quality = BITRATE_DEFAULT

    return {
        "format": "bestaudio/best",
        "outtmpl": str(Path(output_dir) / "%(id)s.%(ext)s"),
        "writethumbnail": True,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": audio_format or AUDIO_FORMAT_DEFAULT,
                "preferredquality": preferred_quality,
            },
            {"key": "FFmpegMetadata", "add_metadata": True, "add_chapters": False},
            {"key": "EmbedThumbnail", "already_have_thumbnail": False},
        ],
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
        "quiet": not verbose,
        "no_warnings": not verbose,
        "ignoreerrors": False,  # download_one isolates per-track errors itself
        "noplaylist": True,
        "playlistend": 1,
        "default_search": "ytsearch",
        "extractor_args": youtube_extractor_args(),
        "logger": YDLLogger(logger),
    }


def _unique_path(target: Path) -> Path:
    """Return ``target``, or the first free ``name (n).mp3`` variant of it."""
    if not target.exists():
        return target
    stem = target.stem
    suffix = target.suffix
    parent = target.parent
    counter = 1
    while True:
        candidate = parent / ("%s (%d)%s" % (stem, counter, suffix))
        if not candidate.exists():
            return candidate
        counter += 1


def _same_file(a: Path, b: Path) -> bool:
    """Whether ``a`` and ``b`` are one file: by inode, else by resolved path."""
    try:
        if a.exists() and b.exists():
            return os.path.samefile(str(a), str(b))
    except OSError:
        pass
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return str(a) == str(b)


def _target_path_for_title(
    output_dir: Path,
    title: Optional[str],
    fallback: Optional[str] = None,
    ext: str = ".mp3",
) -> Path:
    """Final path for a title, falling back to ``fallback`` (usually the id)."""
    return Path(output_dir) / (sanitize_filename(title or fallback or "untitled") + ext)


def _is_authoritative(track: Optional[Track]) -> bool:
    """Whether the track's metadata came from an upstream catalogue (Spotify).

    Authoritative metadata beats whatever YouTube reports, for both the filename
    and the ID3 tags.
    """
    return bool(track is not None and track.source_id and track.title)


def _target_path_for_track(
    output_dir: Path,
    track: Optional[Track],
    info: Optional[Dict[str, Any]] = None,
    ext: str = ".mp3",
) -> Path:
    """Final path for a track, preferring the track's own metadata.

    ``ext`` comes from the file yt-dlp actually produced, so the pipeline
    follows whatever ``--format`` asked for without threading the format
    through every call site.

    An authoritative track is named by its ``title`` alone -- never
    ``Artist - Title`` -- because that is what the catalogue calls it. Anything
    else falls back to the resolved YouTube title, then the video id.
    """
    if _is_authoritative(track):
        # Prefix the catalogue position when we know it: file managers and car
        # stereos sort by filename, not by the TRCK tag, so without this an
        # album plays alphabetically.
        stem = sanitize_filename(track.title)
        if track.track_number:
            stem = "%02d - %s" % (track.track_number, stem)
        return Path(output_dir) / (stem + ext)
    info = info or {}
    return _target_path_for_title(output_dir, info.get("title"), info.get("id"), ext)


def _first_entry(info: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """First non-empty entry of a playlist-shaped info dict, or ``None``.

    ``ytsearchN:`` queries and playlist URLs both come back as
    ``{"_type": "playlist", "entries": [...]}``.
    """
    entries = [e for e in (info.get("entries") or []) if e]
    if not entries:
        return None
    return entries[0]


def _format_suffix(base_opts: Optional[Dict[str, Any]]) -> str:
    """Extension the configured ``--format`` will produce.

    Read back off the extract-audio post-processor rather than threading the
    format through every call site, so the skip probe looks for the file the
    download would actually write.
    """
    for processor in (base_opts or {}).get("postprocessors") or []:
        codec = processor.get("preferredcodec")
        if codec:
            return ".%s" % codec
    return ".%s" % AUDIO_FORMAT_DEFAULT


def probe_existing_download(
    url: str,
    output_dir: Path,
    base_opts: Optional[Dict[str, Any]] = None,
    track: Optional[Track] = None,
) -> Optional[DownloadResult]:
    """Return a ``"skipped"`` result if the MP3 already exists, else ``None``.

    This is the skip-existing optimisation, and it saves downloading megabytes
    only to throw them away. An authoritative track already determines its own
    filename, so the check is a pure filesystem stat that touches no network at
    all; anything else needs one cheap metadata-only extraction to learn the title
    the file would have been named after.

    Any failure is swallowed and reported as ``None``, so the real download attempt
    is the one that produces the authoritative error.
    """
    if _is_authoritative(track):
        target = _target_path_for_track(output_dir, track)
        if target.exists():
            return DownloadResult.skipped(
                url, track.title, target, "already downloaded (%s)" % target.name
            )
        return None

    opts = {
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "playlistend": 1,
        "default_search": "ytsearch",
        "extractor_args": youtube_extractor_args(),
        "socket_timeout": 30,
        "logger": YDLLogger(logger),
    }
    if base_opts:
        for key in ("quiet", "no_warnings", "socket_timeout", "retries"):
            if key in base_opts:
                opts[key] = base_opts[key]
        opts["quiet"] = True  # the probe is an implementation detail; never let it talk

    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            return None
        if info.get("_type") == "playlist":
            entry = _first_entry(info)
            if entry is None:
                return None
            info = entry
        title = info.get("title")
        target = _target_path_for_track(output_dir, track, info, _format_suffix(base_opts))
        if target.exists():
            return DownloadResult.skipped(
                url, title, target, "already downloaded (%s)" % target.name
            )
        return None
    except KeyboardInterrupt:
        raise
    except Exception as exc:  # noqa: BLE001 - probing must never break a batch
        logger.debug("Existing-file probe failed for %s: %s", url, exc)
        return None


def _downloaded_path(info: Dict[str, Any], output_dir: Path) -> Optional[Path]:
    """Locate the post-processed MP3 yt-dlp produced, or ``None`` if it is gone.

    yt-dlp does not reliably report the path after post-processing, so the
    reported names are tried first and the id-based output template second.
    """
    requested = info.get("requested_downloads")
    if isinstance(requested, list) and requested:
        first = requested[0]
        if isinstance(first, dict):
            for key in ("filepath", "_filename", "filename"):
                value = first.get(key)
                if value:
                    candidate = Path(value)
                    if candidate.exists():
                        return candidate

    video_id = info.get("id")
    if video_id:
        candidate = Path(output_dir) / ("%s.mp3" % video_id)
        if candidate.exists():
            return candidate
        matches = sorted(Path(output_dir).glob("%s.*" % video_id))
        for match in matches:
            if match.suffix.lower() == ".mp3":
                return match

    filename = info.get("_filename")
    if filename:
        candidate = Path(filename).with_suffix(".mp3")
        if candidate.exists():
            return candidate

    return None


def rename_to_title(
    downloaded: Path,
    target: Path,
    overwrite: bool = False,
) -> Tuple[Path, bool]:
    """Move a freshly downloaded file onto its final, title-based path.

    Returns ``(final_path, reused_existing)``, where ``reused_existing`` is true
    only when ``target`` was already on disk and ``overwrite`` let it win -- which
    the caller reports as a skip.
    """
    if _same_file(downloaded, target):
        return target, False

    if target.exists():
        if not overwrite:
            # Another video already claimed this name; drop the fresh copy.
            try:
                downloaded.unlink()
            except OSError as exc:
                logger.debug("Could not remove %s: %s", downloaded, exc)
            return target, True
        os.replace(str(downloaded), str(target))
        return target, False

    target = _unique_path(target)
    os.replace(str(downloaded), str(target))
    return target, False


def _fetch_cover(url: str) -> Optional[Tuple[bytes, str]]:
    """Fetch cover art as ``(image_bytes, mime_type)``, or ``None`` if empty.

    The MIME type is taken from the response but forced to something image-ish,
    since ID3 readers choke on a bogus one.
    """
    with urllib.request.urlopen(url, timeout=_COVER_TIMEOUT) as response:
        data = response.read()
        mime = response.headers.get("Content-Type") or ""
    if not data:
        return None
    mime = mime.split(";")[0].strip().lower()
    if not mime.startswith("image/"):
        mime = "image/jpeg"
    return data, mime


def tag_mp3(path: Path, track: Track) -> None:
    """Write the track's authoritative metadata into the MP3's ID3 tags.

    Only fields the track actually carries are written, and ``cover_url`` is
    embedded as an ``APIC`` front cover replacing any existing artwork. yt-dlp's
    ``FFmpegMetadata`` and ``EmbedThumbnail`` post-processors have already run
    by this point and are the fallback for YouTube-only sources, so a track with
    no upstream metadata leaves the existing tags untouched.

    Best-effort by design: every failure (mutagen missing, unreadable file,
    unreachable cover URL) is logged as a warning and swallowed, because a
    tagging problem must never fail an otherwise-good download. Nothing but
    ``KeyboardInterrupt`` escapes.
    """
    try:
        from mutagen.id3 import (
            APIC,
            ID3,
            TALB,
            TIT2,
            TPE1,
            TRCK,
            ID3NoHeaderError,
        )

        try:
            tags = ID3(str(path))
        except ID3NoHeaderError:
            tags = ID3()

        changed = False
        if track.title:
            tags.setall("TIT2", [TIT2(encoding=3, text=[track.title])])
            changed = True
        if track.artist:
            tags.setall("TPE1", [TPE1(encoding=3, text=[track.artist])])
            changed = True
        if track.album:
            tags.setall("TALB", [TALB(encoding=3, text=[track.album])])
            changed = True
        if track.track_number:
            tags.setall("TRCK", [TRCK(encoding=3, text=[str(track.track_number)])])
            changed = True

        if track.cover_url:
            try:
                cover = _fetch_cover(track.cover_url)
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # noqa: BLE001 - artwork is optional
                logger.warning("Could not fetch cover art for %s: %s", path.name, exc)
                cover = None
            if cover is not None:
                data, mime = cover
                tags.delall("APIC")
                tags.add(APIC(encoding=0, mime=mime, type=3, desc="Cover", data=data))
                changed = True

        if not changed:
            logger.debug("No metadata to write for %s", path.name)
            return

        tags.save(str(path), v2_version=3)
        logger.debug("Tagged %s", path.name)
    except KeyboardInterrupt:
        raise
    except Exception as exc:  # noqa: BLE001 - tagging must never fail a download
        logger.warning("Could not tag %s: %s", path, exc)


def _tagging_track(track: Track, info: Dict[str, Any]) -> Track:
    """Return the track whose metadata :func:`tag_mp3` should apply.

    An authoritative track is used as-is. For a plain YouTube URL the title is
    *not* metadata -- the caller puts the URL itself there -- and yt-dlp's
    ``FFmpegMetadata`` pass already wrote a correct one, so the title is cleared
    and only genuinely-known fields are applied.
    """
    if _is_authoritative(track):
        return track
    return replace(track, title=None)


def download_one(
    track: Track,
    output_dir: Path,
    opts: Dict[str, Any],
    overwrite: bool = False,
) -> DownloadResult:
    """Download one track and rename the result to its final title.

    ``track.query`` goes straight to yt-dlp, so it may be a plain URL or a
    ``ytsearchN:"..."`` expression; a search comes back playlist-shaped and its
    first entry is used. An authoritative track names the file itself, otherwise
    the resolved YouTube title does. On success the file goes through
    :func:`tag_mp3`. ``output_dir`` must already exist.

    Per-track problems never raise: every failure mode becomes a failed result so
    the batch can carry on. ``KeyboardInterrupt`` does propagate.
    """
    output_dir = Path(output_dir)
    query = track.query

    if not overwrite:
        existing = probe_existing_download(query, output_dir, opts, track=track)
        if existing is not None:
            return existing

    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(query, download=True)

        if not info:
            return DownloadResult.failed(
                query, "yt-dlp returned no metadata for this query", title=track.title
            )
        if info.get("_type") == "playlist":
            entry = _first_entry(info)
            if entry is None:
                return DownloadResult.failed(query, "no search results", title=track.title)
            info = entry

        title = track.title if _is_authoritative(track) else info.get("title")
        downloaded = _downloaded_path(info, output_dir)
        if downloaded is None:
            return DownloadResult.failed(
                query,
                "Download finished but the converted MP3 could not be located",
                title=title,
            )

        target = _target_path_for_track(output_dir, track, info, downloaded.suffix or ".mp3")
        final_path, reused_existing = rename_to_title(downloaded, target, overwrite=overwrite)

        if reused_existing:
            return DownloadResult.skipped(
                query, title, final_path, "already downloaded (%s)" % final_path.name
            )

        tag_mp3(final_path, _tagging_track(track, info))
        return DownloadResult.ok(query, title, final_path)

    except KeyboardInterrupt:
        raise
    except (DownloadError, ExtractorError, GeoRestrictedError, OSError) as exc:
        return DownloadResult.failed(query, classify_error(exc), title=track.title)
    except Exception as exc:
        logger.debug("Unexpected error for %s", query, exc_info=True)
        return DownloadResult.failed(query, classify_error(exc), title=track.title)


def probe_chapters(
    url: str,
    opts: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Return the video's chapter markers, or ``[]`` when it has none.

    Metadata-only, and best-effort: anything that goes wrong is reported as "no
    chapters", so the caller falls back to a plain single-file download rather than
    failing. Each chapter is yt-dlp's own dict,
    ``{"start_time": 0.0, "title": "...", "end_time": 198.0}``.
    """
    probe_opts = {
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "playlistend": 1,
        "default_search": "ytsearch",
        "extractor_args": youtube_extractor_args(),
        "socket_timeout": 30,
        "logger": YDLLogger(logger),
    }
    if opts:
        for key in ("socket_timeout", "retries"):
            if key in opts:
                probe_opts[key] = opts[key]

    try:
        with YoutubeDL(probe_opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            return []
        if info.get("_type") == "playlist":
            entry = _first_entry(info)
            if entry is None:
                return []
            info = entry
        return [c for c in (info.get("chapters") or []) if isinstance(c, dict)]
    except KeyboardInterrupt:
        raise
    except Exception as exc:  # noqa: BLE001 - probing must never break a batch
        logger.debug("Chapter probe failed for %s: %s", url, exc)
        return []


def _chapter_title(chapter: Dict[str, Any], index: int) -> str:
    """Title of one chapter, named by position when the marker carries none."""
    title = (chapter.get("title") or "").strip() if isinstance(chapter, dict) else ""
    return title or ("Chapter %d" % index)


def _chapter_target(output_dir: Path, index: int, title: str) -> Path:
    """Final path of one piece: ``NN - Title.mp3``, numbered from 1.

    The name is derived from the chapter alone, never from what the splitter
    happened to write, so the same piece always resolves to the same path and
    the skip-existing check below is a plain stat.
    """
    return Path(output_dir) / ("%02d - %s.mp3" % (index, sanitize_filename(title)))


def _chapter_track(track: Track, title: str, index: int) -> Track:
    """Metadata for one piece: its own title and number, the album's everything else.

    yt-dlp stamps the *parent* video's tags onto every split piece, so without
    this each track would show up in a player as another copy of the full-album
    stream. Whatever the incoming track knows (a Spotify album name, its square
    cover) is authoritative and inherited.
    """
    return Track(
        query=track.query,
        title=title,
        artist=track.artist,
        album=track.album,
        track_number=index,
        cover_url=track.cover_url,
    )


def _chapter_piece_path(
    chapter: Dict[str, Any],
    pieces_dir: Path,
    index: int,
) -> Optional[Path]:
    """Locate the file the splitter wrote for one chapter, or ``None``.

    ``FFmpegSplitChaptersPP`` records its destination on the chapter dict; the
    numbered output template is the fallback for when it does not.
    """
    recorded = chapter.get("filepath") if isinstance(chapter, dict) else None
    if recorded:
        candidate = Path(recorded)
        if candidate.exists():
            return candidate

    candidate = Path(pieces_dir) / ("%03d.mp3" % index)
    if candidate.exists():
        return candidate
    for match in sorted(Path(pieces_dir).glob("%03d.*" % index)):
        if match.suffix.lower() == ".mp3":
            return match
    return None


def download_chapters(
    track: Track,
    output_dir: Path,
    opts: Dict[str, Any],
    overwrite: bool = False,
) -> List[DownloadResult]:
    """Download one chaptered video and split it into an MP3 per chapter.

    For the "Full Album Stream" upload: one 32-minute video with chapter markers,
    one MP3 per track out. Pieces land in ``output_dir`` as ``NN - Title.mp3`` and
    are re-tagged individually, because yt-dlp copies the parent video's ID3 tags
    onto every piece it cuts.

    The un-split parent is a duplicate of the pieces, so the whole download happens
    in a temporary directory *inside* ``output_dir`` -- same filesystem, so moving
    a piece out is a rename rather than a copy -- which is then removed wholesale.

    Returns one result per chapter in chapter order, or a single failed result when
    the download could not be made sense of.
    """
    output_dir = Path(output_dir)
    query = track.query

    if not overwrite:
        # A resume must not re-fetch half an hour of audio to then skip every
        # piece of it, so the pieces are named from a metadata-only probe first.
        known = probe_chapters(query, opts)
        targets = [
            _chapter_target(output_dir, index, _chapter_title(chapter, index))
            for index, chapter in enumerate(known, start=1)
        ]
        if targets and all(target.exists() for target in targets):
            return [
                DownloadResult.skipped(
                    query,
                    _chapter_title(chapter, index),
                    target,
                    "already downloaded (%s)" % target.name,
                )
                for index, (chapter, target) in enumerate(zip(known, targets), start=1)
            ]

    try:
        tmp_dir = Path(tempfile.mkdtemp(prefix=".chapters-", dir=str(output_dir)))
    except OSError as exc:
        return [DownloadResult.failed(query, classify_error(exc), title=track.title)]

    try:
        pieces_dir = tmp_dir / "pieces"
        split_opts = dict(opts)
        split_opts["outtmpl"] = {
            "default": str(tmp_dir / "%(id)s.%(ext)s"),
            # Numbered rather than titled: yt-dlp's sanitiser is not ours, and
            # every piece is renamed to its final name below anyway.
            "chapter": str(pieces_dir / "%(section_number)03d.%(ext)s"),
        }
        # Appended, not inserted: splitting must happen after the audio has been
        # extracted to MP3, or the pieces come out in the source container.
        split_opts["postprocessors"] = [
            *(opts.get("postprocessors") or []),
            {"key": "FFmpegSplitChapters", "force_keyframes": False},
        ]

        with YoutubeDL(split_opts) as ydl:
            info = ydl.extract_info(query, download=True)

        if not info:
            return [
                DownloadResult.failed(
                    query, "yt-dlp returned no metadata for this query", title=track.title
                )
            ]
        if info.get("_type") == "playlist":
            entry = _first_entry(info)
            if entry is None:
                return [DownloadResult.failed(query, "no search results", title=track.title)]
            info = entry

        chapters = [c for c in (info.get("chapters") or []) if isinstance(c, dict)]
        if not chapters:
            return [
                DownloadResult.failed(
                    query, "the video reported no chapters to split", title=track.title
                )
            ]

        results: List[DownloadResult] = []
        for index, chapter in enumerate(chapters, start=1):
            title = _chapter_title(chapter, index)
            target = _chapter_target(output_dir, index, title)

            if target.exists() and not overwrite:
                results.append(
                    DownloadResult.skipped(
                        query, title, target, "already downloaded (%s)" % target.name
                    )
                )
                continue

            piece = _chapter_piece_path(chapter, pieces_dir, index)
            if piece is None:
                results.append(
                    DownloadResult.failed(
                        query, "chapter %d was not written by the splitter" % index, title=title
                    )
                )
                continue

            os.replace(str(piece), str(target))
            tag_mp3(target, _chapter_track(track, title, index))
            results.append(DownloadResult.ok(query, title, target))

        return results

    except KeyboardInterrupt:
        raise
    except (DownloadError, ExtractorError, GeoRestrictedError, OSError) as exc:
        return [DownloadResult.failed(query, classify_error(exc), title=track.title)]
    except Exception as exc:
        logger.debug("Unexpected error splitting %s", query, exc_info=True)
        return [DownloadResult.failed(query, classify_error(exc), title=track.title)]
    finally:
        # Takes the un-split parent with it: half an hour of audio already
        # present, piece by piece, in output_dir.
        shutil.rmtree(str(tmp_dir), ignore_errors=True)


def write_failed_log(results: Sequence[DownloadResult], output_dir: Path) -> Optional[Path]:
    """Write ``skipped.txt``, one ``URL<TAB>reason`` line per failed result.

    Returns the log's path, or ``None`` when there were no failures or it could not
    be written. An unwritable output directory is tolerated: the report is a
    convenience, not a deliverable, and must not sink a batch that has otherwise
    finished.
    """
    failures = [r for r in results if r.status == STATUS_FAILED]
    if not failures:
        return None

    log_path = Path(output_dir) / SKIPPED_LOG_NAME
    lines = []
    for result in failures:
        reason = _WHITESPACE_RE.sub(" ", result.reason or "Unknown error").strip()
        lines.append("%s\t%s" % (result.url, reason))

    try:
        log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not write %s: %s", log_path, exc)
        return None

    logger.debug("Wrote %d failure(s) to %s", len(failures), log_path)
    return log_path
