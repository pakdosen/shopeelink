#!/usr/bin/env python3
"""Shopee Affiliate short-link generator (cookie-replay strategy).

This module replays the same authenticated request that the Shopee Affiliate
dashboard makes when the user clicks "Buat Link" on the Custom Link page:

    POST https://affiliate.shopee.co.id/api/v3/gql?q=batchCustomLink

The endpoint is GraphQL and accepts a *batch* of long URLs (each with up to
five sub-IDs / tags) in a single request. To call it the user must paste their
authenticated request once — typically by copying it from Chrome DevTools as
"Copy as cURL". We extract the cookies, the Csrf-Token header, and Shopee's
anti-bot signature headers (``X-Sap-Ri``, ``X-Sap-Sec``, ``Af-Ac-Enc-*``) and
replay them with new payloads.

No data is sent anywhere except to ``affiliate.shopee.co.id``. The captured
session is stored on disk in the user's per-platform application data
directory.
"""
from __future__ import annotations

import gzip
import json
import os
import platform
import shlex
import sys
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

GQL_ENDPOINT = (
    "https://affiliate.shopee.co.id/api/v3/gql?q=batchCustomLink"
)
SOURCE_CALLER = "CUSTOM_LINK_CALLER"
DEFAULT_TIMEOUT = 30.0
MAX_SUB_IDS = 5

# Headers that look like Shopee's anti-bot signatures. They are likely tied to
# a specific request body so cannot be reused. We strip them on retry.
_ANTIBOT_HEADER_PREFIXES = ("x-sap-", "af-ac-")
_ANTIBOT_HEADER_NAMES = {"x-sz-sdk-version"}

# GraphQL query exactly as the dashboard sends it, so the server treats us as
# an ordinary client.
_QUERY = (
    "\n    query batchGetCustomLink($linkParams: [CustomLinkParam!], "
    "$sourceCaller: SourceCaller){\n      "
    "batchCustomLink(linkParams: $linkParams, sourceCaller: $sourceCaller){\n"
    "        shortLink\n        longLink\n        failCode\n      }\n"
    "    }\n  "
)
_OPERATION_NAME = "batchGetCustomLink"

# HTTP/2 pseudo-headers that browsers expose in DevTools but that the urllib
# stack will set automatically. Stripping them avoids "invalid header" errors.
_PSEUDO_HEADERS = {":authority", ":method", ":path", ":scheme"}
# Headers the HTTP client must compute itself.
_AUTO_HEADERS = {"content-length", "host"}


class AffiliateError(RuntimeError):
    """Raised when affiliate-link generation fails."""


# ---------------------------------------------------------------------------
# cURL parsing
# ---------------------------------------------------------------------------


