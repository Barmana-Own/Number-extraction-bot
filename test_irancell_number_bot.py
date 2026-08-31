import tempfile
import unittest
from pathlib import Path

from irancell_number_bot import (
    ApiFailure,
    ProductConfig,
    children_for_pattern,
    extract_product,
    format_number,
    normalize_bot_prefix,
    normalize_digits,
)


class NumberHelpersTest(unittest.TestCase):
    def test_normalize_persian_and_display_separators(self):
        self.assertEqual(normalize_digits("۹۰۰ - ۱۳۲۷ - ۷۱۶"), "9001327716")

    def test_normalize_leading_zero(self):
        self.assertEqual(normalize_digits("۰۹۰۰۱۳۲۷۷۱۶"), "9001327716")

    def test_children_split_first_wildcard(self):
        children = children_for_pattern("900*******")
        self.assertEqual(len(children), 10)
        self.assertEqual(children[0], "9000******")
        self.assertEqual(children[-1], "9009******")

    def test_format_900(self):
        self.assertEqual(format_number("9001327716", None, "900"), "900 - 1327 - 716")

    def test_format_professional(self):
        self.assertEqual(format_number("9351080296", "3-3-4", "935"), "935 - 108 - 0296")

    def test_bot_prefix_accepts_displayed_four_digit_prefix(self):
        self.assertEqual(normalize_bot_prefix("۰۹۰۱"), "901")


class FakeIrancellClient:
    def __init__(self):
        self.searches = []

    def get_product(self, _product_id, _referer):
        return {"addons": ["numberSelection"], "prefixes": ["0901"], "format_pattern": "3-3-4"}

    def search_numbers(self, product_id, pattern, _referer):
        self.searches.append((product_id, pattern))
        return {"numbers": ["0901 000 0001"], "limit": 100}


class ExtractionCoreTest(unittest.TestCase):
    def test_extract_product_emits_events_and_respects_target(self):
        events = []
        client = FakeIrancellClient()
        with tempfile.TemporaryDirectory() as temporary:
            result = extract_product(
                client,
                ProductConfig(157, "prepaid-basic", "سیم‌کارت اعتباری پایه"),
                output_root=Path(temporary),
                max_requests=2,
                prefix_filter="0901",
                max_numbers=1,
                on_event=lambda event, payload: events.append((event, payload)),
            )

            self.assertEqual(result["status"], "target_reached")
            self.assertEqual(result["number_count"], 1)
            self.assertEqual(len(client.searches), 1)
            self.assertEqual(events[0][0], "on_started")
            self.assertIn("on_number_found", [event for event, _payload in events])
            self.assertEqual(events[-1][0], "on_completed")

    def test_invalid_prefix_is_rejected_before_search(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(ApiFailure):
                extract_product(
                    FakeIrancellClient(),
                    ProductConfig(157, "prepaid-basic", "سیم‌کارت اعتباری پایه"),
                    output_root=Path(temporary),
                    max_requests=1,
                    prefix_filter="0902",
                )


if __name__ == "__main__":
    unittest.main()
