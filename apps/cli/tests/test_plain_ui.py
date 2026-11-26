"""PlainUI (what dock prints when it is not drawing live), mode choice, and how a process dispatches UI messages.

No runtime needed.
"""

import contextlib
import io
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import ui as ui_module
from src import uimsg
from src.manifest import Manifest
from src.process import PluginProcess
from src.ui import PlainUI, live_mode, make_ui

FFFD = "�"


class Run:
    """A PlainUI with stdout and stderr captured."""

    def __init__(self, verbose=False):
        self.out, self.err = io.StringIO(), io.StringIO()
        self.ui = PlainUI("dev.t.p", verbose)
        self.h = self.ui.handlers()

    def __enter__(self):
        self._stack = contextlib.ExitStack()
        self._stack.enter_context(contextlib.redirect_stdout(self.out))
        self._stack.enter_context(contextlib.redirect_stderr(self.err))
        return self

    def __exit__(self, *exc):
        self._stack.close()

    def send(self, method, **params):
        self.h[method](params)


def script(run):
    """The AC3 script: 2 bars, a table and a panel, updated 100 times."""
    run.ui.begin()
    for i in range(1, 101):
        run.send("bar", id="a", label="first", percent=i, message=f"{i} of 100")
        run.send("bar", id="b", label="second", percent=i / 2)
        run.send("table", id="t", title="peers", columns=["peer", "down"], rows=[["1.2.3.4", i], ["5.6.7.8", 2 * i]])
        run.send("panel", id="p", title="stats", items=[["speed", f"{i} MB/s"], ["peers", "2"]])


class Handlers(unittest.TestCase):
    def test_handler_names(self):
        self.assertEqual(set(Run().h), {"output", "log", "bar", "table", "panel", "clear", "prompt_user", "form"})
        self.assertNotIn("progress", Run().h)

    def test_the_old_name_is_gone(self):
        self.assertFalse(hasattr(ui_module, "TerminalUI"))
        self.assertFalse(hasattr(PlainUI, "progress"))


class PlainRendering(unittest.TestCase):
    def test_script_prints_each_area_once_at_the_end_without_escapes(self):
        with Run() as r:
            script(r)
            self.assertEqual(r.err.getvalue(), "")  # nothing before the run ends
            r.ui.end()
        text = r.err.getvalue()
        self.assertNotIn("\x1b", text)
        self.assertEqual(text.splitlines(), [
            "first  [####################]  100.0%  100 of 100",
            "second  [##########..........]  50.0%",
            "peers", "peer     down", "1.2.3.4  100", "5.6.7.8  200",
            "stats", "speed  100 MB/s", "peers  2",
        ])
        self.assertEqual(r.out.getvalue(), "")

    def test_end_resets_so_a_second_run_prints_only_its_own(self):
        with Run() as r:
            script(r)
            r.ui.end()
            r.err.truncate(0)
            r.err.seek(0)
            r.ui.begin()
            r.ui.end()
        self.assertEqual(r.err.getvalue(), "")

    def test_a_done_bar_prints_at_once_and_only_once(self):
        with Run() as r:
            r.ui.begin()
            r.send("bar", id="a", label="x", percent=40)
            self.assertEqual(r.err.getvalue(), "")
            r.send("bar", id="a", label="x", percent=100, message="all", done=True)
            self.assertEqual(r.err.getvalue(), "x  [####################]  100.0%  all\n")
            r.ui.end()
        self.assertEqual(r.err.getvalue().count("\n"), 1)

    def test_clear_prints_a_table_or_panel_but_not_a_bar(self):
        with Run() as r:
            r.ui.begin()
            r.send("table", id="t", columns=["a"], rows=[["1"]])
            r.send("panel", id="p", items=[["k", "v"]])
            r.send("bar", id="b", label="x", percent=3)
            r.send("clear", id="t", kind="table")
            self.assertEqual(r.err.getvalue(), "a\n1\n")
            r.send("clear", id="p", kind="panel")
            r.send("clear", id="b", kind="bar")
            r.send("clear", id="nope", kind="bar")  # unknown: ignored
            r.ui.end()
        self.assertEqual(r.err.getvalue(), "a\n1\nk  v\n")

    def test_a_cleared_bar_that_was_printed_is_not_printed_again(self):
        with Run() as r:
            r.ui.begin()
            r.send("bar", id="b", label="x", percent=100, done=True)
            r.send("clear", id="b", kind="bar")
            r.ui.end()
        self.assertEqual(r.err.getvalue().count("\n"), 1)

    def test_a_bar_with_unknown_percent(self):
        with Run() as r:
            r.ui.begin()
            r.send("bar", id="b", label="meta", message="fetching")
            r.ui.end()
        self.assertEqual(r.err.getvalue(), "meta  …  fetching\n")


