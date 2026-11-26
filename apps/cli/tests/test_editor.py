"""src.editor: the line buffer and its rendering (pure), and the editor in a pty (AC10)."""

import os
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import ptykit
from src.editor import LineBuffer, render
from src.textwidth import width


def buf(text="", cursor=None):
    b = LineBuffer()
    b.insert(text)
    if cursor is not None:
        b.cursor = cursor
    return b


class LineBufferOps(unittest.TestCase):
    def test_insert_at_the_cursor(self):
        b = buf("helo", 3)
        b.insert("l")
        self.assertEqual((b.text, b.cursor), ("hello", 4))

    def test_left_right_stop_at_the_ends(self):
        b = buf("ab")
        b.right()
        self.assertEqual(b.cursor, 2)
        for _ in range(3):
            b.left()
        self.assertEqual(b.cursor, 0)

    def test_home_end(self):
        b = buf("abc", 1)
        b.end()
        self.assertEqual(b.cursor, 3)
        b.home()
        self.assertEqual(b.cursor, 0)

    def test_backspace_and_delete(self):
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

    def test_kill_end_and_start(self):
        b = buf("hello world", 5)
        b.kill_end()
        self.assertEqual(b.text, "hello")
        b.kill_start()
        self.assertEqual((b.text, b.cursor), ("", 0))

    def test_kill_start_keeps_the_text_after_the_cursor(self):
        b = buf("hello world", 6)
        b.kill_start()
        self.assertEqual((b.text, b.cursor), ("world", 0))

    def test_kill_word_takes_the_word_before_the_cursor(self):
        b = buf("plugin install  foo", 19)
        b.kill_word()
        self.assertEqual(b.text, "plugin install  ")
        b.kill_word()
        self.assertEqual(b.text, "plugin ")

    def test_a_combining_mark_moves_and_deletes_with_its_base(self):
        b = buf("éx")  # é as two code points, then x
        b.left()
        b.left()
        self.assertEqual(b.cursor, 0)
        b.right()
        self.assertEqual(b.cursor, 2)
        b.backspace()
        self.assertEqual(b.text, "x")

    def test_set_replaces_everything_and_puts_the_cursor_at_the_end(self):
        b = buf("abc", 1)
        b.set("history entry")
        self.assertEqual((b.text, b.cursor), ("history entry", 13))


class Render(unittest.TestCase):
    def check(self, prompt, b, cols):
        line, col = render(prompt, b, cols)
        self.assertLessEqual(width(line), cols - 1 if width(prompt) < cols else cols, line)
        self.assertLess(col, cols)
        self.assertGreaterEqual(col, 0)
        return line, col

    def test_a_short_line_is_shown_whole_with_the_cursor_after_the_prompt(self):
        line, col = self.check("dock> ", buf("help", 2), 40)
        self.assertEqual((line, col), ("dock> help", 8))

    def test_a_cjk_line_counts_two_columns_per_character(self):
        line, col = self.check("dock> ", buf("日本語", 2), 40)
        self.assertEqual((line, col), ("dock> 日本語", 6 + 4))

    def test_a_combining_mark_takes_no_column(self):
        line, col = render("> ", buf("éé", 4), 40)
        self.assertEqual(col, 4)

    def test_a_long_line_scrolls_to_keep_the_cursor_visible_at_the_end(self):
        for cols in (10, 40):
            line, col = self.check("dock> ", buf("x" * 100), cols)
            self.assertEqual(col, width(line), "the cursor is at the end of the visible text")

    def test_a_long_line_scrolls_to_keep_the_cursor_visible_in_the_middle(self):
        text = "".join(chr(ord("a") + i % 26) for i in range(100))
        for cols in (10, 40):
            for cursor in (0, 1, 50, 99):
                line, col = self.check("dock> ", buf(text, cursor), cols)
                shown = line[len("dock> "):]
                self.assertIn(text[cursor:cursor + 1] or "", shown + " ")
                self.assertEqual(line[col:col + 1] or text[cursor:cursor + 1], text[cursor:cursor + 1] or "")

    def test_wide_characters_never_overflow_the_width(self):
        for cols in (10, 40):
            for cursor in (0, 7, 30, 60):
                self.check("dock> ", buf("日本語です" * 12, cursor), cols)


