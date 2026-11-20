"""Typed command inputs: unit tests (declarations, parsing) and e2e. Run `make test`."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import dockenv  # noqa: E402
from cli.inputs import InputSpec, UsageError, parse_args, parse_for, specs_from_toml  # noqa: E402
from cli.manifest import Manifest  # noqa: E402

FETCH = [
    {"name": "url", "type": "string", "required": True},
    {"name": "output", "type": "string", "flag": "--output", "short": "-o"},
    {"name": "retries", "type": "int", "flag": "--retries", "default": 3},
    {"name": "quiet", "type": "bool", "flag": "--quiet"},
]


class DeclarationTests(unittest.TestCase):
    def bad(self, raw, reason, name="x"):
        with self.assertRaises(ValueError) as cm:
            specs_from_toml("fetch", raw)
        self.assertEqual(str(cm.exception), f"command 'fetch': input '{name}': {reason}")

    def test_valid_fetch_declaration(self):
        specs = specs_from_toml("fetch", FETCH)
        self.assertEqual([s.name for s in specs], ["url", "output", "retries", "quiet"])
        self.assertTrue(specs[0].required)
        self.assertEqual(specs[1].short, "-o")
        self.assertEqual(specs[2].default, 3)

    def test_bad_type_or_default_is_refused(self):
        cases = [
            ("unknown type", [{"name": "x", "type": "path"}], "unknown type 'path'"),
            ("missing type", [{"name": "x"}], "missing 'type'"),
            ("duplicate name",
             [{"name": "x", "type": "int", "flag": "--a"}, {"name": "x", "type": "int", "flag": "--b"}],
             "duplicate name"),
            ("required with default", [{"name": "x", "type": "int", "required": True, "default": 1, "flag": "--x"}],
             "'required' and 'default' cannot be combined"),
            ("int default is a string", [{"name": "x", "type": "int", "default": "a", "flag": "--x"}],
             "default must be int"),
            ("int default is a bool", [{"name": "x", "type": "int", "default": True, "flag": "--x"}],
             "default must be int"),
            ("string default is an int", [{"name": "x", "type": "string", "default": 1, "flag": "--x"}],
             "default must be string"),
            ("bool default True", [{"name": "x", "type": "bool", "flag": "--x", "default": True}],
             "bool inputs cannot have a default"),
            ("bool default False", [{"name": "x", "type": "bool", "flag": "--x", "default": False}],
             "bool inputs cannot have a default"),
            ("bool default string", [{"name": "x", "type": "bool", "flag": "--x", "default": "no"}],
             "bool inputs cannot have a default"),
            ("bool required", [{"name": "x", "type": "bool", "flag": "--x", "required": True}],
             "bool inputs cannot be required"),
        ]
        for label, raw, reason in cases:
            with self.subTest(label):
                self.bad(raw, reason)

    def test_bad_flag_or_short_is_refused(self):
        cases = [
            ("duplicate flag",
             [{"name": "a", "type": "int", "flag": "--x"}, {"name": "x", "type": "int", "flag": "--x"}],
             "duplicate flag '--x'"),
            ("duplicate short",
             [{"name": "a", "type": "int", "flag": "--a", "short": "-x"},
              {"name": "x", "type": "int", "flag": "--b", "short": "-x"}], "duplicate flag '-x'"),
            ("short without flag", [{"name": "x", "type": "string", "short": "-x"}], "'short' needs a 'flag'"),
        ]
        for flag in ("-x", "--", "--a b", "x", "---x", "--é"):
            cases.append((f"malformed flag {flag!r}", [{"name": "x", "type": "string", "flag": flag}],
                          f"malformed flag '{flag}'"))
        for short in ("x", "--x", "-xy", "-"):
            cases.append((f"malformed short {short!r}",
                          [{"name": "x", "type": "string", "flag": "--x", "short": short}],
                          f"malformed short '{short}'"))
        for label, raw, reason in cases:
            with self.subTest(label):
                self.bad(raw, reason)

    def test_bad_positional_is_refused(self):
        cases = [
            ("positional bool", [{"name": "x", "type": "bool"}], "a positional input cannot be bool"),
            ("required positional after optional positional",
             [{"name": "a", "type": "string"}, {"name": "x", "type": "string", "required": True}],
             "required positional cannot follow an optional positional"),
        ]
        for label, raw, reason in cases:
            with self.subTest(label):
                self.bad(raw, reason)

    def test_missing_name(self):
        with self.assertRaises(ValueError) as cm:
            specs_from_toml("fetch", [{"type": "int"}])
        self.assertIn("command 'fetch'", str(cm.exception))

    def test_bool_required_false_is_fine(self):
        self.assertEqual(len(specs_from_toml("fetch", [{"name": "x", "type": "bool", "flag": "--x",
                                                        "required": False}])), 1)

    def test_unknown_key_is_ignored_for_forward_compatibility(self):
        specs = specs_from_toml("fetch", [{"name": "x", "type": "string", "help": "later", "flag": "--x"}])
        self.assertEqual(specs, [InputSpec("x", "string", False, None, "--x", None)])


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

    def test_positional_short_and_defaults(self):
        self.ok(["https://x", "-o", "a.txt"],
                {"url": "https://x", "output": "a.txt", "retries": 3, "quiet": False})

    def test_flag_and_separate_value_forms(self):
        cases = [
            (["--quiet", "--retries=5", "https://x"], {"url": "https://x", "retries": 5, "quiet": True}),
            (["https://x", "--retries", "5"], {"url": "https://x", "retries": 5, "quiet": False}),
        ]
        for args, expected in cases:
            with self.subTest(args=args):
                self.ok(args, expected)

    def test_dash_arguments(self):
        cases = [
            (["--", "-weird-url"], {"url": "-weird-url", "retries": 3, "quiet": False}),
            (["-"], {"url": "-", "retries": 3, "quiet": False}),
        ]
        for args, expected in cases:
            with self.subTest(args=args):
                self.ok(args, expected)

    def test_int_values_with_sign(self):
        cases = [
            (["u", "--retries", "-1"], {"url": "u", "retries": -1, "quiet": False}),
            (["u", "--retries=+7"], {"url": "u", "retries": 7, "quiet": False}),
            (["u", "--retries", "-0"], {"url": "u", "retries": 0, "quiet": False}),
        ]
        for args, expected in cases:
            with self.subTest(args=args):
                self.ok(args, expected)

    def test_short_equals_value(self):
        cases = [
            (["u", "-o=a.txt"], {"url": "u", "output": "a.txt", "retries": 3, "quiet": False}),
            (["u", "-o=a=b"], {"url": "u", "output": "a=b", "retries": 3, "quiet": False}),
        ]
        for args, expected in cases:
            with self.subTest(args=args):
                self.ok(args, expected)

    def test_non_integers_are_refused(self):
        cases = [
            (["u", "--retries", "many"], "input 'retries' must be an integer, got 'many'"),
            (["u", "--retries", "1.5"], "input 'retries' must be an integer, got '1.5'"),
            (["u", "--retries="], "input 'retries' must be an integer, got ''"),
            (["u", "--retries", "٣"], "input 'retries' must be an integer, got '٣'"),
            (["u", "--retries=１２"], "input 'retries' must be an integer, got '１２'"),
        ]
        for args, message in cases:
            with self.subTest(args=args):
                self.err(args, message)

    def test_flag_value_may_look_like_flag(self):
        self.ok(["u", "-o", "--quiet"], {"url": "u", "output": "--quiet", "retries": 3, "quiet": False})

    def test_non_ascii_passthrough(self):
        self.ok(["http://ü/日本", "-o", "ファイル"],
                {"url": "http://ü/日本", "output": "ファイル", "retries": 3, "quiet": False})

    def test_defaults_and_absent_values(self):
        cases = [
            ("int default zero is filled", [S("n", "int", flag="--n", default=0)], [], {"n": 0}),
            ("optional without default is absent", [S("a")], [], {}),
            ("optional positional default", [S("a", default="d")], [], {"a": "d"}),
            ("optional positional default overridden", [S("a", default="d")], ["z"], {"a": "z"}),
        ]
        for label, specs, args, expected in cases:
            with self.subTest(label):
                self.assertEqual(parse_args(specs, args), expected)

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


class ParseForTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = Manifest.load(ROOT / "tests" / "fixtures" / "probe")

    def test_declared_command_is_parsed(self):
        self.assertEqual(parse_for(self.manifest, "fetch", ["u"]),
                         {"url": "u", "retries": 3, "quiet": False})

    def test_undeclared_or_unknown_command_gives_none(self):
        for command, args in (("echoargs", ["--anything"]), ("nosuch", ["x"])):
            with self.subTest(command=command):
                self.assertIsNone(parse_for(self.manifest, command, args))

    def test_empty_declaration_gives_empty_inputs(self):
        self.assertEqual(parse_for(self.manifest, "noargs", []), {})


MANIFEST = """\
id = "dev.dock.bad"
version = "0.1.0"
api_version = "1"
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
        return subprocess.run([sys.executable, "-m", "cli", *args], input=stdin, capture_output=True,
                              text=True, check=False, env=cls.env, cwd=ROOT, timeout=120)

    def starts(self):
        log = Path(self.home) / "logs" / "dev.dock.probe.log"
        return log.read_text().count(" start ") if log.exists() else 0


