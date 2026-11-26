"""End-to-end: real host, real plugin processes, throwaway DOCK_HOME. Run `make test`."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
PROBE = ROOT / "tests" / "fixtures" / "probe"

import dockenv


class DockE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        dockenv.prebuild_plugins(PROBE)
        cls.home = tempfile.mkdtemp(prefix="dock-home-")
        cls.env = {**os.environ, "DOCK_HOME": cls.home, "PYTHONPATH": str(ROOT)}
        r = cls.dock("plugin", "install", str(PROBE))
        assert r.returncode == 0, r.stderr
        dockenv.write_config(cls.home, dockenv.require_runtime())  # a first install asks the runtime nothing, so setting the runtime here is only the last step of the setup

    @classmethod
    def dock(cls, *args, stdin="", env=None, **kw):
        return subprocess.run(
            [sys.executable, "-m", "src", *args],
            input=stdin, capture_output=True, text=True, check=False,
            env=env or cls.env, cwd=kw.get("cwd", ROOT), timeout=120,
        )

    def test_help_lists_plugin_commands_without_running_them(self):
        r = self.dock("help")
        for cmd in ("code", "fetch"):
            self.assertIn(cmd, r.stdout)
        for line in r.stdout.splitlines():
            self.assertNotRegex(line, r"\bserve\b")

    def test_serve_was_removed(self):
        message = "dock: serve was removed; run commands with 'dock <command>' or 'dock shell'\n"
        for args in (("serve",), ("serve", "--x"), ("-v", "serve")):
            with self.subTest(args=args):
                r = self.dock(*args)
                self.assertEqual((r.returncode, r.stdout, r.stderr), (1, "", message))

    def starts(self, plugin_id):
        log = Path(self.home, "logs", f"{plugin_id}.log")
        return log.read_text().count(" start ") if log.exists() else 0

    def assert_usage_error(self, plugin_id, message, *args):
        """dock refuses the arguments itself: exit 2, its message, and no plugin process started."""
        before = self.starts(plugin_id)
        r = self.dock(*args)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertEqual(r.stderr.strip(), f"dock: {args[0]}: {message}")
        self.assertEqual(self.starts(plugin_id), before, "the plugin was started")

    def test_usage_errors_are_refused_before_anything_starts(self):
        # dock reads a leading '-' as a flag, so a negative number goes after `--`
        for name, message, args in (
                ("bad args", "input 'retries' must be an integer, got 'abc'", ("fetch", "u", "--retries", "abc")),
                ("two words unquoted", "unexpected argument 'Lovelace'", ("fetch", "Ada", "Lovelace")),
                ("negative number without --", "unknown flag '-3'", ("fetch", "u", "-3"))):
            with self.subTest(name):
                self.assert_usage_error("dev.dock.probe", message, *args)

    def test_unknown_command(self):
        r = self.dock("nope")
        self.assertEqual(r.returncode, 1)
        self.assertIn("unknown command", r.stderr)

    def run_with_config(self, text):
        home = tempfile.mkdtemp(prefix="dock-home-")
        self.addCleanup(__import__("shutil").rmtree, home, True)
        env = {**self.env, "DOCK_HOME": home}
        r = self.dock("plugin", "install", str(PROBE), env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        dockenv.write_config(home, dockenv.require_runtime())
        dockenv.add_config(home, text)
        return self.dock("code", "0", env=env), Path(home, "logs", "dev.dock.probe.log")

    def test_a_bad_config_fails_the_command_naming_the_problem_and_starts_nothing(self):
        cases = [(f"removed [{t}] table", body, f"[{t}] ") for t, body in (
            ("http", '[http]\nhosts = ["x"]\n'), ("providers", '[providers]\nllm = "x"\n'),
            ("isolation", "[isolation]\nenabled = true\n"), ("wasm", '[wasm]\nruntime = "x"\n'))]
        cases.append(("isolation table points to container runtime", "[isolation]\nenabled = true\n",
                      "[container] runtime"))
        cases += [(f"unparsable {text!r}", text, "config.toml")
                  for text in ('[providers]\nllm = "x"\n[shell\n', "[container\n", "[shell\n")]
        for name, body, expected in cases:
            with self.subTest(name):
                r, log = self.run_with_config(body)
                self.assertEqual(r.returncode, 1, r.stderr)
                self.assertIn(expected, r.stderr)
                self.assertFalse(log.exists() and " start " in log.read_text(), "the plugin was started")

    def test_a_container_runtime_that_is_not_installed_fails_the_command_with_the_fix(self):
        home = tempfile.mkdtemp(prefix="dock-home-")
        self.addCleanup(__import__("shutil").rmtree, home, True)
        env = {**self.env, "DOCK_HOME": home, "PATH": str(Path(sys.executable).parent)}  # python, no docker or podman
        self.assertEqual(self.dock("plugin", "install", str(PROBE), env=env).returncode, 0)
        r = self.dock("code", "0", env=env)
        self.assertEqual(r.returncode, 1, r.stderr)  # AC8
        self.assertEqual(r.stderr.strip(), "dock: no container runtime found (install docker or podman, "
                                           f"or set [container] runtime in {Path(home, 'config.toml')})")
        self.assertEqual(self.dock("help", env=env).returncode, 0)
        self.assertEqual(self.dock("plugin", "info", "dev.dock.probe", env=env).returncode, 0)


if __name__ == "__main__":
    unittest.main()
