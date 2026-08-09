#!/usr/bin/env python3
"""Command-line entry point for the playlist-to-MP3 pipeline.

Accepts anything a user is likely to paste -- a Spotify playlist or album link
or URI, a YouTube playlist/video URL, or just a playlist *name* to search for --
resolves each one into a :class:`~models.PlaylistJob` via
:func:`playlist_parser.resolve_input`, and hands every job to
:func:`pipeline.run_job` for concurrent downloading.

With ``--upload`` (or ``--dir``, which uploads a folder already on disk) the
finished album folders are then published to the user's own SoundCloud account
through :mod:`uploader` -- this half exists to put your own releases on your own
profile. It drives the browser session :mod:`browser_engine` restores from
``storage_state.json``, or, with ``--attach``, the Chrome you already have open.
That session is created once by ``--login``; no password ever passes through
this program.

Stream discipline
-----------------
Everything diagnostic (log records, the tqdm progress bar, per-track lines,
error messages) goes to **stderr**. The only thing on **stdout** is the final
summary, so ``main.py ... | grep failed`` stays useful and piping the tool into
another program never has to strip terminal escape codes.

Exit codes
----------
=====  =========================================================
    0  everything downloaded or was already present
    1  at least one track failed, or a setup step failed
    2  usage error (no inputs, bad ``--quality``, bad flags)
  130  interrupted with Ctrl-C
=====  =========================================================
"""

from __future__ import annotations

import argparse
import inspect
import logging
import sys
import warnings
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple


def _silence_third_party_warnings() -> None:
    """Hide the ``NotOpenSSLWarning`` urllib3 emits under macOS system Python.

    urllib3 v2 warns, at import time, that the interpreter's ``ssl`` module is
    linked against LibreSSL rather than OpenSSL 1.1.1+. It is emitted through
    ``requests`` the moment ``spotipy`` is imported, it is noise the user can do
    nothing about, and it lands in the middle of the progress output. This must
    run *before* anything that reaches ``urllib3`` is imported, which is why it
    is called at module import time as well as from :func:`main`.
    """
    warnings.filterwarnings("ignore", message=".*OpenSSL.*")


# Must precede any import that can transitively reach urllib3 (spotipy, yt-dlp).
# ``models`` is pure dataclasses, so it is safe either way; every other project
# module is imported lazily inside the functions below.
_silence_third_party_warnings()

from models import (  # noqa: E402 - deliberate: see _silence_third_party_warnings
    AUDIO_FORMATS,
    BITRATE_DEFAULT,
    DEFAULT_OUTPUT_DIR,
    MAX_WORKERS_DEFAULT,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_SKIPPED,
    DownloadResult,
    PlaylistJob,
)

__all__ = ["build_parser", "main"]

logger = logging.getLogger("sc-scrape")

#: Exit statuses, named so the returns below read as intent rather than magic.
EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130

#: Accepted ``--quality`` range in kbps. Below 64 the MP3 is unpleasant; 320 is
#: the top of the LAME/ffmpeg CBR scale, so anything higher is a typo.
QUALITY_MIN = 64
QUALITY_MAX = 320

#: How much of a playlist name fits in the progress bar's description.
DESC_WIDTH = 30

#: Playwright storage-state file: the saved SoundCloud session, written by
#: ``--login`` and read by every ``--upload`` run.
STATE_FILE_DEFAULT = "storage_state.json"

#: Where ``--attach`` looks for an already-running Chrome. Matches the port in
#: browser_engine's own instructions for starting one.
ATTACH_ENDPOINT_DEFAULT = "http://127.0.0.1:9222"

#: Seconds ``--login`` waits for the user to finish logging in by hand. Long,
#: because it can involve a password manager, an e-mail code and a captcha.
LOGIN_TIMEOUT = 300

#: Glyphs used in per-track progress lines, and their ASCII stand-ins. Windows
#: consoles default to cp1252, which cannot encode any of the fancy ones, and a
#: progress line is not worth a ``UnicodeEncodeError``.
_UNICODE_SYMBOLS = {
    "ok": "✓",
    "failed": "✗",
    "skipped": "–",
    "dash": "—",
    "ellipsis": "…",
}
_ASCII_SYMBOLS = {
    "ok": "OK",
    "failed": "FAIL",
    "skipped": "--",
    "dash": "-",
    "ellipsis": "...",
}


def _choose_symbols() -> Dict[str, str]:
    """Pick the progress glyph set stderr can actually encode.

    Returns:
        :data:`_UNICODE_SYMBOLS` when ``sys.stderr``'s encoding round-trips every
        glyph, otherwise :data:`_ASCII_SYMBOLS`.
    """
    encoding = getattr(sys.stderr, "encoding", None) or "utf-8"
    try:
        "".join(_UNICODE_SYMBOLS.values()).encode(encoding)
    except (UnicodeEncodeError, LookupError, TypeError):
        return _ASCII_SYMBOLS
    return _UNICODE_SYMBOLS


#: Decided once, at import, so every progress line agrees with every other one.
_SYMBOLS = _choose_symbols()

#: ``True`` when the glyphs above had to be downgraded; also drives tqdm's own
#: bar characters, which are box-drawing glyphs by default.
_ASCII_ONLY = _SYMBOLS is _ASCII_SYMBOLS