@unittest.skipUnless(ptykit.AVAILABLE, "needs pty and termios")
class EditorInPty(unittest.TestCase):
    def start(self, history=(), cols=40, rows=10, extra=()):
        env = {**os.environ, "PYTHONPATH": str(ROOT), "TERM": "xterm-256color"}
        p = ptykit.Pty([sys.executable, str(ROOT / "tests" / "fixtures" / "editordriver.py"), *extra],
                       cols=cols, rows=rows, env=env, cwd=ROOT)
        self.addCleanup(p.close)
        p.wait_text("ready>")
        return p

    def line_now(self, p):
        """The prompt line the screen shows, without the prompt."""
        for line in reversed(p.text().splitlines()):
            if line.startswith("ready> "):
                return line[len("ready> "):]
        return None

    def test_typing_shows_the_text_and_enter_returns_it(self):
        p = self.start()
        p.send("hello")
        p.wait_text("ready> hello")
        p.send("\r")
        p.wait_text("got: 'hello'")

    def test_arrows_home_end_backspace_delete(self):
        p = self.start()
        p.send("abcdef")
        p.send("\x1b[D\x1b[D")           # left left: before "ef"
        p.send("\x7f")                   # backspace removes d
        p.send("X")
        p.send("\x01")                   # ctrl-a
        p.send("\x1b[3~")                # delete removes a
        p.send("\x05")                   # ctrl-e
        p.send("Z")
        p.send("\r")
        p.wait_text("got: 'bcXefZ'")

    def test_ctrl_k_ctrl_u_ctrl_w(self):
        p = self.start()
        p.send("one two three")
        p.send("\x17")                   # ctrl-w: removes "three"
        p.send("\x1b[D" * 4)             # before "two "
        p.send("\x0b")                   # ctrl-k: removes the rest after the cursor
        p.send("\r")
        p.wait_text("got: 'one '")
        p.send("abc\x15\r")              # ctrl-u clears
        p.wait_text("got: ''")

    def test_history_up_down_keeps_the_edited_line(self):
        p = self.start(extra=["first", "second"])
        p.send("draft")
        p.send("\x1b[A")
        p.wait_for(lambda: self.line_now(p) == "second", what="the last entry")
        p.send("\x1b[A")
        p.wait_for(lambda: self.line_now(p) == "first", what="the first entry")
        p.send("\x1b[B\x1b[B")
        p.wait_for(lambda: self.line_now(p) == "draft", what="the draft back")
        p.send("\r")
        p.wait_text("got: 'draft'")

    def test_tab_completes_a_single_match_and_lists_several(self):
        p = self.start()
        p.send("pl\t")
        p.wait_for(lambda: self.line_now(p) == "plugin", what="completion to plugin")
        p.send("\x15st\t")
        p.wait_text("start  status")
        p.wait_for(lambda: self.line_now(p) == "st", what="the prompt redrawn after the list")
        p.send("\x15\r")

    def test_ctrl_c_prints_and_raises_keyboard_interrupt(self):
        p = self.start()
        p.send("abc\x03")
        p.wait_text("interrupted")

    def test_ctrl_d_on_an_empty_line_is_eof_but_not_on_a_full_one(self):
        p = self.start()
        p.send("ab\x04")                 # on a full line it deletes at the cursor, which is at the end: nothing
        p.send("\r")
        p.wait_text("got: 'ab'")
        p.send("\x04")
        p.wait_text("eof")

    def test_ctrl_l_clears_the_screen_and_redraws_the_line(self):
        p = self.start()
        p.send("keep")
        p.send("\x0c")
        p.wait_data(b"\x1b[H\x1b[2J")
        p.wait_for(lambda: self.line_now(p) == "keep", what="the line after clearing")

    def test_a_cjk_word_can_be_edited_in_the_middle_and_the_cursor_stays_right(self):
        p = self.start()
        p.send("日本語".encode())
        p.send("\x1b[D")                 # before 語
        p.send("X")
        p.wait_for(lambda: self.line_now(p) == "日本X語", what="the inserted X")
        with p._cv:
            col = p.screen.col
        self.assertEqual(col, len("ready> ") + 2 + 2 + 1, "the cursor is after the X, in columns")
        p.send("\r")
        p.wait_text("got: '日本X語'")

    def test_a_long_line_never_wraps_the_screen(self):
        p = self.start(cols=20)
        p.send("x" * 60)
        p.wait_for(lambda: self.line_now(p) is not None and len(self.line_now(p)) > 5, what="the line")
        p.quiet_for(0.2)
        with p._cv:
            self.assertEqual(p.screen.row, 0, "the prompt stayed on one row")

    ALLOWED = re.compile(r"\x1b\[(\d*[AC]|K|J|\?25[hl])")

    def test_only_whitelisted_escape_sequences_are_written(self):
        p = self.start()
        p.send("日本語\x1b[D\x1b[Dabc\x7f\x01\x0bpl\t")
        p.quiet_for(0.3)
        with p._cv:
            seqs = list(p.screen.seqs)
            controls = set(p.screen.controls)
        for s in seqs:
            self.assertTrue(self.ALLOWED.fullmatch(s), repr(s))
        self.assertLessEqual(controls, set())

    def hostile_run(self, seed):
        os.environ.update(DOCK_EDITOR_HOSTILE="1", DOCK_EDITOR_SEED=str(seed))
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in ("DOCK_EDITOR_HOSTILE", "DOCK_EDITOR_SEED")])
        p = self.start(extra=("old\x1b]0;pwned\x07line", "x\x1b[2Jy", "z\x9b2J\x01w"), cols=60)
        p.send("\x1b[A\x1b[A\x1b[A\x1b[B")       # through the history
        p.send("\x15ho\t")                          # candidates: listed, or the common prefix inserted
        p.quiet_for(0.2)
        p.send("\t\x15ho\t\t")                     # and again, on a line that now holds one of them
        p.quiet_for(0.3)
        with p._cv:
            seqs, controls, data = list(p.screen.seqs), list(p.screen.controls), p.data
        for s in seqs:
            self.assertTrue(self.ALLOWED.fullmatch(s), repr(s))
        self.assertEqual(controls, [])
        for bad in (b"\x07", b"\x9b", b"\xc2\x9b", b"\x00", b"\x01", b"pwned\x07"):
            self.assertNotIn(bad, data)
        self.assertIn("�", p.text())

    def test_hostile_candidates_and_history_never_put_their_escape_sequences_on_the_terminal(self):
        for seed in range(6):
            with self.subTest(seed=seed):
                self.hostile_run(seed)


if __name__ == "__main__":
    unittest.main()
