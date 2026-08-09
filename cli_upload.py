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
from typing import Any, List, Optional, Sequence, Tuple

from cli_args import STATE_FILE_DEFAULT
from cli_console import (
    EXIT_FAILURE,
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_USAGE,
    SYMBOLS,
    print_err,
    print_out,
)
from cli_progress import UploadReporter

__all__ = ["run_login", "upload_albums", "upload_only"]

logger = logging.getLogger("sc-scrape")

#: Seconds ``--login`` waits for the user to finish logging in by hand. Long,
#: because it can involve a password manager, an e-mail code and a captcha.
LOGIN_TIMEOUT = 300


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
        print_err(
            "Error: uploading needs %s.py, which could not be imported (%s)."
            % (name, exc)
        )
        print_err(
            "Make sure %s.py is next to main.py, then run 'pip install -r "
            "requirements.txt' and 'playwright install chromium'." % name
        )
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

    Args:
        args: Parsed command-line arguments; only ``--state-file`` is used.

    Returns:
        ``0`` once the session file exists, ``1`` if the login did not complete,
        ``130`` on Ctrl-C.
    """
    engine = _soundcloud("browser_engine")
    if engine is None:
        return EXIT_FAILURE

    print_err(
        "Opening SoundCloud in a browser. Log in there; waiting up to %d seconds."
        % LOGIN_TIMEOUT
    )
    try:
        state_path = engine.login(
            state_path=args.state_file, timeout=LOGIN_TIMEOUT
        )
    except KeyboardInterrupt:
        print_err("Interrupted.")
        return EXIT_INTERRUPTED
    except engine.NotLoggedInError as exc:
        print_err("Error: login did not complete: %s" % exc)
        return EXIT_FAILURE
    except Exception as exc:  # noqa: BLE001 - a login crash is our exit code
        logger.error("login failed: %s", exc, exc_info=args.verbose)
        print_err("Error: login failed: %s" % exc)
        return EXIT_FAILURE

    print_out("Session saved to: %s" % (state_path or args.state_file))
    print_err(
        "Treat that file as a password: anyone holding it is logged in as you."
    )
    return EXIT_OK


def _check_selectors(uploader, page, reporter: UploadReporter) -> int:
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
        reporter.note("%s selector check failed: %s" % (SYMBOLS["failed"], exc))
        return 1

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
    return len(blocking)


def upload_albums(folders: Sequence[Any], args: argparse.Namespace) -> int:
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

    reporter = UploadReporter(len(folders), verbose=args.verbose)
    try:
        if getattr(args, "attach", False):
            # The attached browser is the user's own: it carries its own
            # session, so --state-file and --headed have nothing to say about
            # it, and asset blocking stays off in someone's real window.
            session = engine.attached_browser(
                endpoint=args.attach_endpoint,
                block_assets=False,
            )
        else:
            session = engine.browser(
                state_path=args.state_file,
                headless=not args.headed,
                block_assets=True,
            )
        with session as page:
            if args.dry_run and _check_selectors(uploader, page, reporter):
                # A missing selector is exactly what --dry-run exists to find;
                # going on would only report the same breakage once per folder.
                print_out("Dry run: selector check failed, nothing uploaded")
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
                    # privacy and dry_run are always passed explicitly:
                    # silently dropping dry_run would publish
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
        print_err("Error: no usable SoundCloud session in %s." % args.state_file)
        _login_hint()
        return len(folders) - len(uploaded) - len(skipped)
    except Exception as exc:  # noqa: BLE001 - a browser crash is our exit code
        reporter.close()
        logger.error("upload session failed: %s", exc, exc_info=args.verbose)
        print_err("Error: could not run the upload session: %s" % exc)
        return len(folders) - len(uploaded) - len(skipped)
    finally:
        reporter.close()

    verb = "Dry run" if args.dry_run else "Uploaded"
    print_out(
        "%s: %d set(s), %d skipped, %d failed"
        % (verb, len(uploaded), len(skipped), len(failures))
    )
    for title, url in uploaded:
        print_out("%s -> %s" % (title, url or ("dry run" if args.dry_run else "no URL returned")))
    for title, url in skipped:
        print_out("%s -> %s (already uploaded)" % (title, url or "no URL recorded"))
    for title, reason in failures:
        print_out("%s: failed - %s" % (title, reason))
    return len(failures)


def upload_only(args: argparse.Namespace) -> int:
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
        print_err("Error: not a folder: %s" % folder)
        return EXIT_FAILURE
    try:
        failed = upload_albums([folder], args)
    except KeyboardInterrupt:
        print_err("Interrupted.")
        return EXIT_INTERRUPTED
    return EXIT_FAILURE if failed else EXIT_OK