class Sanitising(unittest.TestCase):
    def test_output_keeps_newline_and_shows_a_tab_as_spaces(self):
        with Run() as r:
            r.send("output", text="a\tb\nc\x1b[31m\x07\x9b")
        self.assertEqual(r.out.getvalue(), f"a    b\nc{FFFD}[31m{FFFD}{FFFD}")

    def test_log_is_sanitised_and_keeps_newlines(self):
        with Run() as r:
            r.send("log", level="info", message="a\x1b]0;x\x07\nb")
        self.assertEqual(r.err.getvalue(), f"[dev.t.p] info: a{FFFD}]0;x{FFFD}\nb\n")

    def test_debug_log_is_hidden_unless_verbose(self):
        with Run() as r:
            r.send("log", level="debug", message="x")
        self.assertEqual(r.err.getvalue(), "")
        with Run(verbose=True) as r:
            r.send("log", level="debug", message="x")
        self.assertEqual(r.err.getvalue(), "[dev.t.p] debug: x\n")

    def test_bar_table_and_panel_text_is_sanitised(self):
        with Run() as r:
            r.ui.begin()
            r.send("bar", id="b", label="l\x1b", message="m\x9b", done=True)
            r.send("table", id="t", title="t\x1b[2J", columns=["c\x07"], rows=[["r\x1b"]])
            r.send("panel", id="p", title="p\x1b", items=[["k\x1b", "v\x00"]])
            r.ui.end()
        self.assertNotIn("\x1b", r.err.getvalue())
        self.assertEqual(r.err.getvalue().count(FFFD), 8)

    def test_prompt_is_sanitised(self):
        with Run() as r, mock.patch("builtins.input", return_value="yes") as inp:
            self.assertEqual(r.h["prompt_user"]({"prompt": "go\x1b[2J? "}), {"text": "yes"})
        inp.assert_called_once_with(f"go{FFFD}[2J? ")

    def test_prompt_at_end_of_input(self):
        with Run() as r, mock.patch("builtins.input", side_effect=EOFError):
            self.assertEqual(r.h["prompt_user"]({"prompt": "? "}), {"eof": True})

    def test_warn_is_sanitised(self):
        with Run() as r:
            r.ui.warn("bad \x1b[2J thing")
        self.assertEqual(r.err.getvalue(), f"dock: bad {FFFD}[2J thing\n")


class Validation(unittest.TestCase):
    def test_invalid_messages_raise_invalid(self):
        cases = [("bar", {"id": "x" * 65}), ("bar", {"label": "no id"}), ("table", {"id": "t", "columns": 1, "rows": []}),
                 ("panel", {"id": "p"}), ("clear", {"id": "a", "kind": "form"})]
        for method, params in cases:
            with self.subTest(method=method, params=params), Run() as r:
                with self.assertRaises(uimsg.Invalid):
                    r.send(method, **params)
                self.assertEqual((r.out.getvalue(), r.err.getvalue()), ("", ""))

    def test_the_33rd_area_is_invalid(self):
        with Run() as r:
            for i in range(32):
                r.send("bar", id=f"b{i}")
            with self.assertRaises(uimsg.Invalid):
                r.send("bar", id="b32")


