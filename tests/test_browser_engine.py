"""Tests for browser_engine, run entirely offline.

sync_playwright is mocked at the module boundary, so no browser is ever
launched and no request leaves the machine. The route-filter tests matter most:
aborting a resource type SoundCloud needs leaves the SPA unbooted, and the
symptom is a selector timing out somewhere far away in the uploader.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sc_scrape.soundcloud import browser_engine


def fake_route(resource_type, url="https://soundcloud.com/asset"):
    route = mock.MagicMock(name="route")
    route.request.resource_type = resource_type
    route.request.url = url
    return route


def fake_playwright():
    """A sync_playwright stand-in; returns (patchable_callable, playwright_mock)."""
    playwright = mock.MagicMock(name="playwright")
    factory = mock.MagicMock(name="sync_playwright")
    factory.return_value.start.return_value = playwright
    return factory, playwright


def chain(playwright):
    """Unpack the browser/context/page mocks a launch would produce."""
    browser_obj = playwright.chromium.launch.return_value
    context = browser_obj.new_context.return_value
    page = context.new_page.return_value
    return browser_obj, context, page


class RouteFilterTest(unittest.TestCase):
    def setUp(self):
        self.handle = browser_engine._make_route_handler()

    def test_blocks_heavy_resource_types(self):
        for resource_type in ("image", "font", "stylesheet", "media"):
            with self.subTest(resource_type=resource_type):
                route = fake_route(resource_type)
                self.handle(route)
                route.abort.assert_called_once_with()
                route.continue_.assert_not_called()

    def test_allows_everything_the_spa_boots_on(self):
        for resource_type in ("document", "script", "xhr", "fetch", "websocket"):
            with self.subTest(resource_type=resource_type):
                route = fake_route(resource_type)
                self.handle(route)
                route.continue_.assert_called_once_with()
                route.abort.assert_not_called()

    def test_unknown_resource_type_is_allowed(self):
        route = fake_route("other")
        self.handle(route)
        route.continue_.assert_called_once_with()

    def test_blocks_analytics_even_for_allowed_types(self):
        hosts = [
            "https://www.google-analytics.com/collect",
            "https://www.googletagmanager.com/gtm.js",
            "https://stats.g.doubleclick.net/j/collect",
            "https://connect.facebook.net/en_US/fbevents.js",
            "https://o1.ingest.sentry.io/api/1/store/",
            "https://static.hotjar.com/c/hotjar-1.js",
            "https://pixel.quantserve.com/pixel",
            "https://sb.scorecardresearch.com/b",
        ]
        for url in hosts:
            with self.subTest(url=url):
                route = fake_route("script", url)
                self.handle(route)
                route.abort.assert_called_once_with()

    def test_soundcloud_xhr_survives(self):
        route = fake_route("xhr", "https://api-v2.soundcloud.com/me")
        self.handle(route)
        route.continue_.assert_called_once_with()
        route.abort.assert_not_called()

    def test_lookalike_host_is_not_treated_as_analytics(self):
        route = fake_route("xhr", "https://notgoogle-analytics.com/x")
        self.handle(route)
        route.continue_.assert_called_once_with()

    def test_handler_swallows_playwright_errors(self):
        route = fake_route("image")
        route.abort.side_effect = RuntimeError("target closed")
        self.handle(route)  # must not raise


class AssetBlockingToggleTest(unittest.TestCase):
    def test_enable_installs_route(self):
        page = mock.MagicMock()
        browser_engine.set_asset_blocking(page, True)
        page.route.assert_called_once_with(
            browser_engine.ROUTE_PATTERN, browser_engine._ROUTE_HANDLER
        )

    def test_disable_unroutes_the_same_handler(self):
        page = mock.MagicMock()
        browser_engine.set_asset_blocking(page, False)
        page.unroute.assert_called_once_with(
            browser_engine.ROUTE_PATTERN, browser_engine._ROUTE_HANDLER
        )
        page.route.assert_not_called()


class GotoTest(unittest.TestCase):
    def test_never_waits_for_networkidle(self):
        page = mock.MagicMock()
        browser_engine.goto(page, "https://soundcloud.com/upload")
        kwargs = page.goto.call_args[1]
        self.assertEqual(kwargs["wait_until"], "domcontentloaded")


class BrowserContextManagerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "storage_state.json"
        self.state.write_text("{}")

    def test_missing_state_raises_with_login_hint(self):
        missing = Path(self.tmp.name) / "nope.json"
        with self.assertRaises(browser_engine.NotLoggedInError) as caught:
            with browser_engine.browser(state_path=str(missing)):
                self.fail("body must not run")
        self.assertIn("--login", str(caught.exception))

    def test_missing_state_does_not_start_playwright(self):
        factory, _ = fake_playwright()
        missing = Path(self.tmp.name) / "nope.json"
        with mock.patch.object(browser_engine, "sync_playwright", factory):
            with self.assertRaises(browser_engine.NotLoggedInError):
                with browser_engine.browser(state_path=str(missing)):
                    pass
        factory.assert_not_called()

    def test_passes_storage_state_to_new_context(self):
        factory, playwright = fake_playwright()
        _, context, _ = chain(playwright)
        with mock.patch.object(browser_engine, "sync_playwright", factory):
            with browser_engine.browser(state_path=str(self.state)) as page:
                self.assertIs(page, context.new_page.return_value)
        kwargs = playwright.chromium.launch.return_value.new_context.call_args[1]
        self.assertEqual(kwargs["storage_state"], str(self.state))
        self.assertEqual(kwargs["viewport"], {"width": 1440, "height": 900})
        self.assertEqual(kwargs["locale"], "en-US")
        self.assertIn("Mozilla/5.0", kwargs["user_agent"])

    def test_headless_and_slow_mo_reach_launch(self):
        factory, playwright = fake_playwright()
        with mock.patch.object(browser_engine, "sync_playwright", factory):
            with browser_engine.browser(state_path=str(self.state), headless=False, slow_mo=250):
                pass
        kwargs = playwright.chromium.launch.call_args[1]
        self.assertFalse(kwargs["headless"])
        self.assertEqual(kwargs["slow_mo"], 250)

    def test_installs_route_by_default(self):
        factory, playwright = fake_playwright()
        _, _, page = chain(playwright)
        with mock.patch.object(browser_engine, "sync_playwright", factory):
            with browser_engine.browser(state_path=str(self.state)):
                pass
        page.route.assert_called_once_with(
            browser_engine.ROUTE_PATTERN, browser_engine._ROUTE_HANDLER
        )

    def test_block_assets_false_installs_no_route(self):
        factory, playwright = fake_playwright()
        _, _, page = chain(playwright)
        with mock.patch.object(browser_engine, "sync_playwright", factory):
            with browser_engine.browser(state_path=str(self.state), block_assets=False):
                pass
        page.route.assert_not_called()

    def test_teardown_runs_when_body_raises(self):
        factory, playwright = fake_playwright()
        browser_obj, context, page = chain(playwright)
        with mock.patch.object(browser_engine, "sync_playwright", factory):
            with self.assertRaises(ValueError):
                with browser_engine.browser(state_path=str(self.state)):
                    raise ValueError("boom")
        page.close.assert_called_once_with()
        context.close.assert_called_once_with()
        browser_obj.close.assert_called_once_with()
        playwright.stop.assert_called_once_with()

    def test_teardown_survives_a_close_that_raises(self):
        factory, playwright = fake_playwright()
        browser_obj, context, page = chain(playwright)
        page.close.side_effect = RuntimeError("already gone")
        with mock.patch.object(browser_engine, "sync_playwright", factory):
            with browser_engine.browser(state_path=str(self.state)):
                pass
        context.close.assert_called_once_with()
        browser_obj.close.assert_called_once_with()
        playwright.stop.assert_called_once_with()


class IsLoggedInTest(unittest.TestCase):
    @staticmethod
    def _page(cookies=()):
        page = mock.MagicMock()
        page.context.cookies.return_value = list(cookies)
        return page

    def test_true_when_an_auth_cookie_is_present(self):
        page = self._page([{"name": "oauth_token", "value": "abc123"}])
        self.assertTrue(browser_engine.is_logged_in(page))

    def test_a_signed_in_looking_header_is_not_enough_on_its_own(self):
        # The logged-out homepage renders the same header shell, so trusting the
        # DOM here silently saved an anonymous session once. Only a token counts.
        page = self._page()
        # assertLogs would depend on global handler state, which another module
        # in this suite reconfigures; patch the logger instead.
        with mock.patch.object(browser_engine.log, "warning") as warned:
            self.assertFalse(browser_engine.is_logged_in(page))
        self.assertTrue(warned.called, "a missing auth cookie should warn")
        self.assertIn("oauth_token", " ".join(str(a) for a in warned.call_args[0]))

    def test_no_cookie_and_no_marker_is_false(self):
        page = self._page()
        page.wait_for_selector.side_effect = RuntimeError("timeout")
        self.assertFalse(browser_engine.is_logged_in(page))

    def test_an_empty_cookie_value_does_not_count(self):
        page = self._page([{"name": "oauth_token", "value": ""}])
        page.wait_for_selector.side_effect = RuntimeError("timeout")
        self.assertFalse(browser_engine.is_logged_in(page))

    def test_tracking_cookies_alone_are_not_auth(self):
        page = self._page(
            [
                {"name": "sc_anonymous_id", "value": "x" * 27},
                {"name": "_ga", "value": "GA1.1"},
            ]
        )
        page.wait_for_selector.side_effect = RuntimeError("timeout")
        self.assertFalse(browser_engine.is_logged_in(page))

    def test_cookie_lookup_failure_is_not_fatal(self):
        page = mock.MagicMock()
        page.context.cookies.side_effect = RuntimeError("context gone")
        page.wait_for_selector.side_effect = RuntimeError("timeout")
        self.assertFalse(browser_engine.is_logged_in(page))

    def test_false_instead_of_raising_when_page_errors(self):
        page = mock.MagicMock()
        page.wait_for_selector.side_effect = RuntimeError("target closed")
        self.assertFalse(browser_engine.is_logged_in(page))

    def test_false_when_a_sign_in_control_is_present(self):
        page = mock.MagicMock()

        def wait(selector, **kwargs):
            if selector in browser_engine.LOGGED_IN_SELECTORS:
                raise RuntimeError("timeout")
            return mock.MagicMock()

        page.wait_for_selector.side_effect = wait
        self.assertFalse(browser_engine.is_logged_in(page))

    def test_never_navigates_to_check(self):
        page = mock.MagicMock()
        browser_engine.is_logged_in(page)
        page.goto.assert_not_called()


class LoginTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "storage_state.json"

    def _writes_state(self, path):
        Path(path).write_text('{"cookies": []}')

    def test_saves_state_with_owner_only_permissions(self):
        factory, playwright = fake_playwright()
        _, context, _ = chain(playwright)
        context.storage_state.side_effect = lambda path: self._writes_state(path)
        with (
            mock.patch.object(browser_engine, "sync_playwright", factory),
            mock.patch.object(browser_engine, "is_logged_in", return_value=True),
        ):
            result = browser_engine.login(state_path=str(self.state), timeout=5)
        self.assertEqual(result, self.state)
        self.assertTrue(self.state.exists())
        self.assertEqual(os.stat(str(self.state)).st_mode & 0o777, 0o600)

    def test_launches_headed(self):
        factory, playwright = fake_playwright()
        _, context, _ = chain(playwright)
        context.storage_state.side_effect = lambda path: self._writes_state(path)
        with (
            mock.patch.object(browser_engine, "sync_playwright", factory),
            mock.patch.object(browser_engine, "is_logged_in", return_value=True),
        ):
            browser_engine.login(state_path=str(self.state), timeout=5)
        self.assertFalse(playwright.chromium.launch.call_args[1]["headless"])

    def test_never_types_credentials(self):
        factory, playwright = fake_playwright()
        _, context, page = chain(playwright)
        context.storage_state.side_effect = lambda path: self._writes_state(path)
        with (
            mock.patch.object(browser_engine, "sync_playwright", factory),
            mock.patch.object(browser_engine, "is_logged_in", return_value=True),
        ):
            browser_engine.login(state_path=str(self.state), timeout=5)
        page.fill.assert_not_called()
        page.type.assert_not_called()

    def test_timeout_raises_and_tears_down(self):
        factory, playwright = fake_playwright()
        browser_obj, context, page = chain(playwright)
        with (
            mock.patch.object(browser_engine, "sync_playwright", factory),
            mock.patch.object(browser_engine, "is_logged_in", return_value=False),
            mock.patch.object(browser_engine.time, "sleep") as sleep,
        ):
            with self.assertRaises(browser_engine.NotLoggedInError) as caught:
                browser_engine.login(state_path=str(self.state), timeout=0)
        self.assertIn("--login", str(caught.exception))
        self.assertFalse(self.state.exists())
        sleep.assert_not_called()
        page.close.assert_called_once_with()
        context.close.assert_called_once_with()
        browser_obj.close.assert_called_once_with()
        playwright.stop.assert_called_once_with()

    def test_polls_until_the_user_gets_through(self):
        factory, playwright = fake_playwright()
        _, context, _ = chain(playwright)
        context.storage_state.side_effect = lambda path: self._writes_state(path)
        answers = [False, False, True]
        with (
            mock.patch.object(browser_engine, "sync_playwright", factory),
            mock.patch.object(browser_engine, "is_logged_in", side_effect=answers),
            mock.patch.object(browser_engine.time, "sleep") as sleep,
        ):
            browser_engine.login(state_path=str(self.state), timeout=60)
        self.assertEqual(sleep.call_count, 2)


class ContractTest(unittest.TestCase):
    def test_blocked_types_exclude_the_spa_essentials(self):
        for resource_type in ("document", "script", "xhr", "fetch", "websocket"):
            self.assertNotIn(resource_type, browser_engine.BLOCKED_RESOURCE_TYPES)

    def test_public_api_is_importable(self):
        for name in browser_engine.__all__:
            self.assertTrue(hasattr(browser_engine, name), name)


if __name__ == "__main__":
    unittest.main()
