"""The inline shell (`python -m src` in a pty) with built-in commands only, so no container is needed (AC10, AC13, AC16).

The long-running command and the one-shot runs with a real plugin are in test_live_e2e.py.
"""

import os
import shutil
import signal
import statistics
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

import ptykit

PROMPT = "dock> "
SHOWN = "dock>"  # the screen model drops trailing blanks, so an empty prompt shows without its space
VERSION = "dock 0.2.0"


def shell_env(home, **extra):
    env = {k: v for k, v in os.environ.items() if k not in ("DOCK_PLAIN", "NO_COLOR", "DOCK_NO_READLINE")}
    env.update(DOCK_HOME=str(home), PYTHONPATH=str(ROOT), TERM="xterm-256color", **extra)
    return env


@unittest.skipUnless(ptykit.AVAILABLE, "needs pty and termios")
class InlineShell(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="dock-shell-pty-"))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)

    def start(self, cols=80, rows=24, **env):
        p = ptykit.Pty([sys.executable, "-m", "src"], cols=cols, rows=rows, env=shell_env(self.home, **env), cwd=ROOT)
        self.addCleanup(p.close)
        p.wait_text(SHOWN)
        return p

    def prompt_line(self, p):
        for line in reversed(p.text().splitlines()):
            if line.startswith(SHOWN):
                return line[len(PROMPT):]
        return None

    def prompts(self, p):
        return sum(1 for line in p.text().splitlines() if line.startswith(SHOWN))

    def assert_restored(self, p):
        self.assertEqual(p.termios(), p.initial_termios, "termios is not what it was before")
        self.assertTrue(p.screen.cursor_visible, "the cursor is still hidden")

    def test_a_command_runs_and_ctrl_d_exits_zero_with_the_terminal_restored(self):
        p = self.start()
        p.send("version\r")
        p.wait_text(VERSION)
        p.send("\x04")
        self.assertEqual(p.wait_exit(), 0)
        self.assert_restored(p)

    def test_editing_keys_work_at_the_prompt(self):
        p = self.start()
        p.send("vrsion")
        p.send("\x01")        # ctrl-a
        p.send("\x1b[C")      # right
        p.send("e")
        p.wait_for(lambda: self.prompt_line(p) == "version", what="the edited line")
        p.send("\r")
        p.wait_text(VERSION)

    def test_history_is_recalled_after_a_restart(self):
        p = self.start()
        p.send("version\r")
        p.wait_text(VERSION)
        p.send("\x04")
        self.assertEqual(p.wait_exit(), 0)
        self.assertTrue((self.home / "history").exists(), "the history file was not written")
        self.assertEqual((self.home / "history").stat().st_mode & 0o777, 0o600)
        p2 = self.start()
        p2.send("\x1b[A")
        p2.wait_for(lambda: self.prompt_line(p2) == "version", what="the previous session's command")
        p2.send("\r")
        p2.wait_text(VERSION)

    def test_tab_completes_a_command_and_a_subcommand(self):
        p = self.start()
        p.send("plug\t")
        p.wait_for(lambda: self.prompt_line(p) == "plugin", what="plugin")
        p.send("li\t")
        p.wait_for(lambda: self.prompt_line(p) == "plugin list", what="plugin list")
        p.send("\r")
        p.wait_text("no plugins installed")

    def test_tab_lists_several_matches_below_the_prompt(self):
        p = self.start()
        p.send("s\t")
        p.wait_text("search")
        p.wait_text("status")

    def test_ctrl_c_clears_the_line_and_the_next_command_is_clean(self):
        p = self.start()
        p.send("garbage")
        p.send("\x03")
        p.wait_text("^C")
        p.wait_for(lambda: self.prompts(p) >= 2, what="a new prompt")
        p.send("version\r")
        p.wait_text(VERSION)
        self.assertNotIn("garbage", p.text().split("^C", 1)[1])
        p.send("\x04")
        self.assertEqual(p.wait_exit(), 0)

    def test_a_cjk_word_is_edited_in_the_middle_with_the_cursor_in_the_right_column(self):
        p = self.start()
        p.send("日本語")
        p.send("\x1b[D\x1b[D")   # between 日 and 本
        p.send("X")
        p.wait_for(lambda: self.prompt_line(p) == "日X本語", what="the edited CJK line")
        with p._cv:
            self.assertEqual(p.screen.col, len(PROMPT) + 2 + 1 + 0, "the cursor is after the X")
        p.send("\x03")

    def test_a_long_line_scrolls_in_a_narrow_terminal_and_stays_on_one_row(self):
        p = self.start(cols=20)
        p.send("x" * 70)
        p.quiet_for(0.2)
        with p._cv:
            self.assertEqual(p.screen.row, 0)
        p.send("\x03")

    def test_an_approval_prompt_works_in_cooked_mode_in_the_inline_shell(self):
        p = self.start()
        p.send(f"plugin install {ROOT.parent.parent / 'plugins' / 'dm'}\r")
        p.wait_text("asks for: network, fs:write:cwd")
        p.wait_text("Install it? [y/N]")
        p.send("n\r")
        p.wait_text("not installed")
        p.wait_for(lambda: self.prompts(p) >= 2, what="the prompt after the answer")
        p.send("plugin install " + str(ROOT.parent.parent / "plugins" / "dm") + "\r")
        p.wait_text("Install it? [y/N]")
        p.send("y\r")
        p.wait_text("installed dev.dock.dm")
        p.wait_for(lambda: self.prompts(p) >= 3, what="the prompt after the install")  # a ^D sent earlier is lost
        p.send("\x04")
        self.assertEqual(p.wait_exit(), 0)

    def test_a_key_is_echoed_within_20_ms(self):
        p = self.start()
        times = []
        for ch in "abcdefghijklmnopqrst":
            mark = len(p.data)
            start = time.monotonic()
            p.send(ch)
            p.wait_for(lambda: ch.encode() in p.data[mark:], timeout=5, what=f"the echo of {ch}")
            times.append(time.monotonic() - start)
            time.sleep(0.01)
        median = statistics.median(times)
        print(f"\n  echo: median {median * 1000:.1f} ms, max {max(times) * 1000:.1f} ms", file=sys.stderr)
        self.assertLessEqual(median, 0.02)
        self.assertLess(max(times), 0.25)
        p.send("\x03")

    def test_ctrl_z_stops_with_the_terminal_restored_and_fg_redraws_the_prompt(self):
        p = self.start()
        p.send("ver")
        p.wait_for(lambda: self.prompt_line(p) == "ver", what="the typed text")
        p.send("\x1a")   # raw mode: a key, which the editor turns into a stop
        p.wait_stopped()
        self.assert_restored(p)
        self.assertEqual(self.prompt_line(p), None, "the prompt was left on screen while stopped")
        p.cont()
        p.wait_for(lambda: self.prompt_line(p) == "ver", what="the prompt and the typed text again")
        p.send("sion\r")
        p.wait_text(VERSION)
        p.send("\x04")
        self.assertEqual(p.wait_exit(), 0)
        self.assert_restored(p)

    def test_sigterm_at_the_prompt_restores_the_terminal(self):
        p = self.start()
        p.signal(signal.SIGTERM)
        self.assertEqual(p.wait_exit(), -signal.SIGTERM)
        self.assert_restored(p)

    def test_sighup_at_the_prompt_restores_the_terminal(self):
        p = self.start()
        p.signal(signal.SIGHUP)
        self.assertEqual(p.wait_exit(), -signal.SIGHUP)
        self.assert_restored(p)

    def test_dock_plain_runs_the_line_based_shell_with_no_escape_sequences_of_ours(self):
        p = ptykit.Pty([sys.executable, "-m", "src"], env=shell_env(self.home, DOCK_PLAIN="1", DOCK_NO_READLINE="1"),
                       cwd=ROOT)
        self.addCleanup(p.close)
        p.wait_text(SHOWN)
        p.send("version\r")
        p.wait_text(VERSION)
        p.send("\x04")
        self.assertEqual(p.wait_exit(), 0)
        with p._cv:
            self.assertEqual(p.screen.seqs, [])


if __name__ == "__main__":
    unittest.main()
