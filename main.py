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
import importlib
import logging
import sys
import warnings
from dataclasses import dataclass, field
from typing import Any, List, Optional, Sequence, Tuple


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
# Everything below is either dependency-free (models, the cli_* modules) or
# imported lazily inside a function, so this call really does get there first.
_silence_third_party_warnings()

from cli_args import (  # noqa: E402 - deliberate: see _silence_third_party_warnings
    build_parser,
    parse_quality,
)
from cli_console import (  # noqa: E402
    EXIT_FAILURE,
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_USAGE,
    SYMBOLS,
    configure_logging,
    print_err,
    print_out,
)
from cli_progress import ProgressReporter  # noqa: E402
from models import (  # noqa: E402
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




def _unquote(text: str) -> str:
    """Strip one matching pair of wrapping quotes, as a shell would have."""
    cleaned = (text or "").strip()
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in ("'", '"'):
        cleaned = cleaned[1:-1].strip()
    return cleaned


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
        line = _unquote(line)
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
        cleaned = _unquote(candidate)
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        merged.append(cleaned)
    return merged




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



#: ``--watch``/``--serve`` targets: module, callable, and the flag that asks
#: for it. Both modules are optional, so a missing one must be a message.
_DELEGATES = {
    "watch": ("watcher", "watch_loop", "--watch"),
    "serve": ("server", "serve_forever", "--serve"),
}


def _delegate(mode: str, args: argparse.Namespace, quality: str) -> int:
    """Hand control to ``watcher.watch_loop`` or ``server.serve_forever``."""
    module_name, func_name, flag = _DELEGATES[mode]

    try:
        delegate = getattr(importlib.import_module(module_name), func_name)
    except (ImportError, AttributeError) as exc:
        print_err(
            "Error: %s needs %s.py, which could not be imported (%s)."
            % (flag, module_name, exc)
        )
        print_err(
            "Make sure %s.py is next to main.py and exports %s(), or run without %s."
            % (module_name, func_name, flag)
        )
        return EXIT_FAILURE

    try:
        outcome = delegate(
            output_dir=args.output_dir,
            quality=quality,
            workers=args.workers,
            overwrite=args.overwrite,
            verbose=args.verbose,
        )
    except KeyboardInterrupt:
        print_err("Interrupted.")
        return EXIT_INTERRUPTED
    except Exception as exc:  # noqa: BLE001 - a delegate crash is our exit code
        logger.error("%s failed: %s", flag, exc, exc_info=args.verbose)
        print_err("Error: %s failed: %s" % (flag, exc))
        return EXIT_FAILURE

    return outcome if isinstance(outcome, int) else EXIT_OK


def _write_failure_log(results: Sequence[DownloadResult], output_dir) -> Optional[Any]:
    """Write ``skipped.txt``, returning its path or ``None``."""
    from downloader import write_failed_log

    try:
        return write_failed_log(results, output_dir)
    except OSError as exc:  # write_failed_log swallows these, but belt and braces
        logger.warning("could not write the failure log: %s", exc)
        return None


def _print_summary(
    results: Sequence[DownloadResult],
    job_lines: Sequence[Tuple[str, int, Any]],
    output_dir,
) -> int:
    """Print the final report to stdout and return the failed-track count."""
    downloaded = sum(1 for r in results if r.status == STATUS_OK)
    skipped = sum(1 for r in results if r.status == STATUS_SKIPPED)
    failed = sum(1 for r in results if r.status == STATUS_FAILED)

    print_out("Done: %d downloaded, %d skipped, %d failed" % (downloaded, skipped, failed))
    for name, count, folder in job_lines:
        print_out("%s: %d tracks -> %s" % (name, count, folder))

    if failed:
        log_path = _write_failure_log(results, output_dir)
        if log_path is not None:
            print_out("Failed tracks written to: %s" % log_path)
    return failed


@dataclass
class _DownloadRun:
    """Everything the download pass produced, including how it ended."""

    results: List[DownloadResult] = field(default_factory=list)
    job_lines: List[Tuple[str, int, Any]] = field(default_factory=list)
    folders: List[Any] = field(default_factory=list)
    interrupted: bool = False
    #: A problem that would break every remaining input, not just this one.
    setup_error: bool = False


def _download_job(job: PlaylistJob, args: argparse.Namespace, quality: str, output_dir):
    """Download one resolved playlist, with a progress bar around it."""
    # Imported per job so a patched or reloaded module is honoured, and so
    # --help never pays for yt-dlp's import cost.
    from pipeline import run_job

    reporter = ProgressReporter(job, verbose=args.verbose)
    try:
        return run_job(
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


def _run_downloads(
    inputs: Sequence[str],
    args: argparse.Namespace,
    quality: str,
    output_dir,
    upload: bool,
) -> _DownloadRun:
    """Resolve and download every input, in order."""
    from playlist_parser import SpotifyCredentialsError

    run = _DownloadRun()
    try:
        for text in inputs:
            # Imported per input for the same reason as run_job above.
            from playlist_parser import resolve_input

            try:
                job = resolve_input(text)
            except SpotifyCredentialsError as exc:
                # A missing API key is a setup problem, not a bad input: every
                # other Spotify entry would fail the same way. Stop and explain.
                print_err(str(exc))
                run.setup_error = True
                break
            except Exception as exc:  # noqa: BLE001 - one bad input != dead run
                logger.error("could not resolve %r: %s", text, exc, exc_info=args.verbose)
                run.results.append(
                    DownloadResult.failed(text, "could not resolve input: %s" % exc)
                )
                continue

            if not job.tracks:
                logger.warning("no tracks found for %r", text)
                run.results.append(DownloadResult.failed(text, "no tracks found"))
                continue

            job_results = _download_job(job, args, quality, output_dir)
            run.results.extend(job_results)
            run.job_lines.append(
                (job.name, len(job_results), _job_folder(output_dir, job.name))
            )

            if upload:
                # Ask the files where they landed rather than re-deriving the
                # folder name: the pipeline owns that naming, collisions and all.
                folder = next(
                    (r.path.parent for r in job_results if r.succeeded and r.path), None
                )
                if folder is not None and folder not in run.folders:
                    run.folders.append(folder)

            # run_job catches Ctrl-C itself and returns only what finished, so a
            # short result list is the only signal that the user bailed out.
            if len(job_results) < len(job.tracks):
                run.interrupted = True
                print_err("Interrupted.")
                break
    except KeyboardInterrupt:
        run.interrupted = True
        print_err("Interrupted.")
    return run


def _preflight(args: argparse.Namespace):
    """Check ffmpeg and resolve the output directory.

    Returns the output directory, or ``None`` when the run cannot go ahead;
    the reason is already on stderr.
    """
    from downloader import FFmpegNotFoundError, check_ffmpeg, ensure_output_dir

    try:
        check_ffmpeg()
    except FFmpegNotFoundError as exc:
        print_err(str(exc))
        return None

    try:
        return ensure_output_dir(args.output_dir)
    except (NotADirectoryError, OSError) as exc:
        print_err("Error: %s" % exc)
        return None


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the CLI. See the module docstring for the exit-code contract."""
    parser = build_parser()
    args = parser.parse_args(argv)

    configure_logging(args.verbose)
    _silence_third_party_warnings()

    # --login writes the session file and nothing else, so it runs before any
    # other flag is even validated.
    if args.login:
        from cli_upload import run_login

        return run_login(args)

    if args.dir and (args.inputs or args.playlist or args.name or args.url or args.file):
        parser.error(
            "--dir uploads a folder that is already on disk, so there is nothing to "
            "download; it cannot be combined with -p/-n/-u/-f or positional inputs"
        )

    # --dir means "upload this, download nothing".
    upload = args.upload or bool(args.dir)

    try:
        quality = parse_quality(args.quality)
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
        from cli_upload import upload_only

        return upload_only(args)

    try:
        inputs = _collect_inputs(args)
    except (FileNotFoundError, OSError) as exc:
        print_err("Error: %s" % exc)
        return EXIT_FAILURE

    if not inputs:
        print_err(
            "Error: nothing to download. Give a playlist link, a Spotify URI or a "
            "playlist name -- positionally or with -p/-n/-u/-f.\n"
        )
        parser.print_usage(sys.stderr)
        return EXIT_USAGE

    output_dir = _preflight(args)
    if output_dir is None:
        return EXIT_FAILURE

    run = _run_downloads(inputs, args, quality, output_dir, upload)

    # The report is written even on a partial run: what failed before the Ctrl-C
    # is exactly what the user wants to retry.
    failed = _print_summary(run.results, run.job_lines, output_dir)

    upload_failed = 0
    if upload and not run.interrupted and not run.setup_error:
        if run.folders:
            try:
                from cli_upload import upload_albums

                upload_failed = upload_albums(run.folders, args)
            except KeyboardInterrupt:
                run.interrupted = True
                print_err("Interrupted.")
        else:
            print_err("Nothing to upload: no album folder was produced.")

    if run.interrupted:
        return EXIT_INTERRUPTED
    if run.setup_error or failed or upload_failed:
        return EXIT_FAILURE
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
