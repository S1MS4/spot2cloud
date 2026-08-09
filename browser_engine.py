"""Playwright session layer for SoundCloud.

SoundCloud has no upload API for third parties, so uploading means driving the
real web app. This module owns everything browser-shaped -- the one-off
interactive login, the saved session, and a configured :class:`Page` -- so the
uploader on top can stay a plain synchronous function that knows only about
selectors and files.

The saved session lives in ``storage_state.json``. That file *is* the login:
anyone holding it is signed in as the user, which is why :func:`login` chmods it
to 0600 and why it belongs in .gitignore.

What this cannot promise: SoundCloud sits behind Cloudflare, and a headless
Chromium that refuses images, fonts and CSS is a recognisable combination. There
is no code here (or anywhere) that guarantees a run is not challenged or
blocked. When that happens the practical escape hatches are, in order: rerun
with ``headless=False`` so the browser looks and paints like a real one, then
``block_assets=False`` so the request pattern stops standing out, then wait.
"""

from __future__ import annotations

import json
import contextlib
import logging
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from urllib.parse import urlparse

from playwright.sync_api import Page, sync_playwright

__all__ = [
    "STATE_FILE",
    "SOUNDCLOUD",
    "SIGN_IN_URL",
    "BLOCKED_RESOURCE_TYPES",
    "ANALYTICS_HOSTS",
    "NotLoggedInError",
    "login",
    "is_logged_in",
    "browser",
    "attached_browser",
    "CDP_ENDPOINT",
    "DEFAULT_TIMEOUT_MS",
    "goto",
    "set_asset_blocking",
]

log = logging.getLogger(__name__)

STATE_FILE = "storage_state.json"
SOUNDCLOUD = "https://soundcloud.com"
SIGN_IN_URL = "%s/signin" % SOUNDCLOUD

LOGIN_HINT = "No SoundCloud session found. Run `python main.py --login` first."

# Everything the SPA needs to boot is deliberately absent here. Aborting
# document/script/xhr/fetch/websocket leaves SoundCloud a blank page: the shell
# is React and every track, form and upload endpoint arrives over XHR.
BLOCKED_RESOURCE_TYPES = frozenset({"image", "font", "stylesheet", "media"})

ANALYTICS_HOSTS = (
    "google-analytics.com",
    "googletagmanager.com",
    "doubleclick.net",
    "facebook.com",
    "facebook.net",
    "sentry.io",
    "hotjar.com",
    "quantserve.com",
    "scorecardresearch.com",
)

# Signed-in-only markers, best signal first. SoundCloud reskins its header
# without warning, so this is a chain rather than one selector.
LOGGED_IN_SELECTORS = (
    ".header__userNav",
    "button.header__userNavButton",
    "a.header__userNavButton",
    "[aria-label='User menu']",
    "a.header__upload",
    "a[href='/upload']",
)

# Short: this runs in a poll loop, and a missing marker is the common case.
PROBE_TIMEOUT_MS = 1500
NAV_TIMEOUT_MS = 45000
POLL_SECONDS = 2.0

VIEWPORT = {"width": 1440, "height": 900}
LOCALE = "en-US"

# A stock desktop Chrome string. Kept close to the bundled Chromium's real
# version on purpose - claiming a wildly different Chrome than the one actually
# rendering is a stronger tell than the default UA would have been.
# Chromium's own UA announces "HeadlessChrome", which is a loud automation
# signal, so it gets overridden. Deriving the version from the running browser
# instead of pinning it matters: a claimed version that disagrees with the real
# engine is itself fingerprintable, and a pinned string silently rots on every
# Playwright upgrade (it was already 8 majors stale once).
_UA_TEMPLATE = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/%s Safari/537.36"
)
_UA_FALLBACK_VERSION = "148.0.0.0"


def user_agent_for(browser) -> str:
    """Build a headless-free UA string matching the browser's real version."""
    try:
        version = (browser.version or "").strip()
    except Exception:  # noqa: BLE001 - a UA is never worth failing a launch over
        version = ""
    return _UA_TEMPLATE % (version or _UA_FALLBACK_VERSION)


# Drops the one flag Playwright sets that any script can read back. It does not
# make the browser undetectable; it just stops us announcing ourselves.
CHROMIUM_ARGS = ["--disable-blink-features=AutomationControlled"]

ROUTE_PATTERN = "**/*"