class Modes(unittest.TestCase):
    def tty(self, isatty, **env):
        base = {k: v for k, v in os.environ.items() if k not in ("TERM", "DOCK_PLAIN")}
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(sys, "stderr", mock.Mock(isatty=lambda: isatty)))
        stack.enter_context(mock.patch.dict(os.environ, {**base, **env}, clear=True))
        self.addCleanup(stack.close)

    def test_live_when_stderr_is_a_terminal(self):
        self.tty(True, TERM="xterm-256color")
        self.assertTrue(live_mode())

    def test_plain_when_not_a_terminal(self):
        self.tty(False, TERM="xterm")
        self.assertFalse(live_mode())

    def test_plain_for_dumb_or_missing_term(self):
        for term in ("dumb", ""):
            with self.subTest(term=term):
                self.tty(True, TERM=term)
                self.assertFalse(live_mode())
        self.tty(True)
        self.assertFalse(live_mode())

    def test_dock_plain_forces_plain(self):
        self.tty(True, TERM="xterm", DOCK_PLAIN="1")
        self.assertFalse(live_mode())
        self.tty(True, TERM="xterm", DOCK_PLAIN="")
        self.assertTrue(live_mode())

    def test_plain_without_termios(self):
        self.tty(True, TERM="xterm")
        with mock.patch.dict(sys.modules, {"termios": None}):
            self.assertFalse(live_mode())

    def test_make_ui_is_plain_for_now(self):
        ui = make_ui("dev.t.p", True)
        self.assertIsInstance(ui, PlainUI)
        self.assertEqual((ui.plugin_id, ui.verbose), ("dev.t.p", True))


class Dispatch(unittest.TestCase):
    """AC2, AC4: what the process does with notifications and requests it cannot serve."""

    def proc(self, handlers):
        manifest = Manifest.load(ROOT / "tests" / "fixtures" / "probe")
        proc = PluginProcess(manifest, os.devnull, handlers, mock.Mock())
        proc._log = io.StringIO()
        proc._send = mock.Mock()
        return proc

    def test_unknown_notification_is_logged(self):
        proc = self.proc({})
        proc._handle_incoming({"method": "progress", "params": {"percent": 5}})
        self.assertEqual(proc._log.getvalue(), "[unknown notification] progress\n")
        proc._send.assert_not_called()

    def test_unknown_notification_name_with_controls_is_logged_safely(self):
        proc = self.proc({})
        proc._handle_incoming({"method": "x\ny"})
        self.assertEqual(proc._log.getvalue().count("\n"), 1)

    def test_invalid_notification_is_logged_and_dropped(self):
        h = Run().h
        proc = self.proc(h)
        proc._handle_incoming({"method": "bar", "params": {"id": "x" * 65}})
        self.assertEqual(proc._log.getvalue(), f"[invalid bar] id is longer than {uimsg.MAX_ID} characters\n")
        proc._send.assert_not_called()

    def test_valid_notification_is_handled_quietly(self):
        with Run() as r:
            proc = self.proc(r.h)
            proc._handle_incoming({"method": "bar", "params": {"id": "a", "percent": 3}})
            self.assertEqual(proc._log.getvalue(), "")
            self.assertEqual([a.id for a in r.ui._areas.items()], ["a"])

    def test_an_invalid_request_gets_32602_with_the_reason(self):
        def bad_form(p):
            raise uimsg.Invalid("fields must be a list")

        proc = self.proc({"form": bad_form})
        proc._handle_incoming({"id": 7, "method": "form", "params": {}})
        proc._send.assert_called_once_with({"id": 7, "error": {"code": -32602, "message": "fields must be a list"}})

    def test_unknown_request_is_still_method_not_found(self):
        proc = self.proc({})
        proc._handle_incoming({"id": 7, "method": "nope", "params": {}})
        self.assertEqual(proc._send.call_args.args[0]["error"]["code"], -32601)


if __name__ == "__main__":
    unittest.main()
