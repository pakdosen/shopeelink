#!/usr/bin/env python3
"""Convert Shopee short links to direct product links.

Examples
--------
Input:  https://s.shopee.co.id/1VvkmRGQgz
Output: https://shopee.co.id/product/2637287/23082544058

Usage
-----
    python shopeelink.py <url> [<url> ...]
    cat urls.txt | python shopeelink.py
"""
from __future__ import annotations

import argparse
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Iterable, List

USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
)

# Match URLs like /product/2637287/23082544058 or /opaanlp/2637287/23082544058.
# We accept any non-empty leading segment so we don't have to enumerate every
# Shopee landing path ("product", "opaanlp", "affiliate", "universal-link", ...).
_PATH_NUMERIC_RE = re.compile(r"/[^/?#]+/(\d+)/(\d+)(?:[/?#]|$)")
# Match canonical product slugs like /Item-Name-i.2637287.23082544058
_PATH_DASH_I_RE = re.compile(r"-i\.(\d+)\.(\d+)")
# Match the JS CONFIG.httpUrl embedded in s.shopee.* short-link landing pages.
_HTTP_URL_RE = re.compile(r'httpUrl\s*:\s*"([^"]+)"')

DEFAULT_TIMEOUT = 15.0


class ShopeeLinkError(ValueError):
    """Raised when a URL cannot be converted to a product link."""


def _build_product_url(host: str, shop_id: str, item_id: str) -> str:
    host = host or "shopee.co.id"
    # Shopee short-link host is s.shopee.* — fall back to the main storefront.
    if host.startswith("s."):
        host = host[2:]
    return f"https://{host}/product/{shop_id}/{item_id}"


def _extract_ids(path: str) -> tuple[str, str] | None:
    m = _PATH_DASH_I_RE.search(path)
    if m:
        return m.group(1), m.group(2)
    m = _PATH_NUMERIC_RE.search(path)
    if m:
        return m.group(1), m.group(2)
    return None


def convert_to_product_url(url: str) -> str:
    """Convert a fully-qualified Shopee URL to ``/product/SHOP/ITEM`` form.

    Does not perform any network IO.
    """
    parsed = urllib.parse.urlparse(url)
    ids = _extract_ids(parsed.path)
    if ids is None:
        raise ShopeeLinkError(f"Could not extract shop_id/item_id from URL: {url}")
    return _build_product_url(parsed.netloc, *ids)


class _HeadRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Redirect handler that preserves the HEAD method across redirects."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        new_req = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_req is not None:
            new_req.method = "HEAD"
        return new_req


class _HeadRequest(urllib.request.Request):
    def get_method(self) -> str:  # type: ignore[override]
        return "HEAD"


def _resolve_via_head(url: str, timeout: float) -> str | None:
    opener = urllib.request.build_opener(_HeadRedirectHandler)
    req = _HeadRequest(url, headers={"User-Agent": USER_AGENT})
    try:
        with opener.open(req, timeout=timeout) as resp:  # nosec - user-supplied URL
            return resp.geturl()
    except (urllib.error.URLError, urllib.error.HTTPError):
        return None


def _resolve_via_body(url: str, timeout: float) -> str | None:
    """Fetch the short-link page and extract the JS-embedded destination URL."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec
            body = resp.read(200_000).decode("utf-8", errors="replace")
    except (urllib.error.URLError, urllib.error.HTTPError):
        return None
    m = _HTTP_URL_RE.search(body)
    if not m:
        return None
    # The embedded URL uses backslash-escaped slashes (\/) and \u00xx escapes
    # for query separators. Strip slashes first so unicode_escape doesn't see
    # an invalid \/ sequence.
    raw = m.group(1).replace("\\/", "/")
    return raw.encode("utf-8").decode("unicode_escape")


def resolve_short_link(url: str, timeout: float = DEFAULT_TIMEOUT) -> str:
    """Resolve a Shopee short link to the canonical product URL.

    First tries an HTTP HEAD request that follows redirects (cheap, server-side
    redirect path). Falls back to fetching the landing page HTML and parsing the
    JavaScript ``CONFIG.httpUrl`` field that Shopee embeds for client-side
    redirects.
    """
    final_url = _resolve_via_head(url, timeout)
    if final_url and final_url != url:
        try:
            return convert_to_product_url(final_url)
        except ShopeeLinkError:
            pass
    body_url = _resolve_via_body(url, timeout)
    if body_url:
        return convert_to_product_url(body_url)
    raise ShopeeLinkError(
        f"Could not resolve short link to a product URL: {url}"
    )


def convert(url: str, timeout: float = DEFAULT_TIMEOUT) -> str:
    """Convert any Shopee URL (short or long) to ``/product/SHOP/ITEM`` form.

    Short links (host starts with ``s.``) are resolved by following redirects;
    long links are parsed locally without network IO.
    """
    url = url.strip()
    if not url:
        raise ShopeeLinkError("Empty URL")
    if "://" not in url:
        url = "https://" + url
    parsed = urllib.parse.urlparse(url)
    if parsed.netloc.startswith("s."):
        return resolve_short_link(url, timeout=timeout)
    return convert_to_product_url(url)


def convert_many(urls: Iterable[str], timeout: float = DEFAULT_TIMEOUT) -> List[tuple[str, str | None, str | None]]:
    """Convert many URLs, returning ``(input, output, error)`` tuples."""
    results: List[tuple[str, str | None, str | None]] = []
    for url in urls:
        try:
            results.append((url, convert(url, timeout=timeout), None))
        except Exception as e:  # noqa: BLE001 - surface any failure per-URL
            results.append((url, None, str(e)))
    return results


def _read_inputs(args_urls: List[str]) -> List[str]:
    if args_urls:
        return [u for u in args_urls if u.strip()]
    if sys.stdin.isatty():
        return []
    return [line.strip() for line in sys.stdin if line.strip()]


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="shopeelink",
        description=(
            "Convert Shopee short links (s.shopee.co.id/...) into direct "
            "product links of the form https://shopee.co.id/product/<shop>/<item>."
        ),
    )
    parser.add_argument(
        "urls",
        nargs="*",
        help="One or more Shopee URLs. If omitted, reads one URL per line from stdin.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f"HTTP timeout in seconds (default: {DEFAULT_TIMEOUT}).",
    )
    parser.add_argument(
        "--show-input",
        action="store_true",
        help="Print input and output as TSV (input<TAB>output).",
    )
    args = parser.parse_args(argv)

    inputs = _read_inputs(args.urls)
    if not inputs:
        parser.print_help(sys.stderr)
        return 2

    exit_code = 0
    for url, out, err in convert_many(inputs, timeout=args.timeout):
        if err is not None:
            print(f"ERROR\t{url}\t{err}", file=sys.stderr)
            exit_code = 1
            continue
        if args.show_input:
            print(f"{url}\t{out}")
        else:
            print(out)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
