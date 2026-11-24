"""LiveUI without a pty: the model, the text paths and the choice of renderer. Terminal output goes to a Screen."""

import contextlib
import io
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from cli import ui as ui_module  # noqa: E402
from cli.live import LiveUI  # noqa: E402
from cli.uimsg import Invalid  # noqa: E402
from screenkit import Screen  # noqa: E402


class FakeTerm:
    """What LiveUI needs of a Terminal, drawing on a Screen."""

    def __init__(self, cols=80, rows=10):
        self.screen = Screen(cols, rows)
        self.lock = threading.RLock()
        self.resized = False
        self.resized_at = 0.0
        self.fd_in = 0
        self.erase_region = self.on_resume = None
        self.modes = []

    def write(self, s):
        self.screen.feed(s.replace("\n", "\r\n"))  # the tty's ONLCR

    def size(self):
        return self.screen.cols, self.screen.rows

    def take_resized(self):
        was, self.resized = self.resized, False
        return was

    @contextlib.contextmanager
    def _mode(self, name):
        self.modes.append(name)
        yield
        self.modes.append("/" + name)

    def quiet(self):
        return self._mode("quiet")

    def raw(self):
        return self._mode("raw")

    def cooked(self):
        return self._mode("cooked")

    def flush_input(self):
        self.modes.append("flush")


