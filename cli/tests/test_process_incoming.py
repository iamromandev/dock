"""What the host does with a message a plugin sends it: a bad one never crashes the host."""

import io
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.process import PluginError, PluginProcess
from src.ui import PlainUI


def process(handlers):
    proc = PluginProcess(mock.Mock(), "unused.log", handlers, mock.Mock())
    proc._log = io.StringIO()
    proc.sent = []
    proc._send = proc.sent.append
    return proc


class Incoming(unittest.TestCase):
    def test_a_notification_handler_that_fails_unexpectedly_is_logged_and_the_host_goes_on(self):
        def boom(p):
            raise UnicodeEncodeError("utf-8", "x", 0, 1, "surrogates not allowed")

        proc = process({"output": boom})
        proc._handle_incoming({"method": "output", "params": {"text": "x"}})
        self.assertIn("[invalid output]", proc._log.getvalue())

    def test_an_unexpected_failure_logs_the_class_and_the_method_never_text_a_plugin_could_shape(self):
        def boom(p):
            raise RuntimeError(f"{p['text']}\n[forged] line")

        proc = process({"output": boom})
        proc._handle_incoming({"method": "output", "params": {"text": "secret\u2028text"}})
        self.assertEqual(proc._log.getvalue(), "[invalid output] RuntimeError\n")

    def test_a_reason_and_a_method_with_line_breaks_stay_on_one_log_line(self):
        def refuse(p):
            raise PluginError("bad\n[forged] line\u2028more\x1b[31m", -32602)

        proc = process({"bar": refuse})
        proc._handle_incoming({"method": "bar", "params": {}})
        proc._handle_incoming({"method": "evil\n[forged]\u2029x" + "y" * 500, "params": {}})
        proc._handle_incoming({"method": "bar\n[forged]", "params": 7})
        lines = proc._log.getvalue().split("\n")
        self.assertEqual(len(lines), 4, lines)  # three lines and the empty rest
        self.assertTrue(all(l.startswith(("[invalid ", "[unknown notification] ")) for l in lines[:3]), lines)
        self.assertTrue(all(len(l) < 300 for l in lines), lines)
        self.assertNotIn("\x1b", proc._log.getvalue())

    def test_a_request_handler_that_fails_unexpectedly_answers_with_an_error(self):
        def boom(p):
            raise RuntimeError("bug")

        proc = process({"form": boom})
        proc._handle_incoming({"id": 3, "method": "form", "params": {}})
        self.assertEqual(proc.sent[0]["id"], 3)
        self.assertIn("error", proc.sent[0])

    def test_params_that_are_not_an_object_are_dropped_from_a_notification(self):
        ui = PlainUI("p")
        proc = process(ui.handlers())
        for method in ("output", "log", "bar", "table", "panel", "clear"):
            for params in ("text", [1, 2], 7, True):
                with self.subTest(method=method, params=params):
                    proc._handle_incoming({"method": method, "params": params})
        self.assertIn("params must be an object", proc._log.getvalue())

    def test_params_that_are_not_an_object_get_an_error_reply_to_a_request(self):
        proc = process(PlainUI("p").handlers())
        for method in ("prompt_user", "form"):
            for params in ("text", [1], 7):
                with self.subTest(method=method, params=params):
                    proc.sent.clear()
                    proc._handle_incoming({"id": 5, "method": method, "params": params})
                    self.assertEqual(proc.sent[0]["error"]["code"], -32602)

    def test_text_with_a_lone_surrogate_does_not_crash_the_host(self):
        proc = process(PlainUI("p").handlers())
        with mock.patch.object(sys, "stdout", io.TextIOWrapper(io.BytesIO(), encoding="utf-8")) as out:
            proc._handle_incoming({"method": "output", "params": {"text": "a\ud800b\n"}})
            out.flush()
            self.assertEqual(out.buffer.getvalue(), "a�b\n".encode())


if __name__ == "__main__":
    unittest.main()
