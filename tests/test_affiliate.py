"""Unit tests for the affiliate module (no network)."""
from __future__ import annotations

import gzip
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

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


class StripAntibotHeadersTests(unittest.TestCase):
    def test_drops_x_sap_and_af_ac_headers(self) -> None:
        headers = {
            "Content-Type": "application/json",
            "Cookie": "csrftoken=t",
            "X-Sap-Ri": "abc",
            "X-Sap-Sec": "def",
            "Af-Ac-Enc-Dat": "xyz",
            "Af-Ac-Enc-Sz-Token": "tok",
            "X-Sz-Sdk-Version": "1.12.21",
            "Csrf-Token": "t",
        }
        stripped = affiliate._strip_antibot_headers(headers)
        self.assertNotIn("X-Sap-Ri", stripped)
        self.assertNotIn("X-Sap-Sec", stripped)
        self.assertNotIn("Af-Ac-Enc-Dat", stripped)
        self.assertNotIn("Af-Ac-Enc-Sz-Token", stripped)
        self.assertNotIn("X-Sz-Sdk-Version", stripped)
        # Essentials remain.
        self.assertIn("Cookie", stripped)
        self.assertIn("Csrf-Token", stripped)
        self.assertIn("Content-Type", stripped)


class DecodeBodyTests(unittest.TestCase):
    def test_identity(self) -> None:
        self.assertEqual(affiliate._decode_body(b'{"x":1}', None), '{"x":1}')

    def test_gzip(self) -> None:
        compressed = gzip.compress(b'{"y":2}')
        self.assertEqual(affiliate._decode_body(compressed, "gzip"), '{"y":2}')

    def test_unknown_encoding_returns_raw_decoded(self) -> None:
        # If we can't decompress, fall back to raw decode (so user sees garbage,
        # not silent failure).
        self.assertEqual(affiliate._decode_body(b"plain", "weirdcodec"), "plain")


class GenerateShortLinksTests(unittest.TestCase):
    def _session(self) -> affiliate.Session:
        return affiliate.Session(
            headers={
                "Cookie": "csrftoken=t; SPC_EC=e",
                "Csrf-Token": "t",
                "X-Sap-Sec": "antibot-sig",
                "X-Sap-Ri": "antibot-ri",
            },
            csrf_token="t",
        )

    def test_returns_results_when_first_attempt_succeeds(self) -> None:
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
        with mock.patch.object(
            affiliate, "_do_request", return_value=(200, {}, body)
        ) as do_req:
            results = affiliate.generate_short_links(
                [affiliate.LinkInput(original_link="https://shopee.co.id/product/1/2")],
                self._session(),
            )
        self.assertEqual(do_req.call_count, 1)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].short_link, "https://s.shopee.co.id/AAA")
        self.assertTrue(results[0].ok)

    def test_retries_without_antibot_when_first_attempt_returns_empty(self) -> None:
        empty_body = json.dumps({"data": {"batchCustomLink": []}})
        success_body = json.dumps(
            {
                "data": {
                    "batchCustomLink": [
                        {
                            "shortLink": "https://s.shopee.co.id/BBB",
                            "longLink": "https://shopee.co.id/universal-link/product/1/2",
                            "failCode": 0,
                        }
                    ]
                }
            }
        )
        with mock.patch.object(
            affiliate,
            "_do_request",
            side_effect=[(200, {}, empty_body), (200, {}, success_body)],
        ) as do_req:
            results = affiliate.generate_short_links(
                [affiliate.LinkInput(original_link="https://shopee.co.id/product/1/2")],
                self._session(),
            )
        # Should have made TWO calls: first with anti-bot headers, second
        # without them.
        self.assertEqual(do_req.call_count, 2)
        first_headers = do_req.call_args_list[0][0][1]
        second_headers = do_req.call_args_list[1][0][1]
        self.assertIn("X-Sap-Sec", first_headers)
        self.assertNotIn("X-Sap-Sec", second_headers)
        self.assertEqual(results[0].short_link, "https://s.shopee.co.id/BBB")

    def test_raises_with_raw_response_when_both_attempts_empty(self) -> None:
        empty_body = json.dumps({"data": {"batchCustomLink": []}})
        with mock.patch.object(
            affiliate, "_do_request", return_value=(200, {}, empty_body)
        ), tempfile.TemporaryDirectory() as d, mock.patch.object(
            affiliate, "user_data_dir", return_value=d
        ):
            with self.assertRaises(affiliate.AffiliateError) as ctx:
                affiliate.generate_short_links(
                    [affiliate.LinkInput(original_link="https://shopee.co.id/product/1/2")],
                    self._session(),
                )
            # Debug file should have been written.
            self.assertTrue(os.path.exists(os.path.join(d, "last_response.json")))
        self.assertIn("batch kosong", str(ctx.exception))

    def test_raises_on_http_error_with_response_body(self) -> None:
        with mock.patch.object(
            affiliate, "_do_request", return_value=(403, {}, "Forbidden")
        ), tempfile.TemporaryDirectory() as d, mock.patch.object(
            affiliate, "user_data_dir", return_value=d
        ):
            with self.assertRaises(affiliate.AffiliateError) as ctx:
                affiliate.generate_short_links(
                    [affiliate.LinkInput(original_link="https://shopee.co.id/product/1/2")],
                    self._session(),
                )
        self.assertIn("HTTP 403", str(ctx.exception))
        self.assertIn("Forbidden", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