class Live(unittest.TestCase):
    def setUp(self):
        self.term = FakeTerm()
        self.ui = LiveUI("probe", False, self.term, out_is_tty=True)
        self.h = self.ui.handlers()
        self.out = io.StringIO()
        patch = mock.patch.object(sys, "stdout", self.out)
        patch.start()
        self.addCleanup(patch.stop)

    def frame(self):
        with self.ui._cv:
            self.ui._frame()

    def test_handlers_cover_every_message_the_plugin_may_send(self):
        self.assertEqual(set(self.h), {"output", "log", "bar", "table", "panel", "clear", "prompt_user", "form"})

    def test_a_bar_is_drawn_and_a_cleared_one_is_gone_without_a_trace(self):
        self.h["bar"](dict(id="a", label="x", percent=50))
        self.frame()
        self.assertIn("x  [##########..........]  50.0%", self.term.screen.text())
        self.h["clear"](dict(id="a", kind="bar"))
        self.frame()
        self.assertEqual(self.term.screen.text(), "")

    def test_an_invalid_message_raises_and_changes_nothing(self):
        self.h["bar"](dict(id="a", label="x", percent=50))
        with self.assertRaises(Invalid):
            self.h["bar"](dict(id="a", percent="lots"))
        self.assertEqual([a.data["percent"] for a in self.ui._areas.items()], [50.0])

    def test_output_goes_above_the_region_and_a_partial_line_waits_in_it(self):
        self.h["bar"](dict(id="a", label="x", percent=10))
        self.frame()
        self.h["output"](dict(text="whole line\nunfinished"))
        self.assertEqual(self.out.getvalue(), "whole line\n")
        self.frame()
        text = self.term.screen.text()
        self.assertEqual(text.splitlines()[:2], ["unfinished", "x  [##..................]  10.0%"])
        self.h["output"](dict(text=" more\n"))
        self.assertEqual(self.out.getvalue(), "whole line\nunfinished more\n")

    def test_output_when_stdout_is_not_the_terminal_goes_through_as_it_is_and_leaves_the_region_alone(self):
        ui = LiveUI("probe", False, self.term, out_is_tty=False)
        ui.handlers()["bar"](dict(id="a", label="x", percent=10))
        with ui._cv:
            ui._frame()
        before = self.term.screen.text()
        ui.output(dict(text="no newline"))
        self.assertEqual(self.out.getvalue(), "no newline")
        self.assertEqual(self.term.screen.text(), before)

    def test_text_is_sanitised(self):
        self.h["output"](dict(text="a\x1b[31mb\tc\n"))
        self.h["log"](dict(level="info", message="x\x07y"))
        self.assertEqual(self.out.getvalue(), "a�[31mb    c\n")
        self.assertIn("[probe] info: x�y", self.term.screen.text())

    def test_a_debug_log_is_shown_only_when_verbose(self):
        self.h["log"](dict(level="debug", message="quiet"))
        self.assertNotIn("quiet", self.term.screen.text())
        LiveUI("probe", True, self.term, out_is_tty=True).log(dict(level="debug", message="loud"))
        self.assertIn("loud", self.term.screen.text())

    def test_end_writes_the_open_areas_once_as_plain_lines_and_nothing_else(self):
        self.ui.begin()
        self.addCleanup(self.ui.end)
        self.h["bar"](dict(id="a", label="x", percent=100, done=True))
        self.h["bar"](dict(id="b", label="y", percent=None))
        self.h["bar"](dict(id="gone", label="z", percent=5))
        self.h["clear"](dict(id="gone", kind="bar"))
        self.h["table"](dict(id="t", title="peers", columns=["a", "b"], rows=[["1", "2"]]))
        self.h["panel"](dict(id="p", title="stats", items=[["k", "v"]]))
        self.h["output"](dict(text="partial"))
        self.ui.end()
        self.assertEqual(self.term.screen.text().splitlines(), [
            "partial", "x  [####################]  100.0%", "y  …", "peers", "a  b", "1  2", "stats", "k  v"])
        self.assertTrue(self.term.screen.cursor_visible)
        self.assertEqual(self.term.modes, ["quiet", "/quiet", "flush"])

    def test_end_without_begin_is_harmless(self):
        self.ui.end()
        self.assertEqual(self.term.screen.text(), "")

    def test_a_second_run_starts_clean(self):
        self.ui.begin()
        self.h["bar"](dict(id="a", label="x", percent=1))
        self.ui.end()
        self.ui.begin()
        self.assertEqual(self.ui._areas.items(), [])
        self.ui.end()

    def test_a_33rd_area_is_refused(self):
        for i in range(32):
            self.h["bar"](dict(id=f"a{i}", percent=1))
        with self.assertRaises(Invalid):
            self.h["bar"](dict(id="one-too-many", percent=1))

    # forms ----------------------------------------------------------------------------
    def test_a_form_without_a_terminal_on_stdin_is_asked_as_plain_lines_and_the_region_is_out_of_the_way(self):
        self.h["bar"](dict(id="a", label="x", percent=10))
        self.frame()
        fields = [{"name": "n", "label": "Name", "type": "text"}]
        with mock.patch.object(sys, "stdin", io.StringIO("Ada\n")), mock.patch.object(sys, "stderr", io.StringIO()):
            self.assertEqual(self.h["form"](dict(fields=fields)), {"values": {"n": "Ada"}})
        self.assertEqual(self.term.screen.text(), "", "the region was still drawn while the form was asked")
        self.frame()
        self.assertIn("x  [##", self.term.screen.text())

    def test_a_live_form_answers_with_the_original_strings_while_it_shows_the_sanitised_ones(self):
        fields = [{"name": "k", "label": "K", "type": "choice", "choices": ["a\x1b[0m", "b\x07"], "default": "b\x07"},
                  {"name": "d", "label": "D", "type": "choice", "choices": ["a\x1b[0m", "b\x07"]},
                  {"name": "t", "label": "T", "type": "text", "default": "x\x1b[31m"}]

        class Tty(io.StringIO):
            def isatty(self):
                return True

        with mock.patch.object(sys, "stdin", Tty()), \
                mock.patch.object(self.ui, "_key", side_effect=["enter", "down", "enter", "enter"]):
            answer = self.h["form"](dict(fields=fields))
        self.assertEqual(answer, {"values": {"k": "b\x07", "d": "b\x07", "t": "x\x1b[31m"}})
        self.assertNotIn("\x1b[31m", self.term.screen.text())

    def test_an_invalid_form_is_refused_before_anything_is_asked(self):
        with self.assertRaises(Invalid):
            self.h["form"](dict(fields=[{"name": "c", "label": "L", "type": "choice", "choices": []}]))

    def test_a_prompt_runs_cooked_with_the_region_out_of_the_way(self):
        self.h["bar"](dict(id="a", label="x", percent=10))
        self.frame()
        with mock.patch("builtins.input", return_value="yes") as ask:
            self.assertEqual(self.h["prompt_user"](dict(prompt="ok? ")), {"text": "yes"})
        ask.assert_called_once_with("ok? ")
        self.assertEqual(self.term.modes, ["cooked", "flush", "/cooked"], "keys typed earlier are not the answer")
        self.assertEqual(self.term.screen.text(), "")

    def test_a_prompt_at_the_end_of_input_answers_eof(self):
        with mock.patch("builtins.input", side_effect=EOFError):
            self.assertEqual(self.h["prompt_user"](dict(prompt="ok? ")), {"eof": True})

    def test_ctrl_c_during_a_prompt_propagates_and_drawing_resumes(self):
        with mock.patch("builtins.input", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.h["prompt_user"](dict(prompt="ok? "))
        self.assertFalse(self.ui._paused)


class Halting(unittest.TestCase):
    """Drawing stops for good when the terminal is given back (Ctrl-Z, a signal), and when the renderer fails."""

    def setUp(self):
        self.term = FakeTerm()
        self.ui = LiveUI("probe", False, self.term, out_is_tty=True)
        self.ui.begin()
        self.addCleanup(self.ui.end)

    def frame(self):
        with self.ui._cv:
            self.ui._frame()

    def test_nothing_is_drawn_after_the_region_was_erased_for_a_signal_until_the_run_resumes(self):
        self.ui.handlers()["bar"](dict(id="a", label="x", percent=10))
        self.frame()
        self.term.erase_region()   # what Terminal.restore does before it stops or ends the process
        self.assertEqual(self.term.screen.text(), "")
        self.ui.handlers()["bar"](dict(id="a", label="x", percent=20))
        self.frame()
        self.assertEqual(self.term.screen.text(), "", "the render thread drew after the terminal was given back")
        self.term.on_resume()
        self.frame()
        self.assertIn("20.0%", self.term.screen.text())

    def test_a_form_after_a_renderer_failure_is_asked_as_plain_lines_in_a_line_mode_terminal(self):
        with self.ui._cv:
            self.ui._failed = True
        fields = [{"name": "n", "label": "Name", "type": "text"}]

        class Tty(io.StringIO):
            def isatty(self):
                return True

        with mock.patch.object(sys, "stdin", Tty("Ada\n")), mock.patch.object(sys, "stderr", io.StringIO()):
            self.assertEqual(self.ui.handlers()["form"](dict(fields=fields)), {"values": {"n": "Ada"}})
        self.assertIn("cooked", self.term.modes)
        self.assertNotIn("raw", self.term.modes)

    def test_a_question_is_still_drawn_when_the_renderer_fails_while_it_is_open(self):
        self.ui.handlers()["bar"](dict(id="a", label="x", percent=20))
        with self.ui._cv:
            self.ui._failed = True
        self.ui._set_tail(["Name? Ad"])
        self.assertEqual(self.term.screen.text(), "Name? Ad")
        self.ui._set_tail([])
        self.assertEqual(self.term.screen.text(), "")

    def test_a_failed_renderer_stays_failed_after_a_prompt(self):
        with self.ui._cv:
            self.ui._failed = True
        with mock.patch("builtins.input", return_value="x"):
            self.ui.handlers()["prompt_user"](dict(prompt="? "))
        self.ui.handlers()["bar"](dict(id="a", label="x", percent=20))
        self.frame()
        self.assertEqual(self.term.screen.text(), "")


class MakeUI(unittest.TestCase):
    def test_a_terminal_gives_the_live_renderer_and_none_gives_plain(self):
        self.assertIsInstance(ui_module.make_ui("p", False, FakeTerm()), LiveUI)
        self.assertIsInstance(ui_module.make_ui("p", False), ui_module.PlainUI)


if __name__ == "__main__":
    unittest.main()
