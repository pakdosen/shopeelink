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


class _FakePage:
    """Minimal stand-in for ``playwright.sync_api.Page``."""

    def __init__(
        self,
        navigations=None,
        evaluate_result=None,
        evaluate_error=None,
        has_login_form=False,
    ):
        self.url = ""
        self._navigations = list(navigations or [])
        self._evaluate_result = evaluate_result
        self._evaluate_error = evaluate_error
        # Whether the page currently contains an input[type=password] (used
        # by the new DOM-based login detector).
        self.has_login_form = has_login_form
        self.evaluate_calls: list = []

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

    def set_default_navigation_timeout(self, ms):
        pass

    def set_default_timeout(self, ms):
        pass

    # --- evaluate ---------------------------------------------------------

    def evaluate(self, script, payload=None):
        self.evaluate_calls.append((script, payload))
        if self._evaluate_error:
            raise self._evaluate_error
        # The DOM-based login probe is a tiny inline arrow function. If we
        # see it, answer based on ``has_login_form`` instead of returning
        # the canned fetch result.
        if isinstance(script, str) and 'input[type="password"]' in script:
            return self.has_login_form
        return self._evaluate_result


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
    def _bs(self, page: _FakePage) -> ab.BrowserSession:
        bs = ab.BrowserSession.__new__(ab.BrowserSession)
        bs._page = page
        return bs

    def test_generates_links_when_response_ok(self) -> None:
        body = json.dumps(
            {
                "data": {
                    "batchCustomLink": [
                        {
                            "shortLink": "https://s.shopee.co.id/AAA",
                            "longLink": "https://shopee.co.id/universal-link/product/1/2",
                            "failCode": 0,
                        }
                    ]
                }
            }
        )
        page = _FakePage(evaluate_result={"status": 200, "body": body})
        page.url = "https://affiliate.shopee.co.id/offer/custom_link"
        bs = self._bs(page)
        with tempfile.TemporaryDirectory() as d, mock.patch.object(
            ab, "user_data_dir", return_value=d
        ):
            results = bs.generate(
                [affiliate.LinkInput(original_link="https://shopee.co.id/product/1/2")]
            )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].short_link, "https://s.shopee.co.id/AAA")
        self.assertTrue(results[0].ok)
        # The script should have been evaluated once with our payload.
        self.assertEqual(len(page.evaluate_calls), 1)
        _script, payload = page.evaluate_calls[0]
        self.assertEqual(
            payload["variables"]["linkParams"][0]["originalLink"],
            "https://shopee.co.id/product/1/2",
        )

    def test_navigates_to_dashboard_when_not_already_there(self) -> None:
        body = json.dumps({"data": {"batchCustomLink": [{"shortLink": "https://s/X"}]}})
        # Start on a different URL; goto() will set us back to the dashboard.
        page = _FakePage(
            navigations=["https://affiliate.shopee.co.id/offer/custom_link"],
            evaluate_result={"status": 200, "body": body},
        )
        page.url = "about:blank"
        bs = self._bs(page)
        with tempfile.TemporaryDirectory() as d, mock.patch.object(
            ab, "user_data_dir", return_value=d
        ):
            bs.generate([affiliate.LinkInput(original_link="https://shopee.co.id/product/1/2")])
        # We should have navigated.
        self.assertEqual(page.url, "https://affiliate.shopee.co.id/offer/custom_link")

    def test_raises_on_http_error_with_response_sample(self) -> None:
        page = _FakePage(evaluate_result={"status": 403, "body": '{"error":90309999}'})
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

    def test_raises_when_response_not_json(self) -> None:
        page = _FakePage(evaluate_result={"status": 200, "body": "<html>oops</html>"})
        page.url = "https://affiliate.shopee.co.id/offer/custom_link"
        bs = self._bs(page)
        with tempfile.TemporaryDirectory() as d, mock.patch.object(
            ab, "user_data_dir", return_value=d
        ):
            with self.assertRaises(affiliate.AffiliateError) as ctx:
                bs.generate(
                    [affiliate.LinkInput(original_link="https://shopee.co.id/product/1/2")]
                )
        self.assertIn("bukan JSON valid", str(ctx.exception))

    def test_raises_when_batch_empty(self) -> None:
        page = _FakePage(
            evaluate_result={
                "status": 200,
                "body": json.dumps({"data": {"batchCustomLink": []}}),
            }
        )
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
        # call evaluate.
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
        self.assertEqual(page.evaluate_calls, [])  # never even tried fetch


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


if __name__ == "__main__":
    unittest.main()