def _encodable(text: str, stream: Any = None) -> str:
    """Return ``text`` with any character ``stream`` cannot encode replaced.

    Playlist and track titles are arbitrary user data: a Japanese playlist name
    on a cp1252 console would otherwise take the whole run down mid-progress --
    including in the final summary, long after the downloads succeeded.

    Args:
        text: Line about to be written.
        stream: Destination stream; defaults to ``sys.stderr``.

    Returns:
        The same string, or a lossy but printable version of it.
    """
    stream = sys.stderr if stream is None else stream
    encoding = getattr(stream, "encoding", None) or "utf-8"
    try:
        text.encode(encoding)
    except (UnicodeEncodeError, LookupError, TypeError):
        return text.encode(encoding, "replace").decode(encoding, "replace")
    return text


def _print_out(text: str) -> None:
    """Write one summary line to stdout, never crashing on the console encoding.

    Args:
        text: Line to print.
    """
    print(_encodable(text, sys.stdout))


def _print_err(text: str) -> None:
    """Write one diagnostic line to stderr, never crashing on the console encoding.

    Args:
        text: Line to print.
    """
    print(_encodable(text, sys.stderr), file=sys.stderr)


def _truncate(text: str, limit: int) -> str:
    """Shorten ``text`` to ``limit`` characters, marking the cut with an ellipsis.

    Args:
        text: String to shorten.
        limit: Maximum length of the result, including the marker.

    Returns:
        ``text`` unchanged when it already fits, else a truncated version.
    """
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 3)].rstrip() + "..."


EPILOG = """\
examples:
  # a Spotify playlist link, straight from the "Share" menu
  python3 main.py "https://open.spotify.com/playlist/EXAMPLEPLAYLISTID00000"

  # a playlist by name, plus a Spotify album URI, into a custom folder
  python3 main.py -n "example playlist name" -p spotify:album:EXAMPLEALBUMID00000000 -o ~/Music/rips

  # a whole YouTube playlist at 320 kbps with 8 workers
  python3 main.py -p "https://www.youtube.com/playlist?list=PLxxxxxxxxxxxxxxxx" -q 320 -w 8

  # a batch file (one entry per line, # comments allowed), re-fetching everything
  python3 main.py -f playlists.txt --overwrite -v

  # a "Full Album Stream" video, cut into one tagged MP3 per chapter
  python3 main.py --split-chapters -u "https://www.youtube.com/watch?v=EXAMPLEVID1"

  # keep watching playlists.txt, or serve the Spicetify extension endpoint
  python3 main.py --watch
  python3 main.py --serve

  # save a SoundCloud session once (a browser opens; you log in there)
  python3 main.py --login

  # download your own release and publish it publicly
  python3 main.py --upload -p "https://open.spotify.com/album/EXAMPLEALBUMID00000000"

  # upload a folder that is already on disk, keeping it private (-d implies --upload)
  python3 main.py -d "Songs/Example Album" --privacy private --artist "Artist Name"

  # drive the Chrome you are already logged in to, over CDP
  python3 main.py -d "Songs/Example Album" --upload --attach

  # check the live page without attaching a file or pressing Upload
  python3 main.py -d "Songs/Example Album" --upload --attach --dry-run
"""


