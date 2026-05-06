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
* Generation **fills the dashboard form and clicks the real "Buat Link"
  button**, then captures the GraphQL response off the network. Earlier
  revisions tried calling the endpoint directly via ``page.evaluate`` —
  Shopee's anti-bot SAP layer fingerprints fetches that don't originate
  from a real click handler and either drops them outright or returns
  ``error 90309999`` with a CAPTCHA challenge. Driving the visible form
  uses Shopee's own React handlers, which is what the SAP layer trusts.

The module degrades gracefully when Playwright (or Chrome) is not
installed: ``ensure_browser_available()`` returns a clear error message
the GUI can surface to the user.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
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


def _chunks(items: List[LinkInput], size: int) -> Iterable[List[LinkInput]]:
    """Yield successive ``size``-element slices of ``items``.

    Shopee's dashboard textarea allows up to 5 URLs per submit, so we
    chunk the input to match — submitting more than that would fail
    client-side validation in the form anyway.
    """
    if size <= 0:
        size = 1
    for i in range(0, len(items), size):
        yield items[i : i + size]


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
        """Drive the dashboard form and capture its GraphQL response.

        Earlier revisions called the GraphQL endpoint directly with
        ``page.evaluate(fetch)``. Shopee's anti-bot SAP layer fingerprints
        that path (the call doesn't originate from a real click handler)
        and either rejects the fetch outright or returns ``error 90309999``
        with a CAPTCHA challenge in the body. Filling the visible form and
        clicking the real "Buat Link" button makes the request go through
        Shopee's own React handlers, exactly like a human user — which is
        what their SAP layer is designed to allow.

        We capture the response off the network (``page.on("response")``)
        instead of scraping the result UI, so we don't depend on Shopee's
        result-list DOM staying stable.
        """
        items = list(inputs)
        if not items:
            return []
        if self._page is None:
            raise AffiliateError("BrowserSession belum di-start().")

        self._goto_dashboard_if_needed()
        results: List[LinkResult] = []
        # Dashboard textarea hint says "s/d 5 link" — chunk to match.
        for chunk in _chunks(items, 5):
            results.extend(self._submit_chunk(chunk, timeout_ms))
        return results

    def _goto_dashboard_if_needed(self) -> None:
        try:
            current = self._page.url  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            current = ""
        on_dashboard = (
            "affiliate.shopee.co.id" in current
            and "/offer/custom_link" in current
            and "/login" not in current
            and "/verify/captcha" not in current
        )
        if not on_dashboard:
            try:
                self._page.goto(DASHBOARD_URL, wait_until="domcontentloaded")  # type: ignore[union-attr]
            except Exception as e:  # noqa: BLE001
                raise AffiliateError(
                    "Gagal membuka halaman dashboard affiliate. Sesi "
                    f"mungkin sudah kedaluwarsa.\nDetail: {e}"
                )
            if not self._dashboard_loaded():
                raise AffiliateError(
                    "Anda belum login ke affiliate.shopee.co.id. Klik "
                    "'Hubungkan Chrome…' lalu login dulu."
                )
        try:
            self._page.wait_for_load_state("networkidle", timeout=10_000)  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass

    def _submit_chunk(
        self,
        chunk: List[LinkInput],
        timeout_ms: int,
    ) -> List[LinkResult]:
        """Fill the dashboard form for one batch and capture its response."""
        urls = [it.original_link for it in chunk]
        # Per-chunk tags: the dashboard form has 5 tag inputs that apply
        # to every URL in the batch, so we use the first item's tags.
        tags: Dict[int, str] = {}
        sub_ids = chunk[0].sub_ids if chunk else ()
        for i, value in enumerate(sub_ids, start=1):
            if value:
                tags[i] = str(value)

        # Set up the response capture before we click anything, so we
        # don't miss the response if it lands faster than we expect.
        captured: List[Dict[str, Any]] = []

        def _on_response(response: Any) -> None:
            try:
                if "batchCustomLink" not in response.url:
                    return
                try:
                    body: Any = response.json()
                except Exception:  # noqa: BLE001
                    try:
                        body = {"_raw": response.text()[:5000]}
                    except Exception:  # noqa: BLE001
                        body = {"_raw": "(unable to read body)"}
                captured.append(
                    {
                        "url": response.url,
                        "status": int(response.status),
                        "body": body,
                    }
                )
            except Exception:  # noqa: BLE001
                pass

        self._page.on("response", _on_response)  # type: ignore[union-attr]
        try:
            self._fill_form(urls, tags)
            self._click_buat_link()
            deadline = time.time() + (timeout_ms / 1000.0)
            while time.time() < deadline and not captured:
                # Short-circuit if Shopee redirects to its CAPTCHA challenge
                # — at that point no GraphQL response will ever arrive.
                try:
                    cur = self._page.url or ""  # type: ignore[union-attr]
                except Exception:  # noqa: BLE001
                    cur = ""
                if "/verify/captcha" in cur:
                    raise AffiliateError(
                        "Shopee meminta verifikasi CAPTCHA.\n\n"
                        "Selesaikan puzzle di window Chrome yang muncul "
                        "(geser slider sampai pas), lalu Anda akan kembali "
                        "ke dashboard. Klik Generate lagi setelah itu.\n\n"
                        "Tip: kalau CAPTCHA terus muncul, coba browse-"
                        "browse dulu di dashboard sebentar (klik menu, "
                        "scroll halaman) sebelum klik Generate — Shopee "
                        "lebih percaya kalau ada aktivitas user dulu."
                    )
                try:
                    self._page.wait_for_timeout(200)  # type: ignore[union-attr]
                except Exception:  # noqa: BLE001
                    time.sleep(0.2)
            if not captured:
                raise AffiliateError(
                    f"Tidak ada response dari Shopee dalam "
                    f"{timeout_ms / 1000:.0f} detik. Coba klik Generate "
                    "ulang. Kalau tetap kosong, refresh halaman Chrome "
                    "(F5) dan coba lagi."
                )
        finally:
            try:
                self._page.remove_listener("response", _on_response)  # type: ignore[union-attr]
            except Exception:  # noqa: BLE001
                pass

        last = captured[-1]
        return self._parse_chunk_response(chunk, last)

    def _fill_form(self, urls: List[str], tags: Dict[int, str]) -> None:
        """Fill the URL textarea and the (up to) 5 tag inputs."""
        page = self._page  # type: ignore[assignment]
        textarea = page.locator("textarea").first
        try:
            textarea.wait_for(state="visible", timeout=15_000)
        except Exception as e:  # noqa: BLE001
            raise AffiliateError(
                "Tidak menemukan kolom URL di dashboard. Pastikan halaman "
                "yang terbuka adalah affiliate.shopee.co.id/offer/custom_link "
                "dan Anda sudah login.\n\nDetail: " + str(e)
            )
        textarea.click()
        try:
            textarea.fill("")
        except Exception:  # noqa: BLE001
            pass
        textarea.fill("\n".join(urls))

        tag_inputs = page.locator('input[placeholder*="Contoh"]')
        try:
            count = tag_inputs.count()
        except Exception:  # noqa: BLE001
            count = 0
        for i in range(min(count, 5)):
            value = tags.get(i + 1, "")
            inp = tag_inputs.nth(i)
            try:
                inp.click()
                inp.fill(value)
            except Exception:  # noqa: BLE001
                # Tag inputs are optional — if one fails to fill we just
                # carry on; the user can retry.
                pass

    def _click_buat_link(self) -> None:
        page = self._page  # type: ignore[assignment]
        # Try a couple of selectors so we tolerate small UI changes.
        selectors = [
            ('role="button" name=/buat\\s*link/i', lambda p: p.get_by_role(
                "button", name=re.compile(r"buat\s*link", re.I)
            )),
            ('button:has-text("Buat Link")', lambda p: p.locator(
                'button:has-text("Buat Link")'
            )),
        ]
        last_err: Optional[Exception] = None
        for desc, fn in selectors:
            try:
                btn = fn(page).first
                btn.wait_for(state="visible", timeout=8_000)
                btn.click()
                return
            except Exception as e:  # noqa: BLE001
                last_err = e
                continue
        raise AffiliateError(
            "Tombol 'Buat Link' tidak ditemukan di dashboard.\n\n"
            "Detail: " + (str(last_err) if last_err else "tidak diketahui")
        )

    def _parse_chunk_response(
        self,
        chunk: List[LinkInput],
        captured: Dict[str, Any],
    ) -> List[LinkResult]:
        status = int(captured.get("status") or 0)
        body = captured.get("body")
        debug_path = _save_debug(
            build_payload(chunk),
            status,
            json.dumps(body, ensure_ascii=False)[:8000]
            if isinstance(body, dict)
            else str(body),
        )

        if status >= 400:
            raise AffiliateError(
                f"HTTP {status} dari Shopee.\n\n"
                "Sesi browser mungkin kedaluwarsa — klik 'Hubungkan "
                "Chrome…' dan login ulang.\n\n"
                f"Response sample:\n{json.dumps(body, ensure_ascii=False)[:500]}"
                f"\n\n(disimpan di {debug_path})"
            )
        if not isinstance(body, dict):
            raise AffiliateError(
                "Response Shopee tidak bisa di-parse.\n\n"
                f"Response sample:\n{str(body)[:500]}\n\n"
                f"(disimpan di {debug_path})"
            )

        # Anti-bot rejection format (see Shopee SAP layer):
        # {"error": 90309999, "data": {"batchCustomLink": null}, ...}
        err_code = body.get("error")
        if isinstance(err_code, int) and err_code != 0:
            raise AffiliateError(
                f"Shopee menolak request (anti-bot, error {err_code}).\n\n"
                "Coba langkah ini:\n"
                "  1. Di window Chrome yang terbuka, browse-browse "
                "sebentar — klik menu, scroll, lihat-lihat. Risk engine "
                "Shopee lebih percaya session yang aktif.\n"
                "  2. Tunggu 1-2 menit, lalu klik Generate lagi.\n"
                "  3. Kalau masih, klik 'Hubungkan Chrome…' lagi → "
                "login ulang dengan Reset profile dulu.\n\n"
                f"Response sample:\n{json.dumps(body, ensure_ascii=False)[:500]}"
                f"\n\n(disimpan di {debug_path})"
            )
        if body.get("errors"):
            msgs = "; ".join(
                str(err.get("message", err)) for err in body["errors"]
            )
            raise AffiliateError(f"GraphQL errors: {msgs}")

        batch = ((body or {}).get("data") or {}).get("batchCustomLink") or []
        if not batch:
            raise AffiliateError(
                "Shopee mengembalikan response sukses tapi data kosong. "
                "Coba klik 'Hubungkan Chrome…' → Reset profile → login "
                "ulang, lalu Generate ulang.\n\n"
                f"Response sample:\n{json.dumps(body, ensure_ascii=False)[:500]}"
                f"\n\n(disimpan di {debug_path})"
            )
        return _build_results(chunk, batch)


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
