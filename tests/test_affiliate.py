"""Unit tests for the affiliate module (no network)."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import affiliate  # noqa: E402


SAMPLE_BASH_CURL = (
    "curl 'https://affiliate.shopee.co.id/api/v3/gql?q=batchCustomLink' \\\n"
    "  -X POST \\\n"
    "  -H 'Content-Type: application/json; charset=UTF-8' \\\n"
    "  -H 'Accept: application/json, text/plain, */*' \\\n"
    "  -H 'Origin: https://affiliate.shopee.co.id' \\\n"
    "  -H 'Referer: https://affiliate.shopee.co.id/offer/custom_link' \\\n"
    "  -H 'Csrf-Token: kwg7g5IW-test-token' \\\n"
    "  -H 'X-Sap-Ri: 4cb4fa698d96f9fac21b8f3f050179f06e6d09fc74d1d2857d86' \\\n"
    "  -H 'X-Sap-Sec: fsaGHNYdTBwPqRSJuBaJuRTJ5Baaud-test' \\\n"
    "  -H 'Cookie: csrftoken=kwg7g5IW-test-token; SPC_EC=abc; SPC_F=xyz' \\\n"
    "  --data-raw '{\"operationName\":\"batchGetCustomLink\"}'"
)


class ParseCurlTests(unittest.TestCase):
    def test_parse_basic_bash_curl(self) -> None:
        parsed = affiliate.parse_curl(SAMPLE_BASH_CURL)
        self.assertEqual(
            parsed["url"],
            "https://affiliate.shopee.co.id/api/v3/gql?q=batchCustomLink",
        )
        self.assertEqual(parsed["method"], "POST")
        self.assertEqual(
            parsed["headers"]["Content-Type"],
            "application/json; charset=UTF-8",
        )
        self.assertEqual(parsed["headers"]["Csrf-Token"], "kwg7g5IW-test-token")
        self.assertIn("csrftoken=kwg7g5IW-test-token", parsed["headers"]["Cookie"])
        self.assertIn('"operationName":"batchGetCustomLink"', parsed["body"])

    def test_strips_pseudo_headers(self) -> None:
        # Browser DevTools sometimes embeds HTTP/2 pseudo-headers via Copy as
        # cURL on certain platforms; make sure we strip them so urllib accepts
        # the request.
        text = (
            "curl 'https://example.com' "
            "-H ':authority: example.com' "
            "-H ':method: POST' "
            "-H 'X-Other: keep' "
        )
        parsed = affiliate.parse_curl(text)
        self.assertNotIn(":authority", parsed["headers"])
        self.assertNotIn(":method", parsed["headers"])
        self.assertEqual(parsed["headers"]["X-Other"], "keep")

    def test_invalid_input_raises(self) -> None:
        with self.assertRaises(affiliate.AffiliateError):
            affiliate.parse_curl("")
        with self.assertRaises(affiliate.AffiliateError):
            affiliate.parse_curl("definitely not curl")


class SessionTests(unittest.TestCase):
    def test_from_curl_extracts_csrf_from_cookie_when_header_missing(self) -> None:
        text = (
            "curl 'https://affiliate.shopee.co.id/api/v3/gql?q=batchCustomLink' "
            "-H 'Cookie: csrftoken=fromcookie123; SPC_EC=abc' "
        )
        s = affiliate.Session.from_curl(text)
        self.assertEqual(s.csrf_token, "fromcookie123")
        self.assertEqual(s.headers.get("Csrf-Token"), "fromcookie123")

    def test_cookie_summary_lists_names_only(self) -> None:
        s = affiliate.Session(
            headers={"Cookie": "csrftoken=abc; SPC_EC=xyz; foo=bar"}
        )
        summary = s.cookie_summary
        self.assertIn("csrftoken", summary)
        self.assertIn("SPC_EC", summary)
        # Values must not leak into the summary.
        self.assertNotIn("abc", summary)
        self.assertNotIn("xyz", summary)

    def test_save_and_load_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "session.json")
            s = affiliate.Session(
                headers={"Cookie": "csrftoken=t; SPC_EC=e"}, csrf_token="t"
            )
            affiliate.save_session(s, path)
            loaded = affiliate.load_session(path)
            assert loaded is not None
            self.assertEqual(loaded.headers, s.headers)
            self.assertEqual(loaded.csrf_token, s.csrf_token)
            self.assertTrue(affiliate.clear_session(path))
            self.assertIsNone(affiliate.load_session(path))


class PayloadTests(unittest.TestCase):
    def test_build_payload_matches_dashboard_shape(self) -> None:
        payload = affiliate.build_payload(
            [
                affiliate.LinkInput(
                    original_link="https://shopee.co.id/product/274540732/28055610077",
                    sub_ids=("PF1", "", "", "", ""),
                )
            ]
        )
        self.assertEqual(payload["operationName"], "batchGetCustomLink")
        self.assertEqual(payload["variables"]["sourceCaller"], "CUSTOM_LINK_CALLER")
        params = payload["variables"]["linkParams"]
        self.assertEqual(len(params), 1)
        self.assertEqual(
            params[0]["originalLink"],
            "https://shopee.co.id/product/274540732/28055610077",
        )
        self.assertEqual(params[0]["advancedLinkParams"], {"subId1": "PF1"})

    def test_build_payload_omits_empty_sub_ids(self) -> None:
        payload = affiliate.build_payload(
            [affiliate.LinkInput(original_link="https://shopee.co.id/product/1/2")]
        )
        params = payload["variables"]["linkParams"][0]
        # No advancedLinkParams when all sub-IDs are empty.
        self.assertNotIn("advancedLinkParams", params)

    def test_build_payload_with_multiple_sub_ids(self) -> None:
        payload = affiliate.build_payload(
            [
                affiliate.LinkInput(
                    original_link="https://shopee.co.id/product/1/2",
                    sub_ids=("a", "b", "", "d", ""),
                )
            ]
        )
        adv = payload["variables"]["linkParams"][0]["advancedLinkParams"]
        self.assertEqual(adv, {"subId1": "a", "subId2": "b", "subId4": "d"})


if __name__ == "__main__":
    unittest.main()
