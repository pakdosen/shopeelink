"""Unit tests for shopeelink (no network)."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import shopeelink  # noqa: E402


class ConvertToProductUrlTests(unittest.TestCase):
    def test_opaanlp_redirect_path(self) -> None:
        url = (
            "https://shopee.co.id/opaanlp/2637287/23082544058"
            "?__mobile__=1&utm_source=an_11319881104"
        )
        self.assertEqual(
            shopeelink.convert_to_product_url(url),
            "https://shopee.co.id/product/2637287/23082544058",
        )

    def test_already_product_path(self) -> None:
        url = "https://shopee.co.id/product/2637287/23082544058"
        self.assertEqual(
            shopeelink.convert_to_product_url(url),
            "https://shopee.co.id/product/2637287/23082544058",
        )

    def test_canonical_slug_path(self) -> None:
        url = "https://shopee.co.id/Some-Awesome-Item-i.2637287.23082544058"
        self.assertEqual(
            shopeelink.convert_to_product_url(url),
            "https://shopee.co.id/product/2637287/23082544058",
        )

    def test_strips_short_link_host_prefix(self) -> None:
        url = "https://s.shopee.co.id/product/123456/7890123"
        self.assertEqual(
            shopeelink.convert_to_product_url(url),
            "https://shopee.co.id/product/123456/7890123",
        )

    def test_other_locale(self) -> None:
        url = "https://shopee.sg/product/998877/776655"
        self.assertEqual(
            shopeelink.convert_to_product_url(url),
            "https://shopee.sg/product/998877/776655",
        )

    def test_invalid_url_raises(self) -> None:
        with self.assertRaises(shopeelink.ShopeeLinkError):
            shopeelink.convert_to_product_url("https://example.com/not/a/product")


class ConvertManyTests(unittest.TestCase):
    def test_mixed_success_and_failure(self) -> None:
        results = shopeelink.convert_many(
            [
                "https://shopee.co.id/product/123456/7890123",
                "https://example.com/no-ids",
            ]
        )
        self.assertEqual(len(results), 2)
        self.assertEqual(
            results[0][1], "https://shopee.co.id/product/123456/7890123"
        )
        self.assertIsNone(results[0][2])
        self.assertIsNone(results[1][1])
        self.assertIsNotNone(results[1][2])


if __name__ == "__main__":
    unittest.main()