class InstallRejectsBadDeclarations(DockCase):
    def test_bad_declaration_is_rejected_and_not_indexed(self):
        cases = {
            "unknown type": '{ name = "u", type = "path" }',
            "duplicate name": '{ name = "u", type = "int", flag = "--a" }, { name = "u", type = "int", flag = "--b" }',
            "short without flag": '{ name = "u", type = "string", short = "-u" }',
            "positional bool": '{ name = "u", type = "bool" }',
            "required+default": '{ name = "u", type = "int", flag = "--u", required = true, default = 1 }',
            "bad default": '{ name = "u", type = "int", flag = "--u", default = "x" }',
            "duplicate flag": '{ name = "a", type = "int", flag = "--u" }, { name = "u", type = "int", flag = "--u" }',
            "duplicate short": '{ name = "a", type = "int", flag = "--a", short = "-u" }, '
                               '{ name = "u", type = "int", flag = "--u", short = "-u" }',
            "malformed flag": '{ name = "u", type = "string", flag = "-u" }',
            "malformed short": '{ name = "u", type = "string", flag = "--u", short = "-uu" }',
            "required positional after optional": '{ name = "a", type = "string" }, '
                                                  '{ name = "u", type = "string", required = true }',
            "bool default": '{ name = "u", type = "bool", flag = "--u", default = true }',
            "bool required": '{ name = "u", type = "bool", flag = "--u", required = true }',
        }
        for label, inputs in cases.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as d:
                (Path(d) / "plugin.toml").write_text(MANIFEST.format(inputs=inputs))
                r = self.dock("plugin", "install", d)
                self.assertEqual(r.returncode, 1, r.stdout)
                self.assertIn("command 'fetch': input 'u'", r.stderr)
                self.assertNotIn("dev.dock.bad", self.dock("plugin", "list").stdout)