class NotLoggedInError(RuntimeError):
    """No usable SoundCloud session; the user has to sign in interactively."""


def _is_analytics(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in ANALYTICS_HOSTS)


def _make_route_handler():
    """Build the interception callback; separate so tests can drive it directly."""

    def handle(route) -> None:
        try:
            request = route.request
            if request.resource_type in BLOCKED_RESOURCE_TYPES:
                route.abort()
            elif _is_analytics(request.url):
                route.abort()
            else:
                route.continue_()
        except Exception:
            # Handlers keep firing while a page is navigating away or closing,
            # where abort/continue_ raise on a dead target. Never let that
            # bubble into the caller's upload.
            log.debug("route handler failed", exc_info=True)

    return handle


# One shared callback, because page.unroute() only removes the exact object
# that page.route() was given.
_ROUTE_HANDLER = _make_route_handler()


def set_asset_blocking(page: Page, enabled: bool) -> None:
    """Turn interception on or off for one page.

    The uploader turns it *off* on the upload page itself, where the artwork
    preview and the progress UI are images and CSS.
    """
    if enabled:
        page.route(ROUTE_PATTERN, _ROUTE_HANDLER)
    else:
        page.unroute(ROUTE_PATTERN, _ROUTE_HANDLER)


def goto(page: Page, url: str, wait_until: str = "domcontentloaded"):
    """Navigate without waiting for networkidle, which SoundCloud never reaches."""
    return page.goto(url, wait_until=wait_until, timeout=NAV_TIMEOUT_MS)


AUTH_COOKIES = ("oauth_token", "sc_oauth_token")


def has_auth_cookie(page: Page) -> bool:
    """Whether the context holds a real SoundCloud auth token.

    This is the only trustworthy signal. DOM checks are guesses: the logged-out
    homepage renders the same header shell and hides the sign-in control behind
    a menu, so "header present, no sign-in button" reads as success on a page
    where nobody is signed in. That false positive silently saves an anonymous
    session, and every later step then fails somewhere far less obvious.
    """
    try:
        cookies = page.context.cookies("https://soundcloud.com")
    except Exception:  # noqa: BLE001
        return False
    return any(
        cookie.get("name") in AUTH_COOKIES and cookie.get("value")
        for cookie in cookies
    )


def is_logged_in(page: Page) -> bool:
    """Whether this session is actually signed in. Never raises, never navigates.

    Requires an auth cookie. A signed-in-looking header is accepted only as
    corroboration, never on its own -- see :func:`has_auth_cookie`.
    """
    if has_auth_cookie(page):
        return True

    # No token: the DOM cannot overrule that, but a signed-in header with no
    # cookie is worth surfacing because it means SoundCloud changed its auth
    # storage and AUTH_COOKIES needs updating.
    for selector in LOGGED_IN_SELECTORS:
        try:
            if page.wait_for_selector(selector, timeout=PROBE_TIMEOUT_MS, state="attached"):
                log.warning(
                    "browser_engine: page looks signed in but no %s cookie was found; "
                    "SoundCloud may have changed how it stores auth",
                    "/".join(AUTH_COOKIES),
                )
                return False
        except Exception:
            continue
    return False


def _close_all(page, context, browser_obj, playwright) -> None:
    for item in (page, context, browser_obj):
        if item is not None:
            with contextlib.suppress(Exception):
                item.close()
    if playwright is not None:
        with contextlib.suppress(Exception):
            playwright.stop()


def login(state_path: str = STATE_FILE, timeout: int = 300) -> Path:
    """Open a real browser, wait for the user to sign in, and save the session.

    Credentials are never touched by this process: the user types them into
    SoundCloud's own page (and handles any 2FA or captcha there), and we only
    poll until the header says they got through.
    """
    path = Path(state_path)
    playwright = sync_playwright().start()
    browser_obj = None
    context = None
    page = None
    try:
        browser_obj = _launch(playwright, headless=False)
        context = browser_obj.new_context(
            user_agent=user_agent_for(browser_obj), viewport=VIEWPORT, locale=LOCALE
        )
        page = context.new_page()
        goto(page, SIGN_IN_URL)

        # main.py attaches a console handler to the root logger, so this is the
        # prompt the user actually sees.
        log.info(
            "A browser window is open. Sign in to SoundCloud there and this will "
            "detect it automatically (waiting up to %ds).",
            timeout,
        )

        deadline = time.monotonic() + timeout
        while True:
            if is_logged_in(page):
                context.storage_state(path=str(path))
                # This file is a live credential, not config.
                os.chmod(str(path), 0o600)
                log.info("Signed in. Session saved to %s (mode 0600).", path)
                return path
            if time.monotonic() >= deadline:
                break
            time.sleep(POLL_SECONDS)

        raise NotLoggedInError(
            "Timed out after %ds waiting for sign-in. Run `python main.py --login` "
            "again and complete the sign-in in the browser window." % timeout
        )
    finally:
        _close_all(page, context, browser_obj, playwright)


