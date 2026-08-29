import unittest

from irancell_number_bot import children_for_pattern, format_number, normalize_digits


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


if __name__ == "__main__":
    unittest.main()
