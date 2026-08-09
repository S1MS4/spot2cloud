"""The SoundCloud half of the CLI: login, and publishing album folders.

Kept in its own module so ``main`` can import it lazily. ``browser_engine``
imports Playwright at module scope, and a download-only user has no reason to
have a browser installed -- so nothing here may be imported at startup.
:func:`_soundcloud` is what turns that missing install into install advice
rather than a traceback.
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from typing import Any, List, Optional, Sequence

from spot2cloud.cli.console import (
    EXIT_FAILURE,
    EXIT_INTERRUPTED,
    EXIT_OK,
    SYMBOLS,
    print_err,
    print_out,
)
from spot2cloud.cli.progress import UploadReporter
from spot2cloud.store.upload_ledger import UploadLedger, set_key, track_key

__all__ = ["run_login", "upload_albums", "upload_only"]

logger = logging.getLogger("spot2cloud")

#: Seconds ``--login`` waits for the user to finish logging in by hand. Long,
#: because it can involve a password manager, an e-mail code and a captcha.
LOGIN_TIMEOUT = 300


def _soundcloud(name: str):
    """Import an upload-stage module, or explain why it is unavailable.

    ``browser_engine`` and ``uploader`` pull in Playwright, which a user who only
    ever downloads has no reason to install, so they are imported here rather than
    at module scope. Returns ``None`` after printing what went wrong.
    """
    import importlib

    try:
        return importlib.import_module("spot2cloud.soundcloud.%s" % name)
    except ImportError as exc:
        print_err("Error: uploading needs %s, which could not be imported (%s)." % (name, exc))
        print_err("Reinstall with 'pip install -e .', then run 'playwright install chromium'.")
        return None


def _login_hint() -> None:
    """Tell the user how to create the session file the upload stage needs."""
    print_err("Run 'python3 main.py --login' first, then try again.")


def run_login(args: argparse.Namespace) -> int:
    """Open SoundCloud's own login page and save the resulting session.

    There is deliberately no ``--password`` flag and no prompt: Playwright shows
    SoundCloud's real page, the user types into that, and all this program ever
    touches is the session state it leaves behind. A credential we never hold is
    one we cannot log, leak, or leave in a shell history.
    """
    engine = _soundcloud("browser_engine")
    if engine is None:
        return EXIT_FAILURE

    print_err(
        "Opening SoundCloud in a browser. Log in there; waiting up to %d seconds." % LOGIN_TIMEOUT
    )
    try:
        state_path = engine.login(state_path=args.state_file, timeout=LOGIN_TIMEOUT)
    except KeyboardInterrupt:
        print_err("Interrupted.")
        return EXIT_INTERRUPTED
    except engine.NotLoggedInError as exc:
        print_err("Error: login did not complete: %s" % exc)
        return EXIT_FAILURE
    except Exception as exc:
        logger.error("login failed: %s", exc, exc_info=args.verbose)
        print_err("Error: login failed: %s" % exc)
        return EXIT_FAILURE

    print_out("Session saved to: %s" % (state_path or args.state_file))
    print_err("Treat that file as a password: anyone holding it is logged in as you.")
    return EXIT_OK


def _selectors_ok(uploader, page, reporter: UploadReporter) -> bool:
    """Verify the uploader's selectors against the live page, and report.

    Only the keys in ``uploader.PREATTACH_KEYS`` can fail here: the metadata
    form does not exist until files are attached, and attaching them is the
    upload, so the rest legitimately read as missing on an empty page.
    """
    try:
        found = uploader.check_selectors(page) or {}
    except Exception as exc:  # noqa: BLE001 - the check is the point of --dry-run
        reporter.note("%s selector check failed: %s" % (SYMBOLS["failed"], exc))
        return False

    preattach = getattr(uploader, "PREATTACH_KEYS", frozenset(found))
    missing = sorted(name for name, present in found.items() if not present)
    blocking = [name for name in missing if name in preattach]
    for name in blocking:
        reporter.note("%s selector missing: %s" % (SYMBOLS["failed"], name))
    reporter.note(
        "%s %d/%d selectors found (%d only exist once files are attached)"
        % (
            SYMBOLS["ok"],
            len(found) - len(missing),
            len(found),
            len(missing) - len(blocking),
        )
    )
    return not blocking


def _open_session(engine, args: argparse.Namespace):
    """Open the browser the upload will drive."""
    if getattr(args, "attach", False):
        # The attached browser is the user's own: it carries its own session, so
        # --state-file and --headed have nothing to say about it, and asset
        # blocking stays off in someone's real window.
        return engine.attached_browser(endpoint=args.attach_endpoint, block_assets=False)
    return engine.browser(
        state_path=args.state_file,
        headless=not args.headed,
        block_assets=True,
    )


@dataclass
class _Outcome:
    """What became of one album folder."""

    status: str  # UPLOADED, SKIPPED or FAILED
    title: str
    #: Set URL when uploaded or skipped, failure reason when failed.
    detail: Optional[str] = None


UPLOADED = "uploaded"
SKIPPED = "skipped"
FAILED = "failed"


def _record_upload(ledger, key: str, album, set_url: Optional[str]) -> None:
    """Write the set and its tracks to the ledger."""
    with ledger:
        ledger.record(key, set_url=set_url)
        # Per-track keys too, so a future partial resume can tell which files of
        # a half-finished set already landed.
        for track in getattr(album, "tracks", None) or []:
            source = getattr(track, "upload_path", None) or getattr(track, "path", None)
            if source is not None:
                ledger.record(track_key(str(source)), set_url=set_url)


def _upload_one(page, folder, index: int, args, engine, uploader, reporter) -> _Outcome:
    """Publish one album folder.

    Raises only ``engine.NotLoggedInError``, which kills the whole batch. Every
    other failure becomes a FAILED outcome so one bad folder cannot end the run.
    """
    try:
        album = uploader.scan_album(folder)
    except Exception as exc:
        logger.error("could not read %s: %s", folder, exc, exc_info=args.verbose)
        reporter.failed(index, folder.name, "could not read the album: %s" % exc)
        return _Outcome(FAILED, folder.name, str(exc))

    title = getattr(album, "title", None) or folder.name
    ledger = UploadLedger(folder)
    key = set_key(title)
    recorded = ledger.get(key)
    if recorded is not None:
        url = recorded.get("set_url")
        reporter.skipped(index, title, url)
        return _Outcome(SKIPPED, title, url)

    reporter.begin(title)
    try:
        if not args.dry_run:
            # Transcoding is minutes of CPU and a second copy of the album on
            # disk. A dry run attaches no files, so it has no use for either.
            album = uploader.prepare_for_upload(album)
        # privacy and dry_run are always passed explicitly: dropping dry_run
        # would publish what the user asked us only to rehearse, and dropping
        # privacy would fall back to upload_album's own default instead of the
        # visibility asked for. upload_album then refuses to submit at all
        # unless SoundCloud's own privacy select reads back what was asked for.
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
        return _Outcome(FAILED, title, str(exc))
    except Exception as exc:
        logger.error("upload of %s failed: %s", title, exc, exc_info=args.verbose)
        reporter.failed(index, title, "unexpected error: %s" % exc)
        return _Outcome(FAILED, title, str(exc))

    if args.dry_run:
        # Nothing was published, so nothing is written -- not even an empty
        # ledger. A dry run leaves the folder as it found it, and the real run
        # afterwards must not skip the set as done.
        reporter.ok(index, title, "dry run")
        return _Outcome(UPLOADED, title, None)

    _record_upload(ledger, key, album, set_url)
    reporter.ok(index, title, set_url)
    return _Outcome(UPLOADED, title, set_url)


def _print_upload_summary(outcomes: Sequence[_Outcome], dry_run: bool) -> None:
    """Write the per-set lines and the count line to stdout."""
    buckets = {UPLOADED: [], SKIPPED: [], FAILED: []}
    for outcome in outcomes:
        buckets[outcome.status].append(outcome)

    print_out(
        "%s: %d set(s), %d skipped, %d failed"
        % (
            "Dry run" if dry_run else "Uploaded",
            len(buckets[UPLOADED]),
            len(buckets[SKIPPED]),
            len(buckets[FAILED]),
        )
    )
    for outcome in buckets[UPLOADED]:
        fallback = "dry run" if dry_run else "no URL returned"
        print_out("%s -> %s" % (outcome.title, outcome.detail or fallback))
    for outcome in buckets[SKIPPED]:
        print_out(
            "%s -> %s (already uploaded)" % (outcome.title, outcome.detail or "no URL recorded")
        )
    for outcome in buckets[FAILED]:
        print_out("%s: failed - %s" % (outcome.title, outcome.detail))


def upload_albums(folders: Sequence[Any], args: argparse.Namespace) -> int:
    """Publish each album folder to SoundCloud and print the upload summary.

    One browser session covers the whole batch, and each folder's ledger is
    consulted first, so an interrupted run resumes instead of leaving the user
    a duplicate set to delete by hand.

    Returns how many folders did not go up -- ``len(folders)`` when the session
    could not be opened at all.
    """
    engine = _soundcloud("browser_engine")
    uploader = _soundcloud("uploader")
    if engine is None or uploader is None:
        return len(folders)

    outcomes: List[_Outcome] = []
    reporter = UploadReporter(len(folders), verbose=args.verbose)
    try:
        with _open_session(engine, args) as page:
            if args.dry_run and not _selectors_ok(uploader, page, reporter):
                # A missing selector is exactly what --dry-run exists to find;
                # going on would report the same breakage once per folder.
                print_out("Dry run: selector check failed, nothing uploaded")
                return len(folders)

            for index, folder in enumerate(folders, 1):
                outcomes.append(_upload_one(page, folder, index, args, engine, uploader, reporter))
    except engine.NotLoggedInError as exc:
        reporter.close()
        # The module's own message says the same thing; one hint is enough.
        logger.debug("no session: %s", exc)
        print_err("Error: no usable SoundCloud session in %s." % args.state_file)
        _login_hint()
        return _unhandled(folders, outcomes)
    except Exception as exc:
        reporter.close()
        logger.error("upload session failed: %s", exc, exc_info=args.verbose)
        print_err("Error: could not run the upload session: %s" % exc)
        return _unhandled(folders, outcomes)
    finally:
        reporter.close()

    _print_upload_summary(outcomes, args.dry_run)
    return sum(1 for outcome in outcomes if outcome.status == FAILED)


def _unhandled(folders: Sequence[Any], outcomes: Sequence[_Outcome]) -> int:
    """Folders the aborted session never got a good answer for."""
    settled = sum(1 for o in outcomes if o.status in (UPLOADED, SKIPPED))
    return len(folders) - settled


def upload_only(args: argparse.Namespace) -> int:
    """Run the upload stage alone, for ``--dir``."""
    from pathlib import Path

    folder = Path(args.dir).expanduser()
    if not folder.is_dir():
        print_err("Error: not a folder: %s" % folder)
        return EXIT_FAILURE
    try:
        failed = upload_albums([folder], args)
    except KeyboardInterrupt:
        print_err("Interrupted.")
        return EXIT_INTERRUPTED
    return EXIT_FAILURE if failed else EXIT_OK
