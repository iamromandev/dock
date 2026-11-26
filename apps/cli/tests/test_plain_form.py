"""Forms in plain mode: prompts on stderr, answers as lines on stdin (R13, AC9). No runtime needed."""

import contextlib
import io
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import uimsg
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

    def test_handler_is_registered(self):
        self.assertIn("form", PlainUI("x").handlers())


class OriginalStrings(FormCase):
    """The terminal shows sanitised text; the plugin gets back the strings it sent."""

    def test_a_choice_answers_with_the_original_string_by_number_by_shown_text_and_by_default(self):
        field = choice(choices=("a\x1b[0m", "b\x07"), default="b\x07")
        self.assertEqual(self.ask([field], "1\n")[0], {"values": {"k": "a\x1b[0m"}})
        self.assertEqual(self.ask([field], "b�\n")[0], {"values": {"k": "b\x07"}})
        self.assertEqual(self.ask([field], "\n")[0], {"values": {"k": "b\x07"}})
        self.assertNotIn("\x1b", self.err_stream.getvalue())

    def test_an_empty_answer_to_a_text_field_gives_the_original_default(self):
        self.assertEqual(self.ask([text(default="d\x1b[0m")], "\n")[0], {"values": {"t": "d\x1b[0m"}})
        self.assertNotIn("\x1b", self.err_stream.getvalue())


class Text(FormCase):
    def test_answer_and_prompt(self):
        result, err = self.ask([text()], "Ada\n")
        self.assertEqual(result, {"values": {"t": "Ada"}})
        self.assertIn("Name", err)

    def test_title_is_shown_once(self):
        _, err = self.ask([text("a"), text("b")], "1\n2\n", title="Settings")
        self.assertEqual(err.count("Settings"), 1)

    def test_empty_means_default_or_empty_string(self):
        self.assertEqual(self.ask([text(default="Bob")], "\n")[0], {"values": {"t": "Bob"}})
        self.assertEqual(self.ask([text()], "\n")[0], {"values": {"t": ""}})

    def test_default_is_shown(self):
        self.assertIn("[Bob]", self.ask([text(default="Bob")], "\n")[1])

    def test_answer_keeps_inner_spaces_and_drops_only_the_newline(self):
        self.assertEqual(self.ask([text()], "  a b \n")[0], {"values": {"t": "  a b "}})

    def test_secret_from_a_terminal_is_read_without_echo(self):
        with mock.patch("getpass.getpass", return_value="hunter2") as gp:
            result, _ = self.ask([text(secret=True, label="Token")], "", tty=True)
        self.assertEqual(result, {"values": {"t": "hunter2"}})
        self.assertIs(gp.call_args.kwargs["stream"], self.err_stream)
        self.assertIn("Token", gp.call_args.args[0])

    def test_secret_default_is_not_printed(self):
        with mock.patch("getpass.getpass", return_value="") as gp:
            result, _ = self.ask([text(secret=True, default="s3cret")], "", tty=True)
        self.assertEqual(result, {"values": {"t": "s3cret"}})
        self.assertNotIn("s3cret", gp.call_args.args[0])

    def test_secret_from_a_pipe_is_a_plain_line(self):
        with mock.patch("getpass.getpass") as gp:
            self.assertEqual(self.ask([text(secret=True)], "pw\n")[0], {"values": {"t": "pw"}})
        gp.assert_not_called()

    def test_secret_at_end_of_input_from_a_terminal(self):
        with mock.patch("getpass.getpass", side_effect=EOFError):
            self.assertEqual(self.ask([text(secret=True)], "", tty=True)[0], {"eof": True})


class Confirm(FormCase):
    def test_accepted_spellings(self):
        for answer, want in (("y", True), ("Y", True), ("yes", True), ("YeS", True), (" y ", True),
                             ("n", False), ("N", False), ("no", False), ("NO", False)):
            with self.subTest(answer=answer):
                self.assertEqual(self.ask([confirm()], answer + "\n")[0], {"values": {"c": want}})

    def test_hint_follows_the_default(self):
        self.assertIn("(y/N)", self.ask([confirm(default=False)], "\n")[1])
        self.assertIn("(Y/n)", self.ask([confirm(default=True)], "\n")[1])
        self.assertIn("(y/n)", self.ask([confirm()], "y\n")[1])

    def test_empty_means_the_default(self):
        self.assertEqual(self.ask([confirm(default=True)], "\n")[0], {"values": {"c": True}})
        self.assertEqual(self.ask([confirm(default=False)], "\n")[0], {"values": {"c": False}})

    def test_empty_without_a_default_asks_again(self):
        result, err = self.ask([confirm()], "\ny\n")
        self.assertEqual(result, {"values": {"c": True}})
        self.assertEqual(err.count("Sure?"), 2)