def build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser.

    Returns:
        A configured :class:`argparse.ArgumentParser`. ``--quality`` is accepted
        as free text here and validated in :func:`main`, so the error message can
        explain the allowed range instead of listing hundreds of choices.
    """
    parser = argparse.ArgumentParser(
        prog="main.py",
        description=(
            "Download Spotify and YouTube playlists as tagged audio files (MP3 by "
            "default, see --format) with ID3 tags and embedded cover art.\n"
            "\n"
            "Inputs may be given positionally or through -p/-n/-u/-f, in any mix: a "
            "Spotify playlist or album (URL or spotify: URI), a YouTube playlist or "
            "video URL, or a plain playlist name to search for. Spotify tracks are "
            "matched against YouTube by title, artist and duration.\n"
            "\n"
            "Each playlist lands in its own folder under --output-dir, with a resume "
            "ledger alongside it, so re-running skips what is already on disk. One bad track "
            "never aborts the batch: failures are reported and written to skipped.txt.\n"
            "\n"
            "Requires ffmpeg on PATH. Spotify inputs need no credentials: the "
            "tracklist, collection name and cover art are read from Spotify's "
            "public pages. SPOTIPY_CLIENT_ID and SPOTIPY_CLIENT_SECRET are used "
            "only as a fallback, if that scrape fails."
        ),
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "inputs",
        nargs="*",
        metavar="INPUT",
        help=(
            "playlist links, Spotify URIs, video URLs or playlist names, in any mix "
            "(can be combined with -p/-n/-u/-f)"
        ),
    )
    parser.add_argument(
        "-p",
        "--playlist",
        action="append",
        dest="playlist",
        metavar="URL",
        help="a Spotify or YouTube playlist/album URL or URI; repeat for several",
    )
    parser.add_argument(
        "-n",
        "--name",
        action="append",
        dest="name",
        metavar="NAME",
        help="a playlist name to search for; repeat for several",
    )
    parser.add_argument(
        "-u",
        "--url",
        action="append",
        dest="url",
        metavar="URL",
        help="a single video URL; repeat for several",
    )
    parser.add_argument(
        "-f",
        "--file",
        dest="file",
        metavar="PATH",
        help="text file with one entry per line (blank lines and # comments ignored)",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        dest="output_dir",
        default="./%s" % DEFAULT_OUTPUT_DIR,
        metavar="DIR",
        help="root directory for the playlist folders (default: ./%s)" % DEFAULT_OUTPUT_DIR,
    )
    parser.add_argument(
        "-q",
        "--quality",
        dest="quality",
        default=BITRATE_DEFAULT,
        metavar="KBPS",
        help="MP3 bitrate in kbps, %d-%d (default: %s)" % (QUALITY_MIN, QUALITY_MAX, BITRATE_DEFAULT),
    )
    parser.add_argument(
        "-w",
        "--workers",
        dest="workers",
        type=int,
        default=MAX_WORKERS_DEFAULT,
        metavar="N",
        help="parallel downloads per playlist (default: %d)" % MAX_WORKERS_DEFAULT,
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="re-download tracks that are already on disk (default: skip them)",
    )
    parser.add_argument(
        "--format",
        dest="audio_format",
        choices=AUDIO_FORMATS,
        default="mp3",
        help=(
            "audio format to save (default: %(default)s). opus and m4a are what "
            "YouTube already serves, so they are copied rather than re-encoded "
            "-- best fidelity, and -q is ignored for them"
        ),
    )
    parser.add_argument(
        "--split-chapters",
        action="store_true",
        help="split a full-album stream into one MP3 per chapter",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="watch playlists.txt and download new entries as they appear",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="run the localhost endpoint used by the Spicetify extension",
    )
    parser.add_argument(
        "--login",
        action="store_true",
        help=(
            "open SoundCloud in a browser, wait for you to log in, save the session "
            "to --state-file and exit (all other flags are ignored)"
        ),
    )
    parser.add_argument(
        "--upload",
        action="store_true",
        help=(
            "publish each downloaded album to your own SoundCloud account once it "
            "has finished; this is for uploading your own releases"
        ),
    )
    parser.add_argument(
        "-d",
        "--dir",
        dest="dir",
        metavar="DIR",
        help=(
            "upload an album folder that is already on disk; implies --upload and "
            "downloads nothing (cannot be combined with -p/-n/-u/-f or positional inputs)"
        ),
    )
    parser.add_argument(
        "--privacy",
        dest="privacy",
        choices=("private", "public"),
        default="public",
        help="visibility of the uploaded tracks (default: public)",
    )
    parser.add_argument(
        "--artist",
        dest="artist",
        metavar="NAME",
        help=(
            "artist to credit on every uploaded track (default: the album's own "
            "tag); SoundCloud prefills this field with your account name"
        ),
    )
    parser.add_argument(
        "--attach",
        action="store_true",
        help=(
            "drive the Chrome already running with --remote-debugging-port instead "
            "of launching one; uses that browser's own profile and session"
        ),
    )
    parser.add_argument(
        "--attach-endpoint",
        dest="attach_endpoint",
        default=ATTACH_ENDPOINT_DEFAULT,
        metavar="URL",
        help="CDP endpoint for --attach (default: %s)" % ATTACH_ENDPOINT_DEFAULT,
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="run the browser visibly; try this when a headless run gets challenged",
    )
    parser.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help=(
            "with --upload: open the live upload page and report what would go "
            "up, attaching no files and pressing nothing"
        ),
    )
    parser.add_argument(
        "--state-file",
        dest="state_file",
        default=STATE_FILE_DEFAULT,
        metavar="PATH",
        help="saved SoundCloud session written by --login (default: %s)" % STATE_FILE_DEFAULT,
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="verbose output, including yt-dlp's own diagnostics (disables the progress bar)",
    )
    return parser


def _configure_logging(verbose: bool) -> None:
    """Route all log records to stderr so stdout carries only the summary.

    Args:
        verbose: When ``True``, log at DEBUG with logger names and levels shown;
            otherwise log bare messages at INFO.
    """
    # Log records carry playlist and track names straight from the network, and
    # logging has no per-record encoding guard: on a cp1252 console an unlucky
    # title would print "--- Logging error ---" and a traceback. Degrading the
    # stream's error handler once fixes every record at the source.
    try:
        sys.stderr.reconfigure(errors="backslashreplace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError, OSError):
        pass

    handler = logging.StreamHandler(stream=sys.stderr)
    if verbose:
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        level = logging.DEBUG
    else:
        handler.setFormatter(logging.Formatter("%(message)s"))
        level = logging.INFO

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)


def _parse_quality(value: Any) -> str:
    """Validate the ``--quality`` argument.

    Args:
        value: Raw ``--quality`` string.

    Returns:
        The bitrate as a normalised string, ready for ``pipeline.run_job``.

    Raises:
        ValueError: If it is not a whole number, or falls outside
            :data:`QUALITY_MIN`..:data:`QUALITY_MAX`.
    """
    text = str(value).strip().lower()
    if text.endswith("k"):
        text = text[:-1].strip()
    try:
        kbps = int(text)
    except (TypeError, ValueError):
        raise ValueError(
            "--quality must be a whole number of kbps between %d and %d "
            "(for example 128, 192 or 320), not %r" % (QUALITY_MIN, QUALITY_MAX, value)
        )
    if not QUALITY_MIN <= kbps <= QUALITY_MAX:
        raise ValueError(
            "--quality must be between %d and %d kbps, got %d"
            % (QUALITY_MIN, QUALITY_MAX, kbps)
        )
    return str(kbps)


def _read_entries_file(path: str) -> List[str]:
    """Read one input per line from a text file.

    Blank lines and ``#`` comments are ignored, wrapping quotes are stripped and
    order-preserving de-duplication is applied. Anything else passes through
    verbatim: this CLI accepts playlist *names* and ``spotify:`` URIs, so a
    URL-only filter would silently discard valid input.

    Args:
        path: Path to the list file.

    Returns:
        The de-duplicated entries, in file order.

    Raises:
        FileNotFoundError: If the file does not exist.
        OSError: If the file exists but cannot be read.
    """
    from pathlib import Path

    file_path = Path(path).expanduser()
    if not file_path.is_file():
        raise FileNotFoundError("Input file not found: %s" % file_path)

    entries: List[str] = []
    seen = set()
    text = file_path.read_text(encoding="utf-8", errors="replace")
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if len(line) >= 2 and line[0] == line[-1] and line[0] in ("'", '"'):
            line = line[1:-1].strip()
        if not line or line in seen:
            continue
        seen.add(line)
        entries.append(line)

    logger.debug("read %d entry(ies) from %s", len(entries), file_path)
    return entries


def _collect_inputs(args: argparse.Namespace) -> List[str]:
    """Merge every input source into one ordered, de-duplicated list.

    Positional arguments come first, then ``-p``, ``-n``, ``-u`` and finally the
    contents of ``-f``, each in the order the user gave them.

    Args:
        args: Parsed command-line arguments.

    Returns:
        The merged list of raw input strings.

    Raises:
        FileNotFoundError: If ``-f/--file`` points at a missing file.
        OSError: If ``-f/--file`` cannot be read.
    """
    candidates: List[str] = []
    for group in (args.inputs, args.playlist, args.name, args.url):
        candidates.extend(group or [])
    if args.file:
        candidates.extend(_read_entries_file(args.file))

    merged: List[str] = []
    seen = set()
    for candidate in candidates:
        cleaned = (candidate or "").strip()
        if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in ("'", '"'):
            cleaned = cleaned[1:-1].strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        merged.append(cleaned)
    return merged


class _ProgressReporter:
    """``on_progress`` callback that drives one tqdm bar for one playlist.

    ``pipeline.run_job`` calls this from the **main** thread once per track,
    including skips, so no locking is needed. Per-track lines are emitted through
    ``tqdm.write`` (pinned to stderr) so they scroll above the bar rather than
    shredding it.

    The bar is suppressed in three cases: ``--verbose`` (yt-dlp's own output
    interleaves with it and makes it useless), stderr not being a TTY (piped or
    CI output should stay free of escape codes), and tqdm being unavailable.
    """

    def __init__(self, job: PlaylistJob, verbose: bool = False) -> None:
        """Open a bar for ``job``.

        Args:
            job: The playlist about to run; supplies the total and the ``desc``.
            verbose: When ``True``, print plain log lines and no bar at all.
        """
        self._total = len(job.tracks)
        # DownloadResult only carries a title; the "Artist - Title" label lives on
        # the Track. Results arrive out of order, so map them back by query.
        self._labels = {
            track.query: track.display() for track in job.tracks if track.query
        }
        self._bar = None
        if verbose or self._total == 0:
            return
        try:
            from tqdm import tqdm
        except ImportError:  # pragma: no cover - tqdm is a declared dependency
            logger.debug("tqdm unavailable; falling back to plain progress lines")
            return
        self._bar = tqdm(
            total=self._total,
            desc=_encodable(_truncate(job.name, DESC_WIDTH)),
            unit="track",
            file=sys.stderr,
            disable=not sys.stderr.isatty(),
            ascii=_ASCII_ONLY,
            dynamic_ncols=True,
            leave=True,
        )

    def _write(self, line: str) -> None:
        """Emit one progress line without corrupting the bar.

        Args:
            line: Already-formatted line.
        """
        line = _encodable(line)
        if self._bar is not None:
            # tqdm.write defaults to stdout, which is reserved for the summary.
            self._bar.write(line, file=sys.stderr)
        else:
            logger.info("%s", line)

    def _label(self, result: DownloadResult) -> str:
        """Return the best human-readable name for a result.

        Args:
            result: The finished result.

        Returns:
            ``Artist - Title`` when the originating track is known, else the
            resolved title, else the raw query.
        """
        return self._labels.get(result.url) or result.title or result.url

    def __call__(self, result: DownloadResult, done: int, total: int) -> None:
        """Report one finished track. Signature required by ``ProgressCallback``.

        Args:
            result: Outcome of the track.
            done: How many tracks of this job have finished, 1-based.
            total: Track count of this job.
        """
        index = "[%d/%d]" % (done, total)
        if result.status == STATUS_OK:
            line = "%s %s %s" % (index, _SYMBOLS["ok"], self._label(result))
        elif result.status == STATUS_SKIPPED:
            line = "%s %s %s (%s)" % (
                index,
                _SYMBOLS["skipped"],
                self._label(result),
                result.reason or "already downloaded",
            )
        else:
            line = "%s %s %s %s %s" % (
                index,
                _SYMBOLS["failed"],
                result.url,
                _SYMBOLS["dash"],
                result.reason or "unknown error",
            )
        self._write(line)
        if self._bar is not None:
            self._bar.update(1)

    def close(self) -> None:
        """Close the bar, if one was opened. Safe to call more than once."""
        if self._bar is not None:
            self._bar.close()
            self._bar = None


class _UploadReporter:
    """One tqdm bar over the album folders being published.

    Same discipline as :class:`_ProgressReporter`: every line goes through
    ``tqdm.write`` pinned to stderr so it scrolls above the bar instead of
    shredding it, and the bar is suppressed under ``--verbose``, off a TTY, or
    when tqdm is missing. Uploads are sequential -- one browser page, one set at
    a time -- so no locking is needed here either.
    """

    def __init__(self, total: int, verbose: bool = False) -> None:
        """Open a bar for ``total`` album folders.

        Args:
            total: Number of folders about to be uploaded.
            verbose: When ``True``, print plain log lines and no bar at all.
        """
        self._total = total
        self._bar = None
        if verbose or total == 0:
            return
        try:
            from tqdm import tqdm
        except ImportError:  # pragma: no cover - tqdm is a declared dependency
            logger.debug("tqdm unavailable; falling back to plain progress lines")
            return
        self._bar = tqdm(
            total=total,
            desc="uploading",
            unit="set",
            file=sys.stderr,
            disable=not sys.stderr.isatty(),
            ascii=_ASCII_ONLY,
            dynamic_ncols=True,
            leave=True,
        )

    def _write(self, line: str) -> None:
        """Emit one progress line without corrupting the bar."""
        line = _encodable(line)
        if self._bar is not None:
            # tqdm.write defaults to stdout, which is reserved for the summary.
            self._bar.write(line, file=sys.stderr)
        else:
            logger.info("%s", line)

    def _finish(self, index: int, text: str) -> None:
        """Write one finished-set line and advance the bar by one."""
        self._write("[%d/%d] %s" % (index, self._total, text))
        if self._bar is not None:
            self._bar.update(1)

    def note(self, line: str) -> None:
        """Write one line that is not tied to a set, e.g. a selector report."""
        self._write(line)

    def begin(self, title: str) -> None:
        """Name the set now going up, so a long upload is not silent.

        Args:
            title: Album title.
        """
        if self._bar is not None:
            self._bar.set_description(_encodable(_truncate(title, DESC_WIDTH)))

    def ok(self, index: int, title: str, detail: Optional[str] = None) -> None:
        """Report a set that finished.

        Args:
            index: 1-based position in the run.
            title: Album title.
            detail: Set URL, or a note such as ``"dry run"``.
        """
        suffix = " %s %s" % (_SYMBOLS["dash"], detail) if detail else ""
        self._finish(
            index,
            "Uploading %s%s done%s"
            % (_truncate(title, DESC_WIDTH), _SYMBOLS["ellipsis"], suffix),
        )

    def skipped(self, index: int, title: str, url: Optional[str] = None) -> None:
        """Report a set the ledger says is already on SoundCloud."""
        self._finish(
            index,
            "%s %s (already uploaded: %s)"
            % (_SYMBOLS["skipped"], _truncate(title, DESC_WIDTH), url or "no URL recorded"),
        )

    def failed(self, index: int, title: str, reason: str) -> None:
        """Report a set that did not make it."""
        self._finish(
            index,
            "%s %s %s %s"
            % (_SYMBOLS["failed"], _truncate(title, DESC_WIDTH), _SYMBOLS["dash"], reason),
        )

    def close(self) -> None:
        """Close the bar, if one was opened. Safe to call more than once."""
        if self._bar is not None:
            self._bar.close()
            self._bar = None


def _job_folder(output_dir, name: str):
    """Work out the folder ``pipeline.run_job`` will use for a playlist.

    Mirrors the pipeline's own naming so the summary can name the destination
    without depending on any track having succeeded.

    Args:
        output_dir: Root download directory.
        name: Playlist name.

    Returns:
        The :class:`~pathlib.Path` of the playlist folder.
    """
    from pathlib import Path

    from downloader import sanitize_filename
    from pipeline import FALLBACK_PLAYLIST_NAME

    raw = (name or "").strip()
    folder = sanitize_filename(raw) if raw else ""
    return Path(output_dir) / (folder or FALLBACK_PLAYLIST_NAME)


def _call_filtered(func: Callable[..., Any], **kwargs: Any) -> Any:
    """Call ``func`` with only the keyword arguments it actually accepts.

    ``watcher`` and ``server`` are separate modules whose signatures may not take
    every knob this CLI exposes. Filtering keeps ``--watch``/``--serve`` working
    instead of dying on an unexpected keyword.

    Args:
        func: The delegate to call.
        **kwargs: Candidate keyword arguments.

    Returns:
        Whatever ``func`` returns.
    """
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):  # pragma: no cover - builtins/C callables
        return func()
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in signature.parameters.values()):
        return func(**kwargs)
    accepted = {
        name
        for name, param in signature.parameters.items()
        if param.kind
        in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    }
    return func(**{k: v for k, v in kwargs.items() if k in accepted})


def _delegate(mode: str, args: argparse.Namespace, quality: str) -> int:
    """Hand control to ``watcher.watch_loop`` or ``server.serve_forever``.

    Both modules are imported lazily and are optional: a missing one is a clear
    message, not a traceback.

    Args:
        mode: ``"watch"`` or ``"serve"``.
        args: Parsed command-line arguments.
        quality: Validated bitrate.

    Returns:
        The delegate's exit code, ``0`` if it returned nothing, ``1`` if the
        module is missing or blew up, ``130`` on Ctrl-C.
    """
    if mode == "watch":
        module_name, func_name, flag = "watcher", "watch_loop", "--watch"
    else:
        module_name, func_name, flag = "server", "serve_forever", "--serve"

    try:
        if mode == "watch":
            from watcher import watch_loop as delegate
        else:
            from server import serve_forever as delegate
    except ImportError as exc:
        _print_err(
            "Error: %s needs %s.py, which could not be imported (%s)."
            % (flag, module_name, exc)
        )
        _print_err(
            "Make sure %s.py is next to main.py and exports %s(), or run without %s."
            % (module_name, func_name, flag)
        )
        return EXIT_FAILURE

    try:
        outcome = _call_filtered(
            delegate,
            output_dir=args.output_dir,
            quality=quality,
            workers=args.workers,
            overwrite=args.overwrite,
            verbose=args.verbose,
        )
    except KeyboardInterrupt:
        _print_err("Interrupted.")
        return EXIT_INTERRUPTED
    except Exception as exc:  # noqa: BLE001 - a delegate crash is our exit code
        logger.error("%s failed: %s", flag, exc, exc_info=args.verbose)
        _print_err("Error: %s failed: %s" % (flag, exc))
        return EXIT_FAILURE

    return outcome if isinstance(outcome, int) else EXIT_OK


def _print_summary(
    results: Sequence[DownloadResult],
    job_lines: Sequence[Tuple[str, int, Any]],
    output_dir,
) -> int:
    """Print the final report to stdout and write ``skipped.txt`` if needed.

    Args:
        results: Every result of the run, across all jobs.
        job_lines: ``(playlist name, track count, folder)`` per job, in run order.
        output_dir: Root download directory, where ``skipped.txt`` is written.

    Returns:
        The number of failed tracks.
    """
    from downloader import write_failed_log

    downloaded = sum(1 for r in results if r.status == STATUS_OK)
    skipped = sum(1 for r in results if r.status == STATUS_SKIPPED)
    failed = sum(1 for r in results if r.status == STATUS_FAILED)

    _print_out("Done: %d downloaded, %d skipped, %d failed" % (downloaded, skipped, failed))
    for name, count, folder in job_lines:
        _print_out("%s: %d tracks -> %s" % (name, count, folder))

    if failed:
        try:
            log_path = write_failed_log(results, output_dir)
        except OSError as exc:  # write_failed_log swallows these, but belt and braces
            logger.warning("could not write the failure log: %s", exc)
            log_path = None
        if log_path is not None:
            _print_out("Failed tracks written to: %s" % log_path)
    return failed


def _soundcloud(name: str):
    """Import an upload-stage module, or explain why it is unavailable.

    ``browser_engine`` and ``uploader`` pull in Playwright, which a user who only
    ever downloads has no reason to install, so they are imported here rather
    than at module scope.

    Args:
        name: ``"browser_engine"`` or ``"uploader"``.

    Returns:
        The imported module, or ``None`` after printing what went wrong.
    """
    import importlib

    try:
        return importlib.import_module(name)
    except ImportError as exc:
        _print_err(
            "Error: uploading needs %s.py, which could not be imported (%s)."
            % (name, exc)
        )
        _print_err(
            "Make sure %s.py is next to main.py, then run 'pip install -r "
            "requirements.txt' and 'playwright install chromium'." % name
        )
        return None


def _login_hint() -> None:
    """Tell the user how to create the session file the upload stage needs."""
    _print_err("Run 'python3 main.py --login' first, then try again.")


def _run_login(args: argparse.Namespace) -> int:
    """Open SoundCloud's own login page and save the resulting session.

    There is deliberately no ``--password`` flag and no prompt: Playwright shows
    SoundCloud's real page, the user types into that, and all this program ever
    touches is the session state it leaves behind. A credential we never hold is
    one we cannot log, leak, or leave in a shell history.

    Args:
        args: Parsed command-line arguments; only ``--state-file`` is used.

    Returns:
        ``0`` once the session file exists, ``1`` if the login did not complete,
        ``130`` on Ctrl-C.
    """
    engine = _soundcloud("browser_engine")
    if engine is None:
        return EXIT_FAILURE

    _print_err(
        "Opening SoundCloud in a browser. Log in there; waiting up to %d seconds."
        % LOGIN_TIMEOUT
    )
    try:
        state_path = _call_filtered(
            engine.login, state_path=args.state_file, timeout=LOGIN_TIMEOUT
        )
    except KeyboardInterrupt:
        _print_err("Interrupted.")
        return EXIT_INTERRUPTED
    except engine.NotLoggedInError as exc:
        _print_err("Error: login did not complete: %s" % exc)
        return EXIT_FAILURE
    except Exception as exc:  # noqa: BLE001 - a login crash is our exit code
        logger.error("login failed: %s", exc, exc_info=args.verbose)
        _print_err("Error: login failed: %s" % exc)
        return EXIT_FAILURE

    _print_out("Session saved to: %s" % (state_path or args.state_file))
    _print_err(
        "Treat that file as a password: anyone holding it is logged in as you."
    )
    return EXIT_OK


def _check_selectors(uploader, page, reporter: _UploadReporter) -> int:
    """Verify the uploader's selectors against the live page.

    Args:
        uploader: The imported ``uploader`` module.
        page: An open Playwright page.
        reporter: Progress reporter, used so the report respects the bar.

    Returns:
        How many selectors that should have matched did not; ``1`` if the check
        itself blew up. Only the keys in ``uploader.PREATTACH_KEYS`` count: the
        metadata form does not exist until files are attached, and attaching
        them is the upload, so the rest legitimately read missing here.
    """
    try:
        found = uploader.check_selectors(page) or {}
    except Exception as exc:  # noqa: BLE001 - the check is the point of --dry-run
        reporter.note("%s selector check failed: %s" % (_SYMBOLS["failed"], exc))
        return 1

    preattach = getattr(uploader, "PREATTACH_KEYS", frozenset(found))
    missing = sorted(name for name, present in found.items() if not present)
    blocking = [name for name in missing if name in preattach]
    for name in blocking:
        reporter.note("%s selector missing: %s" % (_SYMBOLS["failed"], name))
    reporter.note(
        "%s %d/%d selectors found (%d only exist once files are attached)"
        % (
            _SYMBOLS["ok"],
            len(found) - len(missing),
            len(found),
            len(missing) - len(blocking),
        )
    )
    return len(blocking)


def _upload_albums(folders: Sequence[Any], args: argparse.Namespace) -> int:
    """Publish each album folder to SoundCloud and print the upload summary.

    One browser session covers the whole batch. The ledger in each folder is
    consulted first, so an interrupted run resumes instead of leaving the user
    with a duplicate set to delete by hand.

    Args:
        folders: Album folders, in the order they were produced.
        args: Parsed command-line arguments.

    Returns:
        How many folders failed. ``len(folders)`` when the session could not be
        opened at all, since in that case nothing went up.
    """
    from upload_ledger import UploadLedger, set_key, track_key

    engine = _soundcloud("browser_engine")
    uploader = _soundcloud("uploader")
    if engine is None or uploader is None:
        return len(folders)

    uploaded: List[Tuple[str, Optional[str]]] = []
    skipped: List[Tuple[str, Optional[str]]] = []
    failures: List[Tuple[str, str]] = []

    reporter = _UploadReporter(len(folders), verbose=args.verbose)
    try:
        if getattr(args, "attach", False):
            # The attached browser is the user's own: it carries its own
            # session, so --state-file and --headed have nothing to say about
            # it, and asset blocking stays off in someone's real window.
            session = _call_filtered(
                engine.attached_browser,
                endpoint=args.attach_endpoint,
                block_assets=False,
            )
        else:
            session = _call_filtered(
                engine.browser,
                state_path=args.state_file,
                headless=not args.headed,
                block_assets=True,
            )
        with session as page:
            if args.dry_run and _check_selectors(uploader, page, reporter):
                # A missing selector is exactly what --dry-run exists to find;
                # going on would only report the same breakage once per folder.
                _print_out("Dry run: selector check failed, nothing uploaded")
                return len(folders)

            for index, folder in enumerate(folders, 1):
                try:
                    album = uploader.scan_album(folder)
                except Exception as exc:  # noqa: BLE001 - one bad folder != dead batch
                    logger.error("could not read %s: %s", folder, exc, exc_info=args.verbose)
                    reporter.failed(index, folder.name, "could not read the album: %s" % exc)
                    failures.append((folder.name, str(exc)))
                    continue

                title = getattr(album, "title", None) or folder.name
                ledger = UploadLedger(folder)
                key = set_key(title)
                recorded = ledger.get(key)
                if recorded is not None:
                    url = recorded.get("set_url")
                    reporter.skipped(index, title, url)
                    skipped.append((title, url))
                    continue

                reporter.begin(title)
                try:
                    if not args.dry_run:
                        # Transcoding is minutes of CPU and a second copy of the
                        # album on disk. A dry run attaches no files, so it has
                        # no use for either.
                        album = uploader.prepare_for_upload(album)
                    # privacy and dry_run are passed explicitly, never through
                    # _call_filtered: silently dropping dry_run would publish
                    # what the user asked us only to rehearse, and silently
                    # dropping privacy would fall back to upload_album's own
                    # default instead of the visibility asked for. upload_album
                    # then refuses to submit at all unless SoundCloud's own
                    # privacy select reads back what was asked for.
                    set_url = uploader.upload_album(
                        page,
                        album,
                        privacy=args.privacy,
                        dry_run=args.dry_run,
                        artist=args.artist,
                    )
                except engine.NotLoggedInError:
                    raise
                except (uploader.UploadError, uploader.SelectorError) as exc:
                    logger.error("upload of %s failed: %s", title, exc, exc_info=args.verbose)
                    reporter.failed(index, title, str(exc))
                    failures.append((title, str(exc)))
                    continue
                except Exception as exc:  # noqa: BLE001 - one bad set != dead batch
                    logger.error("upload of %s failed: %s", title, exc, exc_info=args.verbose)
                    reporter.failed(index, title, "unexpected error: %s" % exc)
                    failures.append((title, str(exc)))
                    continue

                if args.dry_run:
                    # Nothing was published, so nothing is written -- not even an
                    # empty ledger. A dry run leaves the folder as it found it,
                    # and the real run afterwards must not skip the set as done.
                    reporter.ok(index, title, "dry run")
                    uploaded.append((title, None))
                    continue

                with ledger:
                    ledger.record(key, set_url=set_url)
                    # Per-track keys too, so a future partial resume can tell
                    # which files of a half-finished set already landed.
                    for track in getattr(album, "tracks", None) or []:
                        source = getattr(track, "upload_path", None) or getattr(track, "path", None)
                        if source is not None:
                            ledger.record(track_key(str(source)), set_url=set_url)
                reporter.ok(index, title, set_url)
                uploaded.append((title, set_url))
    except engine.NotLoggedInError as exc:
        reporter.close()
        # The module's own message says the same thing; one hint is enough.
        logger.debug("no session: %s", exc)
        _print_err("Error: no usable SoundCloud session in %s." % args.state_file)
        _login_hint()
        return len(folders) - len(uploaded) - len(skipped)
    except Exception as exc:  # noqa: BLE001 - a browser crash is our exit code
        reporter.close()
        logger.error("upload session failed: %s", exc, exc_info=args.verbose)
        _print_err("Error: could not run the upload session: %s" % exc)
        return len(folders) - len(uploaded) - len(skipped)
    finally:
        reporter.close()

    verb = "Dry run" if args.dry_run else "Uploaded"
    _print_out(
        "%s: %d set(s), %d skipped, %d failed"
        % (verb, len(uploaded), len(skipped), len(failures))
    )
    for title, url in uploaded:
        _print_out("%s -> %s" % (title, url or ("dry run" if args.dry_run else "no URL returned")))
    for title, url in skipped:
        _print_out("%s -> %s (already uploaded)" % (title, url or "no URL recorded"))
    for title, reason in failures:
        _print_out("%s: failed - %s" % (title, reason))
    return len(failures)


def _upload_only(args: argparse.Namespace) -> int:
    """Run the upload stage alone, for ``--dir``.

    Args:
        args: Parsed command-line arguments.

    Returns:
        ``0`` when the folder was uploaded or was already recorded, ``1`` on any
        failure, ``130`` on Ctrl-C.
    """
    from pathlib import Path

    folder = Path(args.dir).expanduser()
    if not folder.is_dir():
        _print_err("Error: not a folder: %s" % folder)
        return EXIT_FAILURE
    try:
        failed = _upload_albums([folder], args)
    except KeyboardInterrupt:
        _print_err("Interrupted.")
        return EXIT_INTERRUPTED
    return EXIT_FAILURE if failed else EXIT_OK


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the CLI.

    Args:
        argv: Argument list to parse; defaults to ``sys.argv[1:]``.

    Returns:
        ``0`` when everything downloaded or was already present, ``1`` when any
        track failed or a setup step failed, ``2`` on a usage error, ``130`` when
        interrupted.
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    _configure_logging(args.verbose)
    _silence_third_party_warnings()

    # --login writes the session file and nothing else, so it runs before any
    # other flag is even validated.
    if args.login:
        return _run_login(args)

    if args.dir and (args.inputs or args.playlist or args.name or args.url or args.file):
        parser.error(
            "--dir uploads a folder that is already on disk, so there is nothing to "
            "download; it cannot be combined with -p/-n/-u/-f or positional inputs"
        )

    # --dir means "upload this, download nothing".
    upload = args.upload or bool(args.dir)

    try:
        quality = _parse_quality(args.quality)
    except ValueError as exc:
        parser.error(str(exc))  # exits with EXIT_USAGE
        return EXIT_USAGE  # pragma: no cover - parser.error never returns

    if args.watch and args.serve:
        parser.error("--watch and --serve cannot be used together")
    if args.watch:
        return _delegate("watch", args, quality)
    if args.serve:
        return _delegate("serve", args, quality)

    if args.dir:
        return _upload_only(args)

    try:
        inputs = _collect_inputs(args)
    except (FileNotFoundError, OSError) as exc:
        _print_err("Error: %s" % exc)
        return EXIT_FAILURE

    if not inputs:
        _print_err(
            "Error: nothing to download. Give a playlist link, a Spotify URI or a "
            "playlist name -- positionally or with -p/-n/-u/-f.\n"
        )
        parser.print_usage(sys.stderr)
        return EXIT_USAGE

    from downloader import FFmpegNotFoundError, check_ffmpeg, ensure_output_dir

    try:
        check_ffmpeg()
    except FFmpegNotFoundError as exc:
        _print_err(str(exc))
        return EXIT_FAILURE

    try:
        output_dir = ensure_output_dir(args.output_dir)
    except (NotADirectoryError, OSError) as exc:
        _print_err("Error: %s" % exc)
        return EXIT_FAILURE

    from playlist_parser import SpotifyCredentialsError

    results: List[DownloadResult] = []
    job_lines: List[Tuple[str, int, Any]] = []
    upload_folders: List[Any] = []
    interrupted = False
    setup_error = False

    try:
        for text in inputs:
            # Resolving is imported per input so a patched/reloaded module is
            # honoured, and so --help never pays for spotipy's import cost.
            from playlist_parser import resolve_input
            from pipeline import run_job

            try:
                job = resolve_input(text)
            except SpotifyCredentialsError as exc:
                # A missing API key is a setup problem, not a bad input: every
                # other Spotify entry would fail the same way. Stop and explain.
                _print_err(str(exc))
                setup_error = True
                break
            except Exception as exc:  # noqa: BLE001 - one bad input != dead run
                logger.error("could not resolve %r: %s", text, exc, exc_info=args.verbose)
                results.append(
                    DownloadResult.failed(text, "could not resolve input: %s" % exc)
                )
                continue

            if not job.tracks:
                logger.warning("no tracks found for %r", text)
                results.append(DownloadResult.failed(text, "no tracks found"))
                continue

            reporter = _ProgressReporter(job, verbose=args.verbose)
            try:
                job_results = run_job(
                    job,
                    output_dir,
                    quality=quality,
                    workers=args.workers,
                    overwrite=args.overwrite,
                    verbose=args.verbose,
                    split_chapters=args.split_chapters,
            audio_format=args.audio_format,
                    on_progress=reporter,
                )
            finally:
                reporter.close()

            results.extend(job_results)
            job_lines.append((job.name, len(job_results), _job_folder(output_dir, job.name)))

            if upload:
                # Ask the files where they landed rather than re-deriving the
                # folder name: the pipeline owns that naming, collisions and all.
                folder = next(
                    (r.path.parent for r in job_results if r.succeeded and r.path), None
                )
                if folder is not None and folder not in upload_folders:
                    upload_folders.append(folder)

            # run_job catches Ctrl-C itself and returns only what finished, so a
            # short result list is the only signal that the user bailed out.
            if len(job_results) < len(job.tracks):
                interrupted = True
                _print_err("Interrupted.")
                break
    except KeyboardInterrupt:
        interrupted = True
        _print_err("Interrupted.")

    # The report is written even on a partial run: what failed before the Ctrl-C
    # is exactly what the user wants to retry.
    failed = _print_summary(results, job_lines, output_dir)

    upload_failed = 0
    if upload and not interrupted and not setup_error:
        if upload_folders:
            try:
                upload_failed = _upload_albums(upload_folders, args)
            except KeyboardInterrupt:
                interrupted = True
                _print_err("Interrupted.")
        else:
            _print_err("Nothing to upload: no album folder was produced.")

    if interrupted:
        return EXIT_INTERRUPTED
    if setup_error or failed or upload_failed:
        return EXIT_FAILURE
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
