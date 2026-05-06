#!/usr/bin/env python3
"""Browser-driven Shopee Affiliate short-link generator.

This module replaces the cookie-replay strategy in ``affiliate.py``: instead
of trying to forward captured headers (which Shopee's anti-bot SDK rejects),
we drive the user's installed Chrome and call the ``batchCustomLink``
GraphQL endpoint **from inside the loaded affiliate dashboard page**. The
browser's own JavaScript runtime computes the ``X-Sap-Sec`` HMAC
signature for us, so Shopee accepts the request.

Key design decisions:

* We do **not** call ``playwright.chromium.launch_persistent_context``.
  Playwright's launcher adds several automation flags (``--enable-automation``
  among them) that Shopee's anti-bot system fingerprints and routes to a
  CAPTCHA challenge that itself refuses to render to detected automation.
  So we ``subprocess.Popen`` the user's installed Chrome **ourselves** with
  only ``--remote-debugging-port`` and ``--user-data-dir``, then attach
  Playwright via ``connect_over_cdp``. Chrome looks identical to one a
  human just double-clicked — no infobar, no ``navigator.webdriver``.
* The browser profile lives in ``<user_data_dir>/chrome-profile`` — fully
  separate from the user's personal Chrome profile.
* The user logs in **once** in the launched window. The persistent profile
  keeps them logged in for subsequent ``generate()`` calls.
* Generation runs the fetch inside the page context via ``page.evaluate``,
  so the request is indistinguishable from one made by the dashboard form.

The module degrades gracefully when Playwright (or Chrome) is not
installed: ``ensure_browser_available()`` returns a clear error message
the GUI can surface to the user.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

from affiliate import (  # re-export the small types so callers only import one module
    AffiliateError,
    LinkInput,
    LinkResult,
    SOURCE_CALLER,
    build_payload,
    user_data_dir,
)

DASHBOARD_URL = "https://affiliate.shopee.co.id/offer/custom_link"
LOGIN_URL = "https://affiliate.shopee.co.id"
DEFAULT_LAUNCH_TIMEOUT_S = 30
DEFAULT_NAV_TIMEOUT_MS = 30_000
DEFAULT_FETCH_TIMEOUT_MS = 60_000

# Re-exported for the GUI.
__all__ = [
    "AffiliateError",
    "LinkInput",
    "LinkResult",
    "BrowserSession",
    "ensure_browser_available",
    "chrome_profile_dir",
    "reset_profile",
    "profile_summary",
]


def chrome_profile_dir() -> str:
    """Return the persistent Chrome profile directory (created if missing)."""
    path = os.path.join(user_data_dir(), "chrome-profile")
    os.makedirs(path, exist_ok=True)
    return path


def find_chrome_executable() -> Optional[str]:
    """Locate the user's installed Chrome (or an env override).

    Order:

    1. ``SHOPEELINK_CHROME_PATH`` environment variable (escape hatch for
       non-standard installations).
    2. Per-OS standard installation paths.
    3. ``shutil.which`` for ``chrome`` / ``google-chrome`` / etc.

    Returns ``None`` if Chrome cannot be located.
    """
    env = os.environ.get("SHOPEELINK_CHROME_PATH")
    if env and os.path.isfile(env):
        return env
    if sys.platform == "win32":
        candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        ]
    elif sys.platform == "darwin":
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            os.path.expanduser(
                "~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
            ),
        ]
    else:
        candidates = [
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/snap/bin/google-chrome",
            "/snap/bin/chromium",
        ]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    for name in ("chrome", "google-chrome", "google-chrome-stable", "chromium"):
        path = shutil.which(name)
        if path:
            return path
    return None


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def ensure_browser_available() -> Tuple[bool, str]:
    """Check whether Playwright + Chrome are available.

    Returns ``(ok, message)``. ``message`` is empty on success; otherwise it
    contains a user-facing explanation of how to install the missing piece.
    """
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False, (
            "Library 'playwright' belum terinstal.\n\n"
            "Solusi:\n"
            "  • Kalau Anda jalan dari source: tutup aplikasi, hapus folder "
            "'.venv', lalu klik 'run.bat' / 'run.sh' lagi (auto-install).\n"
            "  • Kalau dari .exe: download .exe versi terbaru "
            "(v1.3.2 ke atas)."
        )
    if find_chrome_executable() is None:
        return False, (
            "Google Chrome tidak ditemukan di komputer Anda.\n\n"
            "Install Chrome dari https://www.google.com/chrome/ lalu coba "
            "lagi. Kalau Chrome terinstal di lokasi non-standar, set "
            "environment variable SHOPEELINK_CHROME_PATH ke path lengkap "
            "chrome.exe sebelum membuka aplikasi."
        )
    return True, ""


@dataclass
class _GenerateOutcome:
    """Internal: result of running the in-page fetch."""

    status: int
    body: str
    needs_login: bool


# JavaScript executed inside the dashboard page. Calls the same GraphQL
# endpoint the form would, but with our payload. Returns the raw response so
# the Python side can parse it.
_FETCH_SCRIPT = r"""
async (payload) => {
    const csrf = (() => {
        const m = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
        return m ? decodeURIComponent(m[1]) : null;
    })();
    const url = "https://affiliate.shopee.co.id/api/v3/gql?q=batchCustomLink";
    const headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "Accept": "application/json, text/plain, */*",
    };
    if (csrf) headers["Csrf-Token"] = csrf;
    const resp = await fetch(url, {
        method: "POST",
        credentials: "include",
        headers,
        body: JSON.stringify(payload),
    });
    let body;
    try {
        body = await resp.text();
    } catch (e) {
        body = "(unable to read body: " + e + ")";
    }
    return { status: resp.status, body };
}
"""


class BrowserSession:
    """Persistent Chrome instance, used to call the dashboard's API.

    Typical lifecycle:

    1. ``with BrowserSession() as bs:`` — starts Playwright + Chrome.
    2. ``bs.ensure_logged_in(open_window=True)`` — opens a visible window
       and blocks until the user has logged in (if not already).
    3. ``results = bs.generate(inputs)`` — runs the in-page fetch and
       returns ``LinkResult`` per input.
    4. Exit the ``with`` block to close Chrome and stop Playwright.
    """

    def __init__(
        self,
        profile_dir: Optional[str] = None,
        headless: bool = False,
        chrome_path: Optional[str] = None,
    ):
        self.profile_dir = profile_dir or chrome_profile_dir()
        self.headless = headless
        self.chrome_path = chrome_path
        self._proc: Optional[subprocess.Popen] = None
        self._pw = None
        self._browser = None  # Browser (from connect_over_cdp)
        self._ctx = None  # BrowserContext
        self._page = None  # Page
        self._cdp_port: Optional[int] = None

    # ----- lifecycle ----------------------------------------------------

    def __enter__(self) -> "BrowserSession":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def start(self) -> None:
        """Launch Chrome as a subprocess and attach Playwright via CDP.

        We deliberately avoid ``playwright.chromium.launch_persistent_context``
        because Playwright's launcher injects automation flags that Shopee's
        anti-bot system fingerprints. Instead we ``Popen`` Chrome ourselves
        with only ``--remote-debugging-port`` and ``--user-data-dir``, then
        attach via ``connect_over_cdp`` — Chrome looks identical to one a
        user just opened by hand (no infobar, no ``navigator.webdriver``).
        """
        if self._page is not None:
            return
        chrome_exe = self.chrome_path or find_chrome_executable()
        if not chrome_exe:
            raise AffiliateError(
                "Google Chrome tidak ditemukan. Install dari "
                "https://www.google.com/chrome/ lalu coba lagi.\n\n"
                "Kalau Chrome terinstal di lokasi non-standar, set "
                "environment variable SHOPEELINK_CHROME_PATH ke path "
                "lengkap chrome.exe sebelum membuka aplikasi."
            )
        port = _find_free_port()
        cmd = [
            chrome_exe,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={self.profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            # Suppress some Chrome features that delay first paint.
            "--disable-features=Translate,OptimizationHints",
        ]
        if self.headless:
            # Modern headless mode (--headless=new) uses the same renderer
            # as headed Chrome, so anti-bot fingerprints stay consistent.
            cmd.append("--headless=new")
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                # On Windows, prevent Ctrl+C in our Python from killing Chrome.
                creationflags=(
                    subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
                    if sys.platform == "win32"
                    else 0
                ),
            )
        except OSError as e:
            self._proc = None
            raise AffiliateError(
                f"Gagal meluncurkan Chrome.\n\nDetail: {e}\n\n"
                "Pastikan Google Chrome sudah terinstal."
            ) from e
        self._cdp_port = port
        # Poll the CDP HTTP endpoint until Chrome is ready (or it dies).
        ready_url = f"http://127.0.0.1:{port}/json/version"
        deadline = time.time() + DEFAULT_LAUNCH_TIMEOUT_S
        while time.time() < deadline:
            if self._proc.poll() is not None:
                self._proc = None
                raise AffiliateError(
                    "Chrome langsung keluar saat dijalankan. Mungkin ada "
                    "window Chrome lain yang memakai folder profile yang "
                    "sama. Tutup window Chrome shopeelink yang lain dan "
                    "coba lagi."
                )
            try:
                with urllib.request.urlopen(ready_url, timeout=1):
                    break
            except Exception:  # noqa: BLE001
                time.sleep(0.3)
        else:
            self._kill_proc()
            raise AffiliateError("Chrome tidak siap dalam 30 detik.")
        try:
            from playwright.sync_api import sync_playwright  # type: ignore[import-not-found]
        except ImportError as e:
            self._kill_proc()
            raise AffiliateError(
                "Library 'playwright' tidak ditemukan. Install dulu via "
                "`pip install playwright` lalu coba lagi."
            ) from e
        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.connect_over_cdp(
                f"http://127.0.0.1:{port}"
            )
        except Exception as e:  # noqa: BLE001
            self._stop_pw()
            self._kill_proc()
            raise AffiliateError(
                f"Gagal connect ke Chrome via CDP.\n\nDetail: {e}"
            ) from e
        # Re-use the default browser context (the persistent profile).
        if self._browser.contexts:
            self._ctx = self._browser.contexts[0]
        else:
            self._ctx = self._browser.new_context()
        if self._ctx.pages:
            self._page = self._ctx.pages[0]
        else:
            self._page = self._ctx.new_page()
        try:
            self._page.set_default_navigation_timeout(DEFAULT_NAV_TIMEOUT_MS)
            self._page.set_default_timeout(DEFAULT_NAV_TIMEOUT_MS)
        except Exception:  # noqa: BLE001
            pass

    def close(self) -> None:
        # Disconnect Playwright first. For ``connect_over_cdp`` browsers
        # this is a no-op on the Chrome side (Chrome stays alive), so we
        # still need to kill the subprocess afterwards.
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:  # noqa: BLE001
                pass
        self._browser = None
        self._ctx = None
        self._page = None
        self._stop_pw()
        self._kill_proc()
        self._cdp_port = None

    def _stop_pw(self) -> None:
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:  # noqa: BLE001
                pass
            self._pw = None

    def _kill_proc(self) -> None:
        """Stop the Chrome subprocess, gracefully if possible.

        Chrome only flushes cookies/localStorage to the persistent profile
        on graceful shutdown, so we try ``terminate``/``taskkill`` (no
        ``/F``) first and only force-kill on timeout.
        """
        if self._proc is None:
            return
        try:
            if sys.platform == "win32":
                # taskkill without /F sends WM_CLOSE — Chrome handles this
                # like the user pressed the X button (cookies are saved).
                subprocess.run(
                    ["taskkill", "/pid", str(self._proc.pid), "/T"],
                    capture_output=True,
                    timeout=5,
                )
            else:
                self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                if sys.platform == "win32":
                    subprocess.run(
                        ["taskkill", "/pid", str(self._proc.pid), "/T", "/F"],
                        capture_output=True,
                        timeout=5,
                    )
                else:
                    self._proc.kill()
                try:
                    self._proc.wait(timeout=5)
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass
        self._proc = None

    # ----- login --------------------------------------------------------

    def is_logged_in(self) -> bool:
        """Return True if the current profile is logged in to affiliate.shopee.co.id."""
        if self._page is None:
            raise AffiliateError("BrowserSession belum di-start().")
        try:
            self._page.goto(DASHBOARD_URL, wait_until="domcontentloaded")
        except Exception:  # noqa: BLE001
            # Even if navigation fails (e.g. partial load), check the URL
            # we ended up on; Shopee redirects unauthenticated users to the
            # login page.
            pass
        return self._dashboard_loaded()

    def _dashboard_loaded(self) -> bool:
        """Return True only if the page is *actually* on the dashboard.

        We check both the URL **and** the DOM, because Shopee's dashboard
        redirect chain looks like:

        1. ``GET /offer/custom_link`` → 302 to ``affiliate.shopee.co.id`` SPA.
        2. SPA boots; sees no auth cookie; client-side ``location.replace``
           to ``shopee.co.id/buyer/login?next=…``.

        Between steps 1 and 2 the URL is still on ``affiliate.shopee.co.id``
        even though we're not really logged in. Polling the URL alone leads
        to a false positive that closes Chrome before the user can finish
        logging in (see github.com/pakdosen/shopeelink#7 follow-up). So we
        also reject when the live DOM contains a login form.
        """
        try:
            url = self._page.url  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            return False
        if not url:
            return False
        # Hard reject any login-flow URL Shopee may redirect through.
        bad_markers = (
            "/login",
            "buyer-login",
            "redirect_url=",
            "creatorhub",
            "shopee.co.id/buyer",
        )
        if any(m in url for m in bad_markers):
            return False
        if "affiliate.shopee.co.id" not in url:
            return False
        # URL says affiliate dashboard, but the SPA may still be in the
        # middle of the redirect to login. Probe the DOM: if there's a
        # password input visible we're NOT logged in.
        try:
            has_login_form = self._page.evaluate(  # type: ignore[union-attr]
                "() => !!document.querySelector('input[type=\"password\"]')"
            )
        except Exception:  # noqa: BLE001
            has_login_form = False
        if has_login_form:
            return False
        return True

    def wait_for_login(self, poll_interval_s: float = 1.5, timeout_s: float = 600) -> bool:
        """Poll until the user has logged in, or ``timeout_s`` elapses.

        Designed to be called from a worker thread while the user interacts
        with the visible browser window.
        """
        if self._page is None:
            raise AffiliateError("BrowserSession belum di-start().")

        # Send the user to the dashboard. If they aren't logged in Shopee
        # redirects to the login page automatically. Wait until the network
        # actually settles so any client-side redirect to /buyer/login has
        # had a chance to fire before we sample the URL for the first time.
        try:
            self._page.goto(DASHBOARD_URL, wait_until="domcontentloaded")
        except Exception:  # noqa: BLE001
            pass
        try:
            self._page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:  # noqa: BLE001
            pass

        deadline = time.monotonic() + timeout_s
        # Require two consecutive positive samples (≈ 3s apart) to declare
        # login. That's another guard against catching the dashboard URL
        # briefly between SPA boot and the JS redirect to login.
        positive_samples = 0
        while time.monotonic() < deadline:
            if self._dashboard_loaded():
                positive_samples += 1
                if positive_samples >= 2:
                    # Give the SPA a moment to fully boot the SDK that
                    # signs requests; otherwise the first generate may
                    # still be rejected.
                    try:
                        self._page.wait_for_load_state("networkidle", timeout=8_000)
                    except Exception:  # noqa: BLE001
                        pass
                    return True
            else:
                positive_samples = 0
            time.sleep(poll_interval_s)
        return False

    # ----- generation ---------------------------------------------------

    def generate(
        self,
        inputs: Iterable[LinkInput],
        timeout_ms: int = DEFAULT_FETCH_TIMEOUT_MS,
    ) -> List[LinkResult]:
        """Send a batch request from the dashboard page and return results."""
        items = list(inputs)
        if not items:
            return []
        if self._page is None:
            raise AffiliateError("BrowserSession belum di-start().")

        # Make sure we're on the dashboard so the in-page SDK is loaded.
        try:
            current = self._page.url
        except Exception:  # noqa: BLE001
            current = ""
        if "affiliate.shopee.co.id" not in current or "/login" in current:
            try:
                self._page.goto(DASHBOARD_URL, wait_until="domcontentloaded")
            except Exception as e:  # noqa: BLE001
                raise AffiliateError(
                    "Gagal membuka halaman dashboard affiliate. Sesi mungkin "
                    f"sudah kedaluwarsa.\nDetail: {e}"
                )
            if not self._dashboard_loaded():
                raise AffiliateError(
                    "Anda belum login ke affiliate.shopee.co.id. Klik "
                    "'Hubungkan Chrome…' lalu login dulu."
                )
            try:
                self._page.wait_for_load_state("networkidle", timeout=8_000)
            except Exception:  # noqa: BLE001
                pass

        payload = build_payload(items)
        # Run the fetch in the page context so Shopee's anti-bot SDK can
        # inject X-Sap-Sec and friends. The script returns the raw status
        # and body; we parse on the Python side so error messages are
        # consistent with the cookie-replay code path.
        try:
            outcome: Dict[str, Any] = self._page.evaluate(
                _FETCH_SCRIPT,
                payload,
            )
        except Exception as e:  # noqa: BLE001
            raise AffiliateError(
                "Gagal menjalankan request di halaman dashboard.\n"
                f"Detail: {e}"
            )

        status = int(outcome.get("status") or 0)
        body = str(outcome.get("body") or "")
        debug_path = _save_debug(payload, status, body)

        if status >= 400:
            raise AffiliateError(
                f"HTTP {status} dari Shopee.\n\n"
                "Sesi browser mungkin kedaluwarsa — klik 'Hubungkan "
                "Chrome…' dan login ulang.\n\n"
                f"Response sample:\n{body[:500] or '(empty body)'}\n\n"
                f"(disimpan di {debug_path})"
            )

        try:
            data = json.loads(body) if body else None
        except json.JSONDecodeError:
            raise AffiliateError(
                "Response Shopee bukan JSON valid.\n\n"
                f"Response sample:\n{body[:500] or '(empty body)'}\n\n"
                f"(disimpan di {debug_path})"
            )

        if isinstance(data, dict) and data.get("errors"):
            msgs = "; ".join(
                str(err.get("message", err)) for err in data["errors"]
            )
            raise AffiliateError(f"GraphQL errors: {msgs}")

        batch = ((data or {}).get("data") or {}).get("batchCustomLink") or []
        if not batch:
            raise AffiliateError(
                "Shopee mengembalikan response sukses tapi data kosong. "
                "Coba klik 'Hubungkan Chrome…' lalu refresh halaman, "
                "kemudian Generate ulang.\n\n"
                f"Response sample:\n{body[:500] or '(empty body)'}\n\n"
                f"(disimpan di {debug_path})"
            )

        return _build_results(items, batch)


def _save_debug(payload: Dict[str, Any], status: int, body: str) -> str:
    """Save last response (no cookies leak — they aren't in the payload)."""
    record = {
        "request": {
            "url": "https://affiliate.shopee.co.id/api/v3/gql?q=batchCustomLink",
            "payload": payload,
            "via": "browser",
        },
        "response": {"status": status, "body": body[:8000]},
    }
    path = os.path.join(user_data_dir(), "last_response.json")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f, indent=2, ensure_ascii=False)
    except OSError:
        pass
    return path


def _build_results(items: List[LinkInput], batch: List[Any]) -> List[LinkResult]:
    results: List[LinkResult] = []
    for item, raw_result in zip(items, batch):
        if not isinstance(raw_result, dict):
            results.append(
                LinkResult(
                    original_link=item.original_link,
                    error="Empty response item",
                )
            )
            continue
        results.append(
            LinkResult(
                original_link=item.original_link,
                short_link=raw_result.get("shortLink"),
                long_link=raw_result.get("longLink"),
                fail_code=int(raw_result.get("failCode") or 0),
            )
        )
    for missing in items[len(results):]:
        results.append(
            LinkResult(
                original_link=missing.original_link,
                error="Tidak ada hasil dari Shopee untuk link ini.",
            )
        )
    return results


# ---------------------------------------------------------------------------
# Profile management helpers (used by GUI).
# ---------------------------------------------------------------------------


def reset_profile() -> bool:
    """Delete the persistent Chrome profile so the user can log in fresh."""
    path = chrome_profile_dir()
    try:
        shutil.rmtree(path, ignore_errors=False)
    except OSError as e:
        # Profile may not exist yet, or be locked by a running Chrome.
        if not os.path.exists(path):
            return False
        raise AffiliateError(
            "Tidak bisa menghapus profile Chrome — pastikan tidak ada "
            f"window Chrome shopeelink yang masih terbuka.\nDetail: {e}"
        )
    os.makedirs(path, exist_ok=True)
    return True


def profile_summary() -> str:
    """Return a short, human-readable status of the profile."""
    path = chrome_profile_dir()
    has_login_state = os.path.exists(os.path.join(path, "Default", "Cookies")) or \
        os.path.exists(os.path.join(path, "Default", "Local State"))
    if not has_login_state:
        return f"Belum ada profile Chrome — klik 'Hubungkan Chrome…' untuk login."
    return f"Profile tersimpan di {path}."
