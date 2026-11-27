"""Typed command inputs: unit tests (declarations, parsing) and e2e. Run `make test`."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from dock_host.inputs import InputSpec, UsageError, check_values, parse_args, specs_from_toml

FETCH = [
    {"name": "url", "type": "string", "required": True},
    {"name": "output", "type": "string", "flag": "--output", "short": "-o"},
    {"name": "retries", "type": "int", "flag": "--retries", "default": 3},
    {"name": "quiet", "type": "bool", "flag": "--quiet"},
]


class CheckValuesTests(unittest.TestCase):
    SPECS = specs_from_toml("fetch", FETCH)

    def fails(self, values, text):
        with self.assertRaises(UsageError) as cm:
            check_values(self.SPECS, values)
        self.assertIn(text, str(cm.exception))

    def test_wrong_types_are_rejected(self):
        self.fails({"url": "u", "retries": "x"}, "must be an integer")
        self.fails({"url": "u", "retries": True}, "must be an integer")
        self.fails({"url": 3}, "must be a string")
        self.fails({"url": "u", "quiet": "yes"}, "must be a boolean")


class DeclarationTests(unittest.TestCase):
    def bad(self, raw, reason, name="x"):
        with self.assertRaises(ValueError) as cm:
            specs_from_toml("fetch", raw)
        self.assertEqual(str(cm.exception), f"command 'fetch': input '{name}': {reason}")

    def test_bad_declaration_is_refused(self):  # type, default, flag and short
        cases = [
            ("unknown type", [{"name": "x", "type": "path"}], "unknown type 'path'"),
            ("missing type", [{"name": "x"}], "missing 'type'"),
            (
                "duplicate name",
                [{"name": "x", "type": "int", "flag": "--a"}, {"name": "x", "type": "int", "flag": "--b"}],
                "duplicate name",
            ),
            (
                "required with default",
                [{"name": "x", "type": "int", "required": True, "default": 1, "flag": "--x"}],
                "'required' and 'default' cannot be combined",
            ),
            (
                "int default is a string",
                [{"name": "x", "type": "int", "default": "a", "flag": "--x"}],
                "default must be int",
            ),
            (
                "int default is a bool",
                [{"name": "x", "type": "int", "default": True, "flag": "--x"}],
                "default must be int",
            ),
            (
                "string default is an int",
                [{"name": "x", "type": "string", "default": 1, "flag": "--x"}],
                "default must be string",
            ),
            (
                "bool default True",
                [{"name": "x", "type": "bool", "flag": "--x", "default": True}],
                "bool inputs cannot have a default",
            ),
            (
                "bool default False",
                [{"name": "x", "type": "bool", "flag": "--x", "default": False}],
                "bool inputs cannot have a default",
            ),
            (
                "bool default string",
                [{"name": "x", "type": "bool", "flag": "--x", "default": "no"}],
                "bool inputs cannot have a default",
            ),
            (
                "bool required",
                [{"name": "x", "type": "bool", "flag": "--x", "required": True}],
                "bool inputs cannot be required",
            ),
        ]
        cases += [
            (
                "duplicate flag",
                [{"name": "a", "type": "int", "flag": "--x"}, {"name": "x", "type": "int", "flag": "--x"}],
                "duplicate flag '--x'",
            ),
            (
                "duplicate short",
                [
                    {"name": "a", "type": "int", "flag": "--a", "short": "-x"},
                    {"name": "x", "type": "int", "flag": "--b", "short": "-x"},
                ],
                "duplicate flag '-x'",
            ),
            (
                "short without flag",
                [{"name": "x", "type": "string", "short": "-x"}],
                "'short' needs a 'flag'",
            ),
        ]
        for flag in ("-x", "--", "--a b", "x", "---x", "--é"):
            cases.append(
                (
                    f"malformed flag {flag!r}",
                    [{"name": "x", "type": "string", "flag": flag}],
                    f"malformed flag '{flag}'",
                )
            )
        for short in ("x", "--x", "-xy", "-"):
            cases.append(
                (
                    f"malformed short {short!r}",
                    [{"name": "x", "type": "string", "flag": "--x", "short": short}],
                    f"malformed short '{short}'",
                )
            )
        for label, raw, reason in cases:
            with self.subTest(label):
                self.bad(raw, reason)


def S(name, type="string", **kw):
    return InputSpec(name, type, **kw)


class ParseTests(unittest.TestCase):
    specs = specs_from_toml("fetch", FETCH)

    def ok(self, args, expected):
        self.assertEqual(parse_args(self.specs, args), expected)

    def err(self, args, message, specs=None):
        with self.assertRaises(UsageError) as cm:
            parse_args(self.specs if specs is None else specs, args)
        self.assertEqual(str(cm.exception), message)

    def test_dash_arguments(self):
        cases = [
            (["--", "-weird-url"], {"url": "-weird-url", "retries": 3, "quiet": False}),
            (["-"], {"url": "-", "retries": 3, "quiet": False}),
        ]
        for args, expected in cases:
            with self.subTest(args=args):
                self.ok(args, expected)

    def test_usage_errors_are_refused(self):
        cases = [
            ([], "missing required input 'url'", None),
            (["u", "--verbose"], "unknown flag '--verbose'", None),
            (["u", "--verbose=1"], "unknown flag '--verbose'", None),
            (["u", "-z"], "unknown flag '-z'", None),
            (["u", "extra"], "unexpected argument 'extra'", None),
            (["u", "-o"], "flag '-o' needs a value", None),
            (["u", "--retries"], "flag '--retries' needs a value", None),
            (["u", "--quiet=true"], "flag '--quiet' takes no value", None),
            (["u", "--quiet", "--quiet"], "flag '--quiet' given more than once", None),
            (["u", "-o", "a", "--output", "b"], "flag '--output' given more than once", None),
            (["x"], "unexpected argument 'x'", []),
            (["--x"], "unknown flag '--x'", []),
            (["x"], "input 'n' must be an integer, got 'x'", [S("n", "int")]),
        ]
        for args, message, specs in cases:
            with self.subTest(args=args, specs=specs):
                self.err(args, message, specs=specs)
        self.assertEqual(parse_args([], []), {})


MANIFEST = """\
id = "dev.dock.bad"
version = "0.1.0"
api_version = "2"
[run]
container = {{ cmd = "python3", args = ["/plugin/main.py"] }}
[[commands]]
name = "fetch"
inputs = [ {inputs} ]
"""


class DockCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.mkdtemp(prefix="dock-home-")
        cls.env = {**os.environ, "DOCK_HOME": cls.home, "PYTHONPATH": str(ROOT)}
        r = cls.dock("plugin", "install", str(ROOT / "tests" / "fixtures" / "probe"))
        assert r.returncode == 0, r.stderr

    @classmethod
    def dock(cls, *args, stdin=""):
        return subprocess.run(
            [sys.executable, "-m", "src", *args],
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
            env=cls.env,
            cwd=ROOT,
            timeout=120,
        )

    def starts(self):
        log = Path(self.home) / "logs" / "dev.dock.probe.log"
        return log.read_text().count(" start ") if log.exists() else 0


if __name__ == "__main__":
    unittest.main()