class TypedInputsE2E(DockCase):
    """Runs the probe plugin in a real container; the classes above start no plugin and need no runtime."""

    @classmethod
    def setUpClass(cls):
        dockenv.prebuild_plugins(ROOT / "tests" / "fixtures" / "probe")
        super().setUpClass()
        dockenv.write_config(cls.home, dockenv.require_runtime())  # a first install asks the runtime nothing, so setting the runtime here is only the last step of the setup

    def invoke(self, *args):
        r = self.dock("fetch", *args)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_declared_command_receives_inputs_and_raw_args(self):
        out = self.invoke("https://x", "-o", "a.txt")
        self.assertEqual(out["args"], ["https://x", "-o", "a.txt"])
        self.assertEqual(out["inputs"], {"url": "https://x", "output": "a.txt", "retries": 3, "quiet": False})

    def test_undeclared_command_gets_no_inputs(self):
        r = self.dock("echoargs", "--whatever", "x")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), {"args": ["--whatever", "x"], "inputs": "<absent>"})

    def test_usage_errors_exit_2_without_starting_plugin(self):
        cases = [
            ([], "missing required input 'url'"),
            (["u", "--retries", "many"], "input 'retries' must be an integer, got 'many'"),
            (["u", "--verbose"], "unknown flag '--verbose'"),
            (["u", "extra"], "unexpected argument 'extra'"),
            (["u", "-o"], "flag '-o' needs a value"),
            (["u", "--quiet=true"], "flag '--quiet' takes no value"),
            (["u", "--quiet", "--quiet"], "flag '--quiet' given more than once"),
        ]
        before = self.starts()
        for args, msg in cases:
            with self.subTest(args=args):
                r = self.dock("fetch", *args)
                self.assertEqual(r.returncode, 2)
                self.assertEqual(r.stderr.strip(), f"dock: fetch: {msg}")
        self.assertEqual(self.starts(), before)

    def test_empty_declaration_command_accepts_no_args_and_gets_empty_inputs(self):
        r = self.dock("noargs")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), {"args": [], "inputs": {}})

    def test_qualified_name_prefixes_error_with_command_part(self):
        r = self.dock("dev.dock.probe:fetch")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(r.stderr.strip(), "dock: fetch: missing required input 'url'")

    def test_shell_reports_error_continues_and_reuses_process(self):
        before = self.starts()
        r = self.dock("shell", stdin="fetch\nfetch https://x\nfetch https://y\nexit\n")
        self.assertIn("dock: fetch: missing required input 'url'", r.stderr)
        self.assertNotIn("exit 2", r.stderr)
        urls = [json.loads(l[l.index("{"):])["inputs"]["url"] for l in r.stdout.splitlines() if "{" in l]
        self.assertEqual(urls, ["https://x", "https://y"])
        self.assertEqual(self.starts(), before + 1)


if __name__ == "__main__":
    unittest.main()
