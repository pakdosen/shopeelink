"""Unit tests for affiliate_browser (no actual browser required)."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import affiliate
import affiliate_browser as ab


class _FakeResponse:
    """Stand-in for ``playwright.sync_api.Response`` passed to handlers."""

    def __init__(self, url, status, payload):
        self.url = url
        self.status = status
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    def text(self):
        return json.dumps(self._payload)


class _FakeLocator:
    """Stand-in for ``playwright.sync_api.Locator``.

    The fake supports ``first/.nth/.count/.wait_for/.click/.fill`` — the
    handful of Locator methods the production code actually calls. A
    ``button``-kind locator triggers the page's response handler when
    clicked, which is what the dashboard's "Buat Link" button does in
    real life (it fires the GraphQL request that we capture).
    """

    def __init__(self, page, kind, count=1):
        self.page = page
        self.kind = kind
        self._count = count

    @property
    def first(self):
        return self

    def nth(self, i):
        return self

    def count(self):
        return self._count

    def wait_for(self, **kwargs):
        return None

    def click(self, **kwargs):
        if self.kind == "button":
            self.page._fire_response()

    def fill(self, value):
        if self.kind == "textarea":
            self.page.textarea_value = value
        elif self.kind == "tag_input":
            self.page.tag_inputs.append(value)


class _FakePage:
    """Minimal stand-in for ``playwright.sync_api.Page``.

    Supports the surface area the production code touches: ``goto``,
    ``wait_for_load_state``, ``evaluate`` (only for the DOM-based login
    probe), ``locator``, ``get_by_role``, ``on``/``remove_listener``,
    ``wait_for_timeout``. Form clicks fire a synthetic GraphQL response
    so the polling loop in ``_submit_chunk`` exits cleanly.
    """

    def __init__(
        self,
        navigations=None,
        has_login_form=False,
        response_payload=None,
        response_status=200,
        response_url="https://affiliate.shopee.co.id/api/v3/gql?q=batchCustomLink",
        captcha_after_click=False,
    ):
        self.url = ""
        self._navigations = list(navigations or [])
        # Whether the page currently contains an input[type=password] (used
        # by the new DOM-based login detector).
        self.has_login_form = has_login_form
        self.evaluate_calls: list = []
        self.textarea_value = ""
        self.tag_inputs: list = []
        self._listeners: dict = {}
        self._response_payload = response_payload
        self._response_status = response_status
        self._response_url = response_url
        self._captcha_after_click = captcha_after_click
        self._fired = False

    # --- navigation -------------------------------------------------------

    def goto(self, url, wait_until=None):
        # Simulate Shopee redirects: pop the next stub URL we should land on.
        if self._navigations:
            self.url = self._navigations.pop(0)
        else:
            self.url = url
        return None

    def wait_for_load_state(self, state="load", timeout=None):
        return None

    def wait_for_timeout(self, ms):
        return None

    def set_default_navigation_timeout(self, ms):
        pass

    def set_default_timeout(self, ms):
        pass

    # --- evaluate (used only by the login-form DOM probe) ----------------

    def evaluate(self, script, payload=None):
        self.evaluate_calls.append((script, payload))
        if isinstance(script, str) and 'input[type="password"]' in script:
            return self.has_login_form
        return None

    # --- locators ---------------------------------------------------------

    def locator(self, selector):
        if "textarea" in selector:
            return _FakeLocator(self, "textarea")
        if "Contoh" in selector:
            return _FakeLocator(self, "tag_input", count=5)
        # Anything else (including button:has-text(...)) → button locator.
        return _FakeLocator(self, "button")

    def get_by_role(self, role, name=None):
        if role == "button":
            return _FakeLocator(self, "button")
        return _FakeLocator(self, "unknown")

    # --- event listeners --------------------------------------------------

    def on(self, event, handler):
        self._listeners.setdefault(event, []).append(handler)

    def remove_listener(self, event, handler):
        if event in self._listeners:
            try:
                self._listeners[event].remove(handler)
            except ValueError:
                pass

    def _fire_response(self):
        """Simulate Shopee's React code calling the GraphQL endpoint after
        the user clicks "Buat Link"."""
        if self._captcha_after_click:
            self.url = (
                "https://shopee.co.id/verify/captcha?anti_bot_tracking_id=xyz"
            )
            return
        if self._fired or self._response_payload is None:
            return
        self._fired = True
        resp = _FakeResponse(
            self._response_url, self._response_status, self._response_payload
        )
        for handler in list(self._listeners.get("response", [])):
            handler(resp)


class EnsureBrowserAvailableTests(unittest.TestCase):
    def test_returns_false_when_playwright_missing(self) -> None:
        # Simulate ImportError by patching the module loader.
        with mock.patch.dict(sys.modules, {"playwright": None}):
            ok, msg = ab.ensure_browser_available()
        self.assertFalse(ok)
        self.assertIn("playwright", msg.lower())


class ChromeProfileDirTests(unittest.TestCase):
    def test_returns_subdir_of_user_data_dir(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(ab, "user_data_dir", return_value=d):
                path = ab.chrome_profile_dir()
            self.assertEqual(path, os.path.join(d, "chrome-profile"))
            self.assertTrue(os.path.isdir(path))


class DashboardLoadedTests(unittest.TestCase):
    def _bs(self, url: str, has_login_form: bool = False) -> ab.BrowserSession:
        bs = ab.BrowserSession.__new__(ab.BrowserSession)
        bs._page = _FakePage(has_login_form=has_login_form)
        bs._page.url = url
        return bs

    def test_login_page_returns_false(self) -> None:
        for bad in [
            "https://affiliate.shopee.co.id/login",
            "https://shopee.co.id/buyer-login?redirect_url=affiliate",
            "https://accounts.shopee.co.id/login",
            "https://shopee.co.id/buyer/login?next=affiliate",
        ]:
            with self.subTest(bad=bad):
                self.assertFalse(self._bs(bad)._dashboard_loaded())

    def test_dashboard_url_returns_true(self) -> None:
        self.assertTrue(
            self._bs("https://affiliate.shopee.co.id/offer/custom_link")._dashboard_loaded()
        )

    def test_empty_url_returns_false(self) -> None:
        self.assertFalse(self._bs("")._dashboard_loaded())

    def test_dashboard_url_with_login_form_returns_false(self) -> None:
        # Regression for the v1.3.0 bug: the URL momentarily reads as the
        # affiliate dashboard while Shopee's SPA is still client-side
        # redirecting to /buyer/login. A live login form means we're not
        # actually logged in, regardless of the URL.
        self.assertFalse(
            self._bs(
                "https://affiliate.shopee.co.id/offer/custom_link",
                has_login_form=True,
            )._dashboard_loaded()
        )


class GenerateTests(unittest.TestCase):
    """Cover the form-automation path used by ``BrowserSession.generate``.

    The production code fills the dashboard's textarea + tag inputs and
    clicks the real "Buat Link" button. The fake page fires a synthetic
    GraphQL response on click so we can exercise success and failure
    branches without launching a browser.
    """

    def _bs(self, page: _FakePage) -> ab.BrowserSession:
        bs = ab.BrowserSession.__new__(ab.BrowserSession)
        bs._page = page
        return bs

    def test_generates_links_when_response_ok(self) -> None:
        page = _FakePage(
            response_payload={
                "data": {
                    "batchCustomLink": [
                        {
                            "shortLink": "https://s.shopee.co.id/AAA",
                            "longLink": "https://shopee.co.id/universal-link/product/1/2",
                            "failCode": 0,
                        }
                    ]
                }
            },
        )
        page.url = "https://affiliate.shopee.co.id/offer/custom_link"
        bs = self._bs(page)
        with tempfile.TemporaryDirectory() as d, mock.patch.object(
            ab, "user_data_dir", return_value=d
        ):
            results = bs.generate(
                [
                    affiliate.LinkInput(
                        original_link="https://shopee.co.id/product/1/2",
                        sub_ids=("PF1", "", "", "", ""),
                    )
                ]
            )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].short_link, "https://s.shopee.co.id/AAA")
        self.assertTrue(results[0].ok)
        # We filled the textarea with the URL and at least one tag.
        self.assertIn(
            "https://shopee.co.id/product/1/2", page.textarea_value
        )
        self.assertIn("PF1", page.tag_inputs)

    def test_navigates_to_dashboard_when_not_already_there(self) -> None:
        page = _FakePage(
            navigations=["https://affiliate.shopee.co.id/offer/custom_link"],
            response_payload={
                "data": {"batchCustomLink": [{"shortLink": "https://s/X"}]}
            },
        )
        page.url = "about:blank"
        bs = self._bs(page)
        with tempfile.TemporaryDirectory() as d, mock.patch.object(
            ab, "user_data_dir", return_value=d
        ):
            bs.generate(
                [affiliate.LinkInput(original_link="https://shopee.co.id/product/1/2")]
            )
        self.assertEqual(
            page.url, "https://affiliate.shopee.co.id/offer/custom_link"
        )

    def test_raises_on_http_error_with_response_sample(self) -> None:
        page = _FakePage(
            response_payload={"error": 90309999},
            response_status=403,
        )
        page.url = "https://affiliate.shopee.co.id/offer/custom_link"
        bs = self._bs(page)
        with tempfile.TemporaryDirectory() as d, mock.patch.object(
            ab, "user_data_dir", return_value=d
        ):
            with self.assertRaises(affiliate.AffiliateError) as ctx:
                bs.generate(
                    [affiliate.LinkInput(original_link="https://shopee.co.id/product/1/2")]
                )
            self.assertTrue(os.path.exists(os.path.join(d, "last_response.json")))
        self.assertIn("HTTP 403", str(ctx.exception))
        self.assertIn("90309999", str(ctx.exception))

    def test_raises_on_anti_bot_error_code(self) -> None:
        # Shopee's anti-bot rejection format: 200 OK but body contains
        # ``error: 90309999`` plus a CAPTCHA challenge payload.
        page = _FakePage(
            response_payload={
                "error": 90309999,
                "data": {"batchCustomLink": None},
            },
        )
        page.url = "https://affiliate.shopee.co.id/offer/custom_link"
        bs = self._bs(page)
        with tempfile.TemporaryDirectory() as d, mock.patch.object(
            ab, "user_data_dir", return_value=d
        ):
            with self.assertRaises(affiliate.AffiliateError) as ctx:
                bs.generate(
                    [affiliate.LinkInput(original_link="https://shopee.co.id/product/1/2")]
                )
        self.assertIn("anti-bot", str(ctx.exception).lower())
        self.assertIn("90309999", str(ctx.exception))

    def test_raises_on_captcha_redirect(self) -> None:
        page = _FakePage(captcha_after_click=True)
        page.url = "https://affiliate.shopee.co.id/offer/custom_link"
        bs = self._bs(page)
        with self.assertRaises(affiliate.AffiliateError) as ctx:
            bs.generate(
                [affiliate.LinkInput(original_link="https://shopee.co.id/product/1/2")]
            )
        self.assertIn("CAPTCHA", str(ctx.exception))

    def test_raises_when_batch_empty(self) -> None:
        page = _FakePage(response_payload={"data": {"batchCustomLink": []}})
        page.url = "https://affiliate.shopee.co.id/offer/custom_link"
        bs = self._bs(page)
        with tempfile.TemporaryDirectory() as d, mock.patch.object(
            ab, "user_data_dir", return_value=d
        ):
            with self.assertRaises(affiliate.AffiliateError):
                bs.generate(
                    [affiliate.LinkInput(original_link="https://shopee.co.id/product/1/2")]
                )

    def test_raises_when_not_logged_in(self) -> None:
        # goto() lands us on a login URL; generate() should refuse to even
        # touch the form.
        page = _FakePage(
            navigations=["https://affiliate.shopee.co.id/login?redirect_url=..."],
        )
        page.url = "about:blank"
        bs = self._bs(page)
        with self.assertRaises(affiliate.AffiliateError) as ctx:
            bs.generate(
                [affiliate.LinkInput(original_link="https://shopee.co.id/product/1/2")]
            )
        self.assertIn("Hubungkan Chrome", str(ctx.exception))
        # Form was never touched (textarea stays empty).
        self.assertEqual(page.textarea_value, "")


class WaitForLoginTests(unittest.TestCase):
    def test_returns_true_when_dashboard_loads_immediately(self) -> None:
        page = _FakePage(
            navigations=["https://affiliate.shopee.co.id/offer/custom_link"]
        )
        bs = ab.BrowserSession.__new__(ab.BrowserSession)
        bs._page = page
        ok = bs.wait_for_login(poll_interval_s=0.01, timeout_s=1)
        self.assertTrue(ok)

    def test_returns_false_after_timeout(self) -> None:
        page = _FakePage(
            navigations=["https://affiliate.shopee.co.id/login"]
        )
        bs = ab.BrowserSession.__new__(ab.BrowserSession)
        bs._page = page
        ok = bs.wait_for_login(poll_interval_s=0.01, timeout_s=0.05)
        self.assertFalse(ok)

    def test_does_not_return_early_when_login_form_visible(self) -> None:
        # Regression for v1.3.0 race: even though the URL is on the
        # affiliate dashboard, a visible login form means the user hasn't
        # finished logging in. wait_for_login MUST keep polling until
        # timeout in this case (otherwise Chrome closes before the user
        # types their password).
        page = _FakePage(
            navigations=["https://affiliate.shopee.co.id/offer/custom_link"],
            has_login_form=True,
        )
        bs = ab.BrowserSession.__new__(ab.BrowserSession)
        bs._page = page
        ok = bs.wait_for_login(poll_interval_s=0.01, timeout_s=0.05)
        self.assertFalse(ok)


class ProfileSummaryTests(unittest.TestCase):
    def test_says_empty_when_no_default_dir(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(ab, "user_data_dir", return_value=d):
                summary = ab.profile_summary()
        self.assertIn("Belum ada profile", summary)


class FindChromeExecutableTests(unittest.TestCase):
    def test_env_override_takes_priority(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".exe", delete=False) as fake:
            fake_path = fake.name
        try:
            with mock.patch.dict(os.environ, {"SHOPEELINK_CHROME_PATH": fake_path}):
                self.assertEqual(ab.find_chrome_executable(), fake_path)
        finally:
            os.unlink(fake_path)

    def test_returns_none_when_env_var_points_to_missing_file(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"SHOPEELINK_CHROME_PATH": "/definitely/not/a/real/path/chrome.exe"},
        ):
            with mock.patch.object(ab.shutil, "which", return_value=None):
                with mock.patch.object(ab.os.path, "isfile", return_value=False):
                    self.assertIsNone(ab.find_chrome_executable())


class FindFreePortTests(unittest.TestCase):
    def test_returns_an_int_in_valid_range(self) -> None:
        port = ab._find_free_port()
        self.assertIsInstance(port, int)
        self.assertGreater(port, 0)
        self.assertLessEqual(port, 65535)


if __name__ == "__main__":
    unittest.main()