def parse_curl(text: str) -> Dict[str, Any]:
    """Parse a Chrome/Firefox "Copy as cURL" command into structured fields.

    Supports both bash style (single-quoted args, ``\\\n`` line continuations)
    and Windows ``cmd`` style (double-quoted args, ``^\n`` continuations).
    Returns a dict with keys ``url`` (str), ``method`` (str), ``headers``
    (dict[str, str]), and ``body`` (str | None).
    """
    if not text or "curl" not in text:
        raise AffiliateError(
            "Tidak menemukan perintah 'curl' di teks yang Anda paste."
        )

    # Normalize line continuations: bash uses ``\\\n``, cmd uses ``^\n``,
    # PowerShell uses `` `\n``. Replace all of them with a single space.
    cleaned = text.replace("\\\r\n", " ").replace("\\\n", " ")
    cleaned = cleaned.replace("^\r\n", " ").replace("^\n", " ")
    cleaned = cleaned.replace("`\r\n", " ").replace("`\n", " ")

    try:
        tokens = shlex.split(cleaned, posix=True)
    except ValueError as e:
        raise AffiliateError(f"Gagal parsing cURL (quoting tidak balance): {e}")

    if not tokens or tokens[0] != "curl":
        # Some users paste with a leading $ or shell prompt — find the curl token.
        if "curl" in tokens:
            tokens = tokens[tokens.index("curl"):]
        else:
            raise AffiliateError("Token 'curl' tidak ditemukan setelah parsing.")

    url: Optional[str] = None
    method: Optional[str] = None
    headers: Dict[str, str] = {}
    body: Optional[str] = None
    cookies_inline: List[str] = []

    i = 1
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("-H", "--header"):
            i += 1
            kv = tokens[i]
            if ":" in kv:
                key, _, value = kv.partition(":")
                headers[key.strip()] = value.strip()
        elif tok in ("-X", "--request"):
            i += 1
            method = tokens[i]
        elif tok in ("-d", "--data", "--data-raw", "--data-binary"):
            i += 1
            body = tokens[i]
            if method is None:
                method = "POST"
        elif tok in ("-b", "--cookie"):
            i += 1
            cookies_inline.append(tokens[i])
        elif tok in ("-A", "--user-agent"):
            i += 1
            headers["User-Agent"] = tokens[i]
        elif tok in ("-e", "--referer"):
            i += 1
            headers["Referer"] = tokens[i]
        elif tok in ("--compressed", "--http2", "--http2-prior-knowledge", "-k", "--insecure"):
            pass
        elif tok in ("-L", "--location"):
            pass
        elif tok.startswith("-"):
            # Unknown flag with an argument. Skip the value too if the next
            # token is not a flag; otherwise keep walking.
            if i + 1 < len(tokens) and not tokens[i + 1].startswith("-"):
                i += 1
        else:
            # The first non-flag token is the URL.
            if url is None:
                url = tok
        i += 1

    if not url:
        raise AffiliateError("URL tidak ditemukan di cURL.")

    if cookies_inline:
        existing = headers.get("Cookie", "")
        merged = "; ".join([c for c in (existing, *cookies_inline) if c])
        headers["Cookie"] = merged

    return {
        "url": url,
        "method": (method or ("POST" if body is not None else "GET")).upper(),
        "headers": _filter_headers(headers),
        "body": body,
    }


def _filter_headers(headers: Dict[str, str]) -> Dict[str, str]:
    """Remove HTTP/2 pseudo-headers and length/host headers."""
    out: Dict[str, str] = {}
    for k, v in headers.items():
        lk = k.lower()
        if lk in _AUTO_HEADERS:
            continue
        if k.startswith(":") or lk in _PSEUDO_HEADERS:
            continue
        out[k] = v
    return out


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


@dataclass
class Session:
    """Captured browser session for the affiliate dashboard."""

    headers: Dict[str, str] = field(default_factory=dict)
    csrf_token: Optional[str] = None

    @classmethod
    def from_curl(cls, text: str) -> "Session":
        parsed = parse_curl(text)
        headers = dict(parsed["headers"])
        cookie = _header_get(headers, "Cookie")
        if not cookie:
            raise AffiliateError(
                "cURL tidak berisi header Cookie — pastikan Anda copy request "
                "yang sudah login (POST ke /api/v3/gql?q=batchCustomLink)."
            )
        # Try to derive CSRF token if not already set, from the cookie jar.
        csrf = _header_get(headers, "Csrf-Token") or _header_get(headers, "X-Csrf-Token")
        if not csrf:
            csrf = _cookie_value(cookie, "csrftoken")
            if csrf:
                headers["Csrf-Token"] = csrf
        return cls(headers=headers, csrf_token=csrf)

    def to_dict(self) -> Dict[str, Any]:
        return {"headers": self.headers, "csrf_token": self.csrf_token}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Session":
        return cls(
            headers=dict(data.get("headers") or {}),
            csrf_token=data.get("csrf_token"),
        )

    @property
    def cookie_summary(self) -> str:
        """Short, non-sensitive description of the captured cookie."""
        cookie = _header_get(self.headers, "Cookie") or ""
        names = [c.split("=", 1)[0].strip() for c in cookie.split(";") if c.strip()]
        if not names:
            return "(belum ada session)"
        return f"{len(names)} cookies (tersimpan), termasuk: " + ", ".join(names[:5])


def _header_get(headers: Dict[str, str], key: str) -> Optional[str]:
    lk = key.lower()
    for k, v in headers.items():
        if k.lower() == lk:
            return v
    return None


def _cookie_value(cookie_header: str, name: str) -> Optional[str]:
    for part in cookie_header.split(";"):
        part = part.strip()
        if not part:
            continue
        k, _, v = part.partition("=")
        if k.strip() == name:
            return v.strip()
    return None


