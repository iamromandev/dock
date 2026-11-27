"""Forms in plain mode: prompts on stderr, answers as lines on stdin (R13, AC9). No runtime needed."""

import contextlib
import io
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.ui import PlainUI


def text(name="t", label="Name", **kw):
    return {"name": name, "label": label, "type": "text", **kw}


def confirm(name="c", label="Sure?", **kw):
    return {"name": name, "label": label, "type": "confirm", **kw}


def choice(name="k", label="Mirror", choices=("eu", "us", "asia"), **kw):
    return {"name": name, "label": label, "type": "choice", "choices": list(choices), **kw}


class FormCase(unittest.TestCase):
    def ask(self, fields, stdin="", title=None, tty=False):
        """Run a form with `stdin` as the answers; returns (result, what was written to stderr)."""
        err = self.err_stream = io.StringIO()
        stdin_io = io.StringIO(stdin)
        stdin_io.isatty = lambda: tty
        p = {"fields": fields, **({"title": title} if title else {})}
        with mock.patch.object(sys, "stdin", stdin_io), contextlib.redirect_stderr(err):
            self.result = PlainUI("dev.t.p").form(p)
        return self.result, err.getvalue()


class Text(FormCase):
    def test_secret_at_end_of_input_from_a_terminal(self):
        with mock.patch("getpass.getpass", side_effect=EOFError):
            self.assertEqual(self.ask([text(secret=True)], "", tty=True)[0], {"eof": True})


class Mixed(FormCase):
    def test_all_types_in_order(self):
        result, _ = self.ask(
            [text("name"), text("pw", secret=True), confirm("ok"), choice("mirror")], "Ada\nsecret\ny\nus\n"
        )
        self.assertEqual(result, {"values": {"name": "Ada", "pw": "secret", "ok": True, "mirror": "us"}})


if __name__ == "__main__":
    unittest.main()
