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

from dock_host import uimsg
from dock_host.manifest import Manifest
from dock_host.process import PluginProcess
from src.ui import PlainUI

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
        run.send(
            "table",
            id="t",
            title="peers",
            columns=["peer", "down"],
            rows=[["1.2.3.4", i], ["5.6.7.8", 2 * i]],
        )
        run.send("panel", id="p", title="stats", items=[["speed", f"{i} MB/s"], ["peers", "2"]])


class PlainRendering(unittest.TestCase):
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


class Sanitising(unittest.TestCase):
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

    def test_prompt_at_end_of_input(self):
        with Run() as r, mock.patch("builtins.input", side_effect=EOFError):
            self.assertEqual(r.h["prompt_user"]({"prompt": "? "}), {"eof": True})


class Validation(unittest.TestCase):
    def test_invalid_messages_raise_invalid(self):
        cases = [
            ("bar", {"id": "x" * 65}),
            ("bar", {"label": "no id"}),
            ("table", {"id": "t", "columns": 1, "rows": []}),
            ("panel", {"id": "p"}),
            ("clear", {"id": "a", "kind": "form"}),
        ]
        for method, params in cases:
            with self.subTest(method=method, params=params), Run() as r:
                with self.assertRaises(uimsg.Invalid):
                    r.send(method, **params)
                self.assertEqual((r.out.getvalue(), r.err.getvalue()), ("", ""))


class Dispatch(unittest.TestCase):
    """AC2, AC4: what the process does with notifications and requests it cannot serve."""

    def proc(self, handlers):
        manifest = Manifest.load(ROOT / "tests" / "fixtures" / "probe")
        proc = PluginProcess(manifest, os.devnull, handlers, mock.Mock())
        proc._log = io.StringIO()
        proc._send = mock.Mock()
        return proc

    def test_an_invalid_request_gets_32602_with_the_reason(self):
        def bad_form(p):
            raise uimsg.Invalid("fields must be a list")

        proc = self.proc({"form": bad_form})
        proc._handle_incoming({"id": 7, "method": "form", "params": {}})
        proc._send.assert_called_once_with(
            {"id": 7, "error": {"code": -32602, "message": "fields must be a list"}}
        )


if __name__ == "__main__":
    unittest.main()