# ---------------------------------------------------------------------------
# Persistent storage
# ---------------------------------------------------------------------------


def user_data_dir(app: str = "shopeelink") -> str:
    """Return per-user, per-app data directory (created if missing)."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        path = os.path.join(base, app)
    elif platform.system() == "Darwin":
        path = os.path.join(os.path.expanduser("~/Library/Application Support"), app)
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
        path = os.path.join(base, app)
    os.makedirs(path, exist_ok=True)
    return path


def session_file_path() -> str:
    return os.path.join(user_data_dir(), "session.json")


def save_session(session: Session, path: Optional[str] = None) -> str:
    target = path or session_file_path()
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(session.to_dict(), f, indent=2)
    os.replace(tmp, target)
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass  # Windows / non-POSIX
    return target


def load_session(path: Optional[str] = None) -> Optional[Session]:
    target = path or session_file_path()
    if not os.path.exists(target):
        return None
    try:
        with open(target, "r", encoding="utf-8") as f:
            data = json.load(f)
        return Session.from_dict(data)
    except (OSError, json.JSONDecodeError):
        return None


def clear_session(path: Optional[str] = None) -> bool:
    target = path or session_file_path()
    try:
        os.remove(target)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------


@dataclass
class LinkInput:
    original_link: str
    sub_ids: Tuple[str, str, str, str, str] = ("", "", "", "", "")

    def to_link_param(self) -> Dict[str, Any]:
        params: Dict[str, Any] = {"originalLink": self.original_link}
        advanced: Dict[str, str] = {}
        for idx, sub in enumerate(self.sub_ids[:MAX_SUB_IDS], start=1):
            if sub:
                advanced[f"subId{idx}"] = sub
        if advanced:
            params["advancedLinkParams"] = advanced
        return params


@dataclass
class LinkResult:
    original_link: str
    short_link: Optional[str] = None
    long_link: Optional[str] = None
    fail_code: int = 0
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.fail_code == 0 and bool(self.short_link)


def build_payload(items: Iterable[LinkInput]) -> Dict[str, Any]:
    return {
        "operationName": _OPERATION_NAME,
        "query": _QUERY,
        "variables": {
            "linkParams": [it.to_link_param() for it in items],
            "sourceCaller": SOURCE_CALLER,
        },
    }


def _strip_antibot_headers(headers: Dict[str, str]) -> Dict[str, str]:
    """Return a copy of ``headers`` without Shopee's anti-bot signature headers."""
    out: Dict[str, str] = {}
    for k, v in headers.items():
        lk = k.lower()
        if lk in _ANTIBOT_HEADER_NAMES:
            continue
        if any(lk.startswith(p) for p in _ANTIBOT_HEADER_PREFIXES):
            continue
        out[k] = v
    return out


def _decode_body(raw: bytes, content_encoding: Optional[str]) -> str:
    """Decompress ``raw`` based on ``content_encoding`` and decode to text."""
    if not raw:
        return ""
    enc = (content_encoding or "").lower().strip()
    try:
        if enc == "gzip":
            raw = gzip.decompress(raw)
        elif enc == "deflate":
            try:
                raw = zlib.decompress(raw)
            except zlib.error:
                raw = zlib.decompress(raw, -zlib.MAX_WBITS)
        elif enc == "br":
            try:
                import brotli  # type: ignore[import-not-found]
                raw = brotli.decompress(raw)
            except Exception:  # noqa: BLE001
                # No brotli available; leave bytes as-is and let the caller fail.
                pass
    except Exception:  # noqa: BLE001
        pass
    return raw.decode("utf-8", errors="replace")


def _save_debug(payload: Dict[str, Any], headers: Dict[str, str], status: int, body: str) -> str:
    """Save the most recent request/response pair for troubleshooting."""
    safe_headers = {
        k: ("<redacted>" if k.lower() in ("cookie", "csrf-token", "x-csrf-token") else v)
        for k, v in headers.items()
    }
    record = {
        "request": {"url": GQL_ENDPOINT, "headers": safe_headers, "payload": payload},
        "response": {"status": status, "body": body[:8000]},
    }
    path = os.path.join(user_data_dir(), "last_response.json")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f, indent=2, ensure_ascii=False)
    except OSError:
        pass
    return path