# Playwright's bundled Chromium identifies itself as "Chrome for Testing" and
# carries automation tells that SoundCloud's bot detection acts on -- it blocked
# a login outright. Driving the real installed Chrome removes the most obvious
# of those. It is a best-effort swap: if Chrome is absent, the bundled build is
# still better than not running.
CHROME_CHANNEL = "chrome"


def _launch_kwargs(headless: bool, slow_mo: int = 0) -> dict:
    """Launch options preferring real Chrome over the bundled Chromium."""
    kwargs = {"headless": headless, "args": CHROMIUM_ARGS}
    if slow_mo:
        kwargs["slow_mo"] = slow_mo
    if Path("/Applications/Google Chrome.app").exists():
        kwargs["channel"] = CHROME_CHANNEL
    return kwargs


def _launch(playwright, headless: bool, slow_mo: int = 0):
    """Launch Chrome, falling back to bundled Chromium if that channel is absent."""
    kwargs = _launch_kwargs(headless, slow_mo)
    try:
        return playwright.chromium.launch(**kwargs)
    except Exception as exc:  # noqa: BLE001 - channel may not be installed
        if "channel" not in kwargs:
            raise
        log.warning("real Chrome unavailable (%s); using bundled Chromium", exc)
        kwargs.pop("channel")
        return playwright.chromium.launch(**kwargs)


@contextmanager
def browser(
    state_path: str = STATE_FILE,
    headless: bool = True,
    block_assets: bool = True,
    slow_mo: int = 0,
) -> Iterator[Page]:
    """Yield a Page carrying the saved session, tearing everything down after."""
    state = Path(state_path)
    if not state.exists():
        raise NotLoggedInError(LOGIN_HINT)

    playwright = sync_playwright().start()
    browser_obj = None
    context = None
    page = None
    try:
        browser_obj = _launch(playwright, headless=headless, slow_mo=slow_mo)
        context = browser_obj.new_context(
            storage_state=str(state),
            user_agent=user_agent_for(browser_obj),
            viewport=VIEWPORT,
            locale=LOCALE,
        )
        context.set_default_timeout(NAV_TIMEOUT_MS)
        page = context.new_page()
        if block_assets:
            set_asset_blocking(page, True)
        yield page
    finally:
        _close_all(page, context, browser_obj, playwright)


# --- importing a session from a real browser -------------------------------
#
# SoundCloud sits behind bot detection that flags Playwright's Chromium during
# login, no matter how the UA is dressed up. Signing in with an ordinary
# browser and lifting the resulting cookies sidesteps that entirely: the login
# never happens under automation. yt-dlp already solves the hard part (macOS
# Keychain decryption for Chrome, the binarycookies format for Safari).

BROWSER_CANDIDATES = ("chrome", "safari", "firefox", "edge", "brave")



def _expiry(value) -> float:
    """Normalise a cookie expiry to the unix seconds Playwright expects.

    Browser stores disagree on epochs: Chrome records microseconds since 1601,
    while Playwright accepts -1 (session cookie) or positive unix seconds. A
    raw Chrome value lands around 1.3e16 and gets rejected outright, so anything
    implausibly large is treated as Chrome's format and converted.
    """
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return -1
    if seconds <= 0:
        return -1
    if seconds > 1e12:  # far beyond any real unix timestamp
        seconds = seconds / 1_000_000 - 11_644_473_600  # 1601 -> 1970
    return seconds if 0 < seconds < 4e9 else -1


