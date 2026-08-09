"""Argument dispatch and the download loop -- the body of the ``spot2cloud`` command.

Accepts anything a user is likely to paste: a Spotify playlist or album link or
URI, a YouTube playlist or video URL, or just a playlist *name* to search for.
Each is resolved into a :class:`~spot2cloud.models.PlaylistJob` and handed to
:func:`spot2cloud.download.pipeline.run_job` for concurrent downloading.

With ``--upload``, or ``--dir`` for a folder already on disk, the finished album
folders are published to the user's own SoundCloud account through
:mod:`spot2cloud.cli.upload`.

Stream discipline
-----------------
Everything diagnostic -- log records, the progress bar, per-track lines, error
messages -- goes to **stderr**. The only thing on **stdout** is the final
summary, so ``spot2cloud ... | grep failed`` stays useful and piping the tool
into another program never has to strip terminal escape codes.

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
from dataclasses import dataclass, field
from typing import Any, List, Optional, Sequence, Tuple

from spot2cloud.cli.args import build_parser, parse_quality
from spot2cloud.cli.console import (
    EXIT_FAILURE,
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_USAGE,
    configure_logging,
    print_err,
    print_out,
)
from spot2cloud.cli.progress import ProgressReporter
from spot2cloud.models import (
    STATUS_FAILED,
    STATUS_OK,
    STATUS_SKIPPED,
    DownloadResult,
    PlaylistJob,
)

__all__ = ["build_parser", "main"]

logger = logging.getLogger("spot2cloud")


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
    """
    from pathlib import Path

    from spot2cloud.download.downloader import sanitize_filename
    from spot2cloud.download.pipeline import FALLBACK_PLAYLIST_NAME

    raw = (name or "").strip()
    folder = sanitize_filename(raw) if raw else ""
    return Path(output_dir) / (folder or FALLBACK_PLAYLIST_NAME)


#: ``--watch``/``--serve`` targets: module, callable, and the flag that asks
#: for it. Both modules are optional, so a missing one must be a message.
_DELEGATES = {
    "watch": ("spot2cloud.services.watcher", "watch_loop", "--watch"),
    "serve": ("spot2cloud.services.server", "serve_forever", "--serve"),
}


def _delegate(mode: str, args: argparse.Namespace, quality: str) -> int:
    """Hand control to ``watcher.watch_loop`` or ``server.serve_forever``."""
    module_name, func_name, flag = _DELEGATES[mode]

    try:
        delegate = getattr(importlib.import_module(module_name), func_name)
    except (ImportError, AttributeError) as exc:
        print_err(
            "Error: %s needs %s.py, which could not be imported (%s)." % (flag, module_name, exc)
        )
        print_err("Reinstall the package, or run without %s." % flag)
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
    except Exception as exc:
        logger.error("%s failed: %s", flag, exc, exc_info=args.verbose)
        print_err("Error: %s failed: %s" % (flag, exc))
        return EXIT_FAILURE

    return outcome if isinstance(outcome, int) else EXIT_OK


def _write_failure_log(results: Sequence[DownloadResult], output_dir) -> Optional[Any]:
    """Write ``skipped.txt``, returning its path or ``None``."""
    from spot2cloud.download.downloader import write_failed_log

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
    from spot2cloud.download.pipeline import run_job

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
    from spot2cloud.sources.playlist_parser import SpotifyCredentialsError

    run = _DownloadRun()
    try:
        for text in inputs:
            # Imported per input for the same reason as run_job above.
            from spot2cloud.sources.playlist_parser import resolve_input

            try:
                job = resolve_input(text)
            except SpotifyCredentialsError as exc:
                # A missing API key is a setup problem, not a bad input: every
                # other Spotify entry would fail the same way. Stop and explain.
                print_err(str(exc))
                run.setup_error = True
                break
            except Exception as exc:
                logger.error("could not resolve %r: %s", text, exc, exc_info=args.verbose)
                run.results.append(DownloadResult.failed(text, "could not resolve input: %s" % exc))
                continue

            if not job.tracks:
                logger.warning("no tracks found for %r", text)
                run.results.append(DownloadResult.failed(text, "no tracks found"))
                continue

            job_results = _download_job(job, args, quality, output_dir)
            run.results.extend(job_results)
            run.job_lines.append((job.name, len(job_results), _job_folder(output_dir, job.name)))

            if upload:
                # Ask the files where they landed rather than re-deriving the
                # folder name: the pipeline owns that naming, collisions and all.
                folder = next((r.path.parent for r in job_results if r.succeeded and r.path), None)
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
    from spot2cloud.download.downloader import FFmpegNotFoundError, check_ffmpeg, ensure_output_dir

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

    # --login writes the session file and nothing else, so it runs before any
    # other flag is even validated.
    if args.login:
        from spot2cloud.cli.upload import run_login

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
        from spot2cloud.cli.upload import upload_only

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
                from spot2cloud.cli.upload import upload_albums

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