def _do_request(
    body: bytes, headers: Dict[str, str], timeout: float
) -> Tuple[int, Dict[str, str], str]:
    """Single POST attempt. Returns (status, response_headers, decoded_body)."""
    req = urllib.request.Request(
        GQL_ENDPOINT, data=body, method="POST", headers=headers
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec
            raw = resp.read()
            resp_headers = {k: v for k, v in resp.headers.items()}
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = b""
        try:
            raw = e.read()
        except Exception:  # noqa: BLE001
            pass
        resp_headers = {k: v for k, v in e.headers.items()} if e.headers else {}
        status = e.code
    text = _decode_body(raw, resp_headers.get("Content-Encoding") or resp_headers.get("content-encoding"))
    return status, resp_headers, text


def generate_short_links(
    inputs: Iterable[LinkInput],
    session: Session,
    timeout: float = DEFAULT_TIMEOUT,
) -> List[LinkResult]:
    """Send a batch request and return one ``LinkResult`` per input.

    Strategy:
    1. First try with **full** captured headers (including Shopee anti-bot
       signatures) since they may be session-bound.
    2. If that returns an empty batch (silent rejection — common anti-bot
       pattern), retry **without** the anti-bot headers, in case they were
       request-bound and replaying them was actually the problem.
    3. If both return empty, raise with the raw response sample so the user
       can paste it back to us for debugging.
    """
    items = list(inputs)
    if not items:
        return []
    payload = build_payload(items)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    base_headers = dict(session.headers)
    base_headers.setdefault("Content-Type", "application/json; charset=UTF-8")
    base_headers.setdefault("Accept", "application/json, text/plain, */*")
    # Force identity encoding so we don't have to handle every codec.
    base_headers["Accept-Encoding"] = "identity"
    if session.csrf_token and not _header_get(base_headers, "Csrf-Token"):
        base_headers["Csrf-Token"] = session.csrf_token

    attempts: List[Tuple[str, Dict[str, str]]] = [
        ("full", base_headers),
        ("no-antibot", _strip_antibot_headers(base_headers)),
    ]

    last_status = 0
    last_body = ""
    last_headers: Dict[str, str] = {}
    for label, headers in attempts:
        try:
            status, _, text = _do_request(body, headers, timeout)
        except urllib.error.URLError as e:
            raise AffiliateError(f"Gagal connect ke Shopee: {e.reason}")
        last_status, last_body, last_headers = status, text, headers

        if status >= 400:
            # No point retrying on the no-antibot attempt with the same auth.
            _save_debug(payload, headers, status, text)
            raise AffiliateError(
                f"HTTP {status} dari Shopee (attempt={label}). "
                "Sesi mungkin sudah kedaluwarsa — refresh dengan Import cURL "
                "terbaru.\n\nResponse:\n" + (text[:500] or "(empty body)")
            )

        try:
            data = json.loads(text) if text else None
        except json.JSONDecodeError:
            _save_debug(payload, headers, status, text)
            raise AffiliateError(
                f"Response Shopee bukan JSON valid (attempt={label}).\n\n"
                "Response sample:\n" + (text[:500] or "(empty body)")
            )

        if isinstance(data, dict) and data.get("errors"):
            msgs = "; ".join(
                str(err.get("message", err)) for err in data["errors"]
            )
            _save_debug(payload, headers, status, text)
            raise AffiliateError(f"GraphQL errors (attempt={label}): {msgs}")

        batch = ((data or {}).get("data") or {}).get("batchCustomLink") or []
        if batch:
            return _build_results(items, batch)
        # Empty batch; try the next attempt.

    debug_path = _save_debug(payload, last_headers, last_status, last_body)
    raise AffiliateError(
        "Shopee mengembalikan response sukses (HTTP "
        f"{last_status}) tapi batch kosong di kedua attempt (full + "
        "no-antibot). Kemungkinan:\n"
        " • Sesi tertolak oleh anti-bot (silent reject)\n"
        " • Atau format payload berubah sejak terakhir di-capture\n\n"
        f"Raw response disimpan di: {debug_path}\n\n"
        "Response sample:\n" + (last_body[:600] or "(empty body)")
    )


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