class Choice(FormCase):
    def test_numbered_list_is_shown(self):
        _, err = self.ask([choice()], "1\n")
        for line in ("1) eu", "2) us", "3) asia"):
            self.assertIn(line, err)

    def test_by_number(self):
        self.assertEqual(self.ask([choice()], "2\n")[0], {"values": {"k": "us"}})

    def test_by_exact_text(self):
        self.assertEqual(self.ask([choice()], "asia\n")[0], {"values": {"k": "asia"}})

    def test_text_that_is_a_number_beats_nothing_but_a_valid_index(self):
        self.assertEqual(self.ask([choice(choices=("10", "20"))], "10\n")[0], {"values": {"k": "10"}})

    def test_empty_means_the_default(self):
        result, err = self.ask([choice(default="us")], "\n")
        self.assertEqual(result, {"values": {"k": "us"}})
        self.assertIn("[us]", err)

    def test_empty_without_a_default_asks_again(self):
        self.assertEqual(self.ask([choice()], "\n3\n")[0], {"values": {"k": "asia"}})

    def test_out_of_range_and_unknown_text_ask_again(self):
        self.assertEqual(self.ask([choice()], "0\n4\n1\n")[0], {"values": {"k": "eu"}})
        self.assertEqual(self.ask([choice()], "mars\n1\n")[0], {"values": {"k": "eu"}})


class Retries(FormCase):
    def test_three_bad_answers_are_invalid(self):
        with self.assertRaises(uimsg.Invalid) as cm:
            self.ask([choice()], "x\ny\nz\n1\n")
        self.assertEqual(str(cm.exception), "no valid answer for k")
        self.assertEqual(cm.exception.code, -32602)

    def test_the_third_try_may_still_succeed(self):
        self.assertEqual(self.ask([confirm()], "x\ny2\nn\n")[0], {"values": {"c": False}})

    def test_the_count_is_per_field(self):
        result, _ = self.ask([confirm("a"), confirm("b")], "x\ny\nx\nn\n")
        self.assertEqual(result, {"values": {"a": True, "b": False}})


class EndOfInput(FormCase):
    def test_eof_at_the_first_field(self):
        self.assertEqual(self.ask([text()], "")[0], {"eof": True})

    def test_eof_in_the_middle_drops_the_values_so_far(self):
        self.assertEqual(self.ask([text("a"), text("b")], "x\n")[0], {"eof": True})

    def test_eof_while_retrying(self):
        self.assertEqual(self.ask([confirm()], "maybe\n")[0], {"eof": True})

    def test_a_last_line_without_a_newline_is_an_answer(self):
        self.assertEqual(self.ask([text()], "Ada")[0], {"values": {"t": "Ada"}})


class Mixed(FormCase):
    def test_all_types_in_order(self):
        result, _ = self.ask([text("name"), text("pw", secret=True), confirm("ok"), choice("mirror")],
                             "Ada\nsecret\ny\nus\n")
        self.assertEqual(result, {"values": {"name": "Ada", "pw": "secret", "ok": True, "mirror": "us"}})

    def test_prompts_are_sanitised(self):
        _, err = self.ask([text(label="a\x1b[2Jb")], "x\n", title="t\x07")
        self.assertNotIn("\x1b", err)
        self.assertNotIn("\x07", err)

    def test_an_invalid_form_is_invalid(self):
        with self.assertRaises(uimsg.Invalid):
            PlainUI("x").form({"fields": "nope"})

    def test_ctrl_c_propagates(self):
        stdin = io.StringIO()
        stdin.readline = mock.Mock(side_effect=KeyboardInterrupt)
        with mock.patch.object(sys, "stdin", stdin), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                PlainUI("x").form({"fields": [text()]})


if __name__ == "__main__":
    unittest.main()
