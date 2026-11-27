"""src.editor: the line buffer and its rendering (pure), and the editor in a pty (AC10)."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from src.editor import LineBuffer, render
from src.textwidth import width


def buf(text="", cursor=None):
    b = LineBuffer()
    b.insert(text)
    if cursor is not None:
        b.cursor = cursor
    return b


class LineBufferOps(unittest.TestCase):
    def test_edit_operations(self):
        with self.subTest("backspace and delete"):
            self.backspace_and_delete()
        with self.subTest("kill word"):
            self.kill_word_takes_the_word_before_the_cursor()
        with self.subTest("combining mark"):
            self.a_combining_mark_moves_and_deletes_with_its_base()

    def backspace_and_delete(self):
        b = buf("abc", 2)
        b.backspace()
        self.assertEqual((b.text, b.cursor), ("ac", 1))
        b.delete()
        self.assertEqual((b.text, b.cursor), ("a", 1))
        b.delete()
        self.assertEqual(b.text, "a")
        b.home()
        b.backspace()
        self.assertEqual(b.text, "a")

    def kill_word_takes_the_word_before_the_cursor(self):
        b = buf("plugin install  foo", 19)
        b.kill_word()
        self.assertEqual(b.text, "plugin install  ")
        b.kill_word()
        self.assertEqual(b.text, "plugin ")

    def a_combining_mark_moves_and_deletes_with_its_base(self):
        b = buf("éx")  # é as two code points, then x
        b.left()
        b.left()
        self.assertEqual(b.cursor, 0)
        b.right()
        self.assertEqual(b.cursor, 2)
        b.backspace()
        self.assertEqual(b.text, "x")


class Render(unittest.TestCase):
    def check(self, prompt, b, cols):
        line, col = render(prompt, b, cols)
        self.assertLessEqual(width(line), cols - 1 if width(prompt) < cols else cols, line)
        self.assertLess(col, cols)
        self.assertGreaterEqual(col, 0)
        return line, col

    def test_a_long_line_scrolls_to_keep_the_cursor_visible_at_the_end(self):
        for cols in (10, 40):
            line, col = self.check("dock> ", buf("x" * 100), cols)
            self.assertEqual(col, width(line), "the cursor is at the end of the visible text")


if __name__ == "__main__":
    unittest.main()
