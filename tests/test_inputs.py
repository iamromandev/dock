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

    def test_empty_declaration_is_valid(self):
        self.assertEqual(specs_from_toml("fetch", []), [])

    def test_unknown_type(self):
        self.bad([{"name": "x", "type": "path"}], "unknown type 'path'")

    def test_missing_type(self):
        self.bad([{"name": "x"}], "missing 'type'")

    def test_missing_name(self):
        with self.assertRaises(ValueError) as cm:
            specs_from_toml("fetch", [{"type": "int"}])
        self.assertIn("command 'fetch'", str(cm.exception))

    def test_duplicate_name(self):
        self.bad([{"name": "x", "type": "int", "flag": "--a"}, {"name": "x", "type": "int", "flag": "--b"}],
                 "duplicate name")

    def test_duplicate_flag(self):
        self.bad([{"name": "a", "type": "int", "flag": "--x"}, {"name": "x", "type": "int", "flag": "--x"}],
                 "duplicate flag '--x'")

    def test_duplicate_short(self):
        self.bad([{"name": "a", "type": "int", "flag": "--a", "short": "-x"},
                  {"name": "x", "type": "int", "flag": "--b", "short": "-x"}], "duplicate flag '-x'")

    def test_short_without_flag(self):
        self.bad([{"name": "x", "type": "string", "short": "-x"}], "'short' needs a 'flag'")

    def test_positional_bool(self):
        self.bad([{"name": "x", "type": "bool"}], "a positional input cannot be bool")

    def test_required_with_default(self):
        self.bad([{"name": "x", "type": "int", "required": True, "default": 1, "flag": "--x"}],
                 "'required' and 'default' cannot be combined")

    def test_default_type_mismatch(self):
        self.bad([{"name": "x", "type": "int", "default": "a", "flag": "--x"}], "default must be int")
        self.bad([{"name": "x", "type": "int", "default": True, "flag": "--x"}], "default must be int")
        self.bad([{"name": "x", "type": "string", "default": 1, "flag": "--x"}], "default must be string")

    def test_malformed_flag(self):
        for flag in ("-x", "--", "--a b", "x", "---x", "--é"):
            with self.subTest(flag=flag):
                self.bad([{"name": "x", "type": "string", "flag": flag}], f"malformed flag '{flag}'")

    def test_malformed_short(self):
        for short in ("x", "--x", "-xy", "-"):
            with self.subTest(short=short):
                self.bad([{"name": "x", "type": "string", "flag": "--x", "short": short}],
                         f"malformed short '{short}'")

    def test_required_positional_after_optional_positional(self):
        self.bad([{"name": "a", "type": "string"}, {"name": "x", "type": "string", "required": True}],
                 "required positional cannot follow an optional positional")

    def test_bool_cannot_have_default(self):
        for default in (True, False, "no"):
            with self.subTest(default=default):
                self.bad([{"name": "x", "type": "bool", "flag": "--x", "default": default}],
                         "bool inputs cannot have a default")

    def test_bool_cannot_be_required(self):
        self.bad([{"name": "x", "type": "bool", "flag": "--x", "required": True}],
                 "bool inputs cannot be required")

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

    def test_flags_before_positional_and_equals(self):
        self.ok(["--quiet", "--retries=5", "https://x"], {"url": "https://x", "retries": 5, "quiet": True})

    def test_space_separated_value(self):
        self.ok(["https://x", "--retries", "5"], {"url": "https://x", "retries": 5, "quiet": False})

    def test_double_dash_makes_rest_positional(self):
        self.ok(["--", "-weird-url"], {"url": "-weird-url", "retries": 3, "quiet": False})

    def test_negative_number_consumed_as_value(self):
        self.ok(["u", "--retries", "-1"], {"url": "u", "retries": -1, "quiet": False})

    def test_plus_sign_int(self):
        self.ok(["u", "--retries=+7"], {"url": "u", "retries": 7, "quiet": False})

    def test_short_flag_equals_value(self):
        self.ok(["u", "-o=a.txt"], {"url": "u", "output": "a.txt", "retries": 3, "quiet": False})

    def test_short_flag_equals_keeps_later_equals_in_value(self):
        self.ok(["u", "-o=a=b"], {"url": "u", "output": "a=b", "retries": 3, "quiet": False})

    def test_short_bool_flag_with_value(self):
        specs = [S("q", "bool", flag="--quiet", short="-q")]
        self.err(["-q=1"], "flag '-q' takes no value", specs=specs)

    def test_unknown_short_flag_with_equals(self):
        self.err(["u", "-z=1"], "unknown flag '-z'")

    def test_negative_zero_int(self):
        self.ok(["u", "--retries", "-0"], {"url": "u", "retries": 0, "quiet": False})

    def test_unicode_digits_are_not_integers(self):
        self.err(["u", "--retries", "\u0663"], "input 'retries' must be an integer, got '\u0663'")
        self.err(["u", "--retries=\uff11\uff12"], "input 'retries' must be an integer, got '\uff11\uff12'")

    def test_empty_equals_value_is_integer_error(self):
        self.err(["u", "--retries="], "input 'retries' must be an integer, got ''")

    def test_int_default_zero_is_filled(self):
        specs = [S("n", "int", flag="--n", default=0)]
        self.assertEqual(parse_args(specs, []), {"n": 0})

    def test_flag_value_may_look_like_flag(self):
        self.ok(["u", "-o", "--quiet"], {"url": "u", "output": "--quiet", "retries": 3, "quiet": False})

    def test_single_dash_is_positional(self):
        self.ok(["-"], {"url": "-", "retries": 3, "quiet": False})

    def test_non_ascii_passthrough(self):
        self.ok(["http://ü/日本", "-o", "ファイル"],
                {"url": "http://ü/日本", "output": "ファイル", "retries": 3, "quiet": False})

    def test_missing_required(self):
        self.err([], "missing required input 'url'")

    def test_bad_int(self):
        self.err(["u", "--retries", "many"], "input 'retries' must be an integer, got 'many'")
        self.err(["u", "--retries", "1.5"], "input 'retries' must be an integer, got '1.5'")

    def test_unknown_flag(self):
        self.err(["u", "--verbose"], "unknown flag '--verbose'")
        self.err(["u", "--verbose=1"], "unknown flag '--verbose'")
        self.err(["u", "-z"], "unknown flag '-z'")

    def test_extra_positional(self):
        self.err(["u", "extra"], "unexpected argument 'extra'")

    def test_flag_needs_value(self):
        self.err(["u", "-o"], "flag '-o' needs a value")
        self.err(["u", "--retries"], "flag '--retries' needs a value")

    def test_bool_with_value(self):
        self.err(["u", "--quiet=true"], "flag '--quiet' takes no value")

    def test_flag_twice(self):
        self.err(["u", "--quiet", "--quiet"], "flag '--quiet' given more than once")
        self.err(["u", "-o", "a", "--output", "b"], "flag '--output' given more than once")

    def test_empty_declaration_rejects_anything(self):
        self.assertEqual(parse_args([], []), {})
        self.err(["x"], "unexpected argument 'x'", specs=[])
        self.err(["--x"], "unknown flag '--x'", specs=[])

    def test_optional_without_default_is_absent(self):
        self.assertEqual(parse_args([S("a")], []), {})

    def test_optional_positional_default(self):
        self.assertEqual(parse_args([S("a", default="d")], []), {"a": "d"})
        self.assertEqual(parse_args([S("a", default="d")], ["z"]), {"a": "z"})

    def test_bool_default_false(self):
        self.assertEqual(parse_args([S("q", "bool", flag="--q")], []), {"q": False})

    def test_positional_int_validated(self):
        self.err(["x"], "input 'n' must be an integer, got 'x'", specs=[S("n", "int")])


class ParseForTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = Manifest.load(ROOT / "tests" / "fixtures" / "probe")

    def test_declared_command_is_parsed(self):
        self.assertEqual(parse_for(self.manifest, "fetch", ["u"]),
                         {"url": "u", "retries": 3, "quiet": False})

    def test_declared_command_usage_error(self):
        with self.assertRaises(UsageError):
            parse_for(self.manifest, "fetch", [])

    def test_undeclared_command_gives_none(self):
        self.assertIsNone(parse_for(self.manifest, "echoargs", ["--anything"]))

    def test_unknown_command_gives_none(self):
        self.assertIsNone(parse_for(self.manifest, "nosuch", ["x"]))

    def test_empty_declaration_gives_empty_inputs(self):
        self.assertEqual(parse_for(self.manifest, "noargs", []), {})


MANIFEST = """\
id = "dev.dock.bad"
version = "0.1.0"
api_version = "1"
[run]
any = {{ cmd = "python3", args = ["main.py"], requires = ["python3"] }}
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
                              text=True, check=False, env=cls.env, cwd=ROOT, timeout=30)

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

    def test_empty_declaration_command_rejects_any_arg(self):
        before = self.starts()
        for args, msg in ((["x"], "unexpected argument 'x'"), (["--x"], "unknown flag '--x'")):
            with self.subTest(args=args):
                r = self.dock("noargs", *args)
                self.assertEqual(r.returncode, 2)
                self.assertEqual(r.stderr.strip(), f"dock: noargs: {msg}")
        self.assertEqual(self.starts(), before)

    def test_short_flag_equals_value(self):
        self.assertEqual(self.invoke("u", "-o=a.txt")["inputs"]["output"], "a.txt")

    def test_bad_args_start_nothing_even_with_stale_provisioned_marker(self):
        home = tempfile.mkdtemp(prefix="dock-stale-")
        env = {**self.env, "DOCK_HOME": home}
        run = lambda *a: subprocess.run([sys.executable, "-m", "cli", *a], capture_output=True, text=True,
                                        env=env, cwd=ROOT, timeout=30)
        for name in ("probe", "provprobe"):
            self.assertEqual(run("plugin", "install", str(ROOT / "tests" / "fixtures" / name)).returncode, 0)
        marker = Path(home, "state", "dev.dock.provprobe.provisioned")
        marker.write_text(json.dumps({"version": "0.1.0", "since": 0}))
        r = run("fetch", "u", "--retries", "many")
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertEqual(r.stderr.strip(), "dock: fetch: input 'retries' must be an integer, got 'many'")
        for plugin_id in ("dev.dock.probe", "dev.dock.provprobe"):
            log = Path(home, "logs", f"{plugin_id}.log")
            self.assertEqual(log.read_text().count(" start ") if log.exists() else 0, 0, plugin_id)
        self.assertTrue(marker.exists())

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

    def test_shell_undeclared_command_gets_no_inputs_key(self):
        r = self.dock("shell", stdin="echoargs --whatever x\nexit\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        payloads = [json.loads(l[l.index("{"):]) for l in r.stdout.splitlines() if "{" in l]
        self.assertEqual(payloads, [{"args": ["--whatever", "x"], "inputs": "<absent>"}])


if __name__ == "__main__":
    unittest.main()