def import_cookies_from_browser(
    browser_name: str, state_path: str = STATE_FILE
) -> Path:
    """Lift a signed-in SoundCloud session out of a real browser.

    Raises:
        NotLoggedInError: The browser holds no SoundCloud auth cookie, i.e. you
            are not signed in there.
        RuntimeError: The cookie store could not be read at all -- on macOS this
            is usually Full Disk Access, which the terminal needs for Safari.
    """
    from yt_dlp.cookies import extract_cookies_from_browser

    class _Quiet:
        def debug(self, *a, **k): pass
        def info(self, *a, **k): pass
        def warning(self, *a, **k): pass
        def error(self, *a, **k): pass
        def to_screen(self, *a, **k): pass
        def write_debug(self, *a, **k): pass
        def report_warning(self, *a, **k): pass
        def report_error(self, *a, **k): pass

    try:
        jar = extract_cookies_from_browser(browser_name, logger=_Quiet())
    except Exception as exc:  # noqa: BLE001 - surfaced with actionable advice
        raise RuntimeError(
            "could not read %s's cookies: %s\n"
            "On macOS the terminal needs Full Disk Access "
            "(System Settings -> Privacy & Security -> Full Disk Access)."
            % (browser_name, exc)
        ) from exc

    cookies = []
    for cookie in jar:
        if "soundcloud.com" not in (cookie.domain or ""):
            continue
        cookies.append(
            {
                "name": cookie.name,
                "value": cookie.value or "",
                "domain": cookie.domain,
                "path": cookie.path or "/",
                "expires": _expiry(cookie.expires),
                "httpOnly": False,
                "secure": bool(cookie.secure),
                "sameSite": "Lax",
            }
        )

    if not any(c["name"] in AUTH_COOKIES and c["value"] for c in cookies):
        raise NotLoggedInError(
            "%s has SoundCloud cookies but no auth token -- you are not signed "
            "in there. Open %s in %s, log in, then run this again."
            % (browser_name, SOUNDCLOUD, browser_name)
        )

    target = Path(state_path)
    target.write_text(json.dumps({"cookies": cookies, "origins": []}, indent=1))
    os.chmod(target, 0o600)
    log.info(
        "Imported %d SoundCloud cookies from %s into %s (mode 0600)",
        len(cookies), browser_name, target,
    )
    return target


# --- driving your own Chrome -----------------------------------------------
#
# The most reliable way to look like a real browser is to be one. Rather than
# dressing up a bundled Chromium, attach to the Chrome already running on this
# machine, with its real profile, real history and real signed-in session.
# Nothing is spoofed because nothing needs to be.
#
# Start Chrome with remote debugging enabled first:
#     /Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome \\
#         --remote-debugging-port=9222 --user-data-dir="$HOME/.chrome-automation"
# The separate --user-data-dir keeps this away from your day-to-day profile so a
# crash or a stray automation step cannot disturb it; log in to SoundCloud once
# in that window and the session persists.

CDP_ENDPOINT = "http://127.0.0.1:9222"

# An attached context starts with Playwright's 30s default anyway; setting it
# explicitly means the uploader's own timeouts are the only ones that decide
# how long anything waits.
DEFAULT_TIMEOUT_MS = 30_000


@contextmanager
def attached_browser(endpoint: str = CDP_ENDPOINT, block_assets: bool = False):
    """Yield a Page from an already-running Chrome over CDP.

    Raises:
        RuntimeError: Nothing is listening on the debugging port, with the
            command needed to start Chrome correctly.
    """
    playwright = sync_playwright().start()
    browser_obj = None
    try:
        try:
            browser_obj = playwright.chromium.connect_over_cdp(endpoint)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                "no Chrome listening on %s.\\n"
                "Start one with:\\n"
                "  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' "
                "--remote-debugging-port=9222 "
                '--user-data-dir="$HOME/.chrome-automation"' % endpoint
            ) from exc

        context = browser_obj.contexts[0] if browser_obj.contexts else browser_obj.new_context()
        context.set_default_timeout(DEFAULT_TIMEOUT_MS)
        page = context.pages[0] if context.pages else context.new_page()
        if block_assets:
            page.route(ROUTE_PATTERN, _ROUTE_HANDLER)
        yield page
    finally:
        # Detach only. This is the user's own Chrome, with their own tabs in it:
        # nothing here may close a page, a context or the browser. On a CDP
        # connection close() drops the connection rather than the browser
        # (verified against a live Chrome on 9222, which kept running and kept
        # its tabs), and stopping playwright releases the driver either way.
        with contextlib.suppress(Exception):
            if browser_obj is not None:
                browser_obj.close()
        with contextlib.suppress(Exception):
            playwright.stop()
