"""Display width of text in a terminal: wide characters take 2 columns, combining ones none. No runtime needed."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cli.textwidth import cut, width  # noqa: E402


class Width(unittest.TestCase):
    def test_widths(self):
        cases = [
            ("", 0),
            ("abc", 3),
            ("日本語", 6),
            ("ｱ", 1),  # half-width katakana
            ("Ａ", 2),  # full-width A
            ("é", 1),  # e + combining acute
            ("😀", 2),
            ("a‍b", 2),  # zero-width joiner takes no column
            ("👨‍👩", 4),  # ZWJ sequence: 2 columns per base character
            ("a b", 3),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(width(text), expected)


class Cut(unittest.TestCase):
    def test_fitting_text_is_unchanged(self):
        self.assertEqual(cut("abc", 3), "abc")
        self.assertEqual(cut("", 0), "")

    def test_ascii_at_widths_0_to_5(self):
        expected = {0: "", 1: "…", 2: "a…", 3: "ab…", 4: "abc…", 5: "abcd…"}
        for cols, want in expected.items():
            with self.subTest(cols=cols):
                self.assertEqual(cut("abcdefgh", cols), want)

    def test_wide_characters_are_not_split(self):
        self.assertEqual(cut("日本語", 4), "日…")
        self.assertEqual(cut("日本語", 5), "日本…")
        self.assertEqual(cut("日本語", 2), "…")

    def test_result_always_fits(self):
        for text in ("abcdefgh", "日本語日本語", "éééé", "😀😀😀😀"):
            for cols in range(0, 9):
                with self.subTest(text=text, cols=cols):
                    self.assertLessEqual(width(cut(text, cols)), cols)

    def test_combining_mark_stays_with_its_base(self):
        self.assertEqual(cut("ééé", 2), "é…")


if __name__ == "__main__":
    unittest.main()
