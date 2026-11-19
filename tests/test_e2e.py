"""End-to-end: real host, real plugin processes, throwaway DOCK_HOME. Run `make test`."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

import dockenv  # noqa: E402


class DockE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        dockenv.prebuild_plugins(ROOT / "plugin" / "hello")
        cls.home = tempfile.mkdtemp(prefix="dock-home-")
        cls.env = {**os.environ, "DOCK_HOME": cls.home, "PYTHONPATH": str(ROOT)}
        for name in ("hello",):
            r = cls.dock("plugin", "install", str(ROOT / "plugin" / name))
            assert r.returncode == 0, r.stderr
        dockenv.write_config(cls.home, dockenv.require_runtime())  # a first install asks the runtime nothing, so setting the runtime here is only the last step of the setup

    @classmethod
    def dock(cls, *args, stdin="", env=None, **kw):
        return subprocess.run(
            [sys.executable, "-m", "cli", *args],
            input=stdin, capture_output=True, text=True, check=False,
            env=env or cls.env, cwd=kw.get("cwd", ROOT), timeout=120,
        )

    def test_help_lists_plugin_commands_without_running_them(self):
        r = self.dock("help")
        for cmd in ("hello", "count"):
            self.assertIn(cmd, r.stdout)

    def test_python_plugin_with_args(self):
        r = self.dock("hello", "Roman")
        self.assertEqual(r.stdout.strip(), "hello, Roman!")

    def test_python_plugin_prompts_through_host(self):
        r = self.dock("hello", stdin="Ada\n")
        self.assertIn("hello, Ada!", r.stdout)

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

    def test_bad_args_become_clean_error(self):
        self.assert_usage_error("dev.dock.hello", "input 'n' must be an integer, got 'abc'", "count", "abc")

    def test_hello_greets_a_quoted_multi_word_name(self):
        r = self.dock("hello", "Ada Lovelace")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "hello, Ada Lovelace!")

    def test_hello_with_two_words_unquoted_is_a_usage_error(self):
        self.assert_usage_error("dev.dock.hello", "unexpected argument 'Lovelace'", "hello", "Ada", "Lovelace")

    def test_hello_with_an_empty_name_greets_it_instead_of_prompting(self):
        r = self.dock("hello", "")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("your name", r.stdout)
        self.assertEqual(r.stdout.strip(), "hello, stranger!")

    def test_count_counts_to_the_given_number(self):
        r = self.dock("count", "3")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("counted to 3", r.stdout)

    def test_count_defaults_to_ten(self):
        r = self.dock("count")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("counted to 10", r.stdout)

    def test_count_negative_needs_double_dash(self):
        # dock reads a leading '-' as a flag, so a negative number goes after `--`
        self.assert_usage_error("dev.dock.hello", "unknown flag '-3'", "count", "-3")
        r = self.dock("count", "--", "-3")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("counted to -3", r.stdout)

    def test_a_stale_provisioned_marker_is_ignored(self):
        home = tempfile.mkdtemp(prefix="dock-home-")
        env = {**self.env, "DOCK_HOME": home}
        self.assertEqual(self.dock("plugin", "install", str(ROOT / "plugin" / "hello"), env=env).returncode, 0)
        dockenv.write_config(home, dockenv.require_runtime())
        marker = Path(home, "state", "dev.dock.hello.provisioned")
        marker.parent.mkdir(exist_ok=True)
        marker.write_text('{"version": "0.1.0"}')
        r = self.dock("hello", "Ada", env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("hello, Ada!", r.stdout)
        self.assertNotIn("provisioned", r.stderr)
        log = Path(home, "logs", "dev.dock.hello.log").read_text()
        self.assertEqual(log.count(" start "), 1, "only the command's own plugin was started")
        self.assertTrue(marker.exists(), "an old marker is left alone")

    def test_unknown_command(self):
        r = self.dock("nope")
        self.assertEqual(r.returncode, 1)
        self.assertIn("unknown command", r.stderr)

    def run_with_config(self, text):
        home = tempfile.mkdtemp(prefix="dock-home-")
        self.addCleanup(__import__("shutil").rmtree, home, True)
        env = {**self.env, "DOCK_HOME": home}
        r = self.dock("plugin", "install", str(ROOT / "plugin" / "hello"), env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        dockenv.write_config(home, dockenv.require_runtime())
        dockenv.add_config(home, text)
        return self.dock("hello", "Ada", env=env), Path(home, "logs", "dev.dock.hello.log")

    def test_a_removed_config_table_fails_a_command_naming_the_table(self):
        for table, body in (("http", '[http]\nhosts = ["x"]\n'), ("providers", '[providers]\nllm = "x"\n'),
                            ("isolation", "[isolation]\nenabled = true\n"), ("wasm", '[wasm]\nruntime = "x"\n')):
            with self.subTest(table=table):
                r, log = self.run_with_config(body)
                self.assertEqual(r.returncode, 1, r.stderr)
                self.assertIn(f"[{table}] ", r.stderr)
                self.assertNotIn("hello, Ada!", r.stdout)
                self.assertFalse(log.exists() and " start " in log.read_text(), "the plugin was started")

    def test_the_isolation_table_message_points_to_container_runtime(self):
        r, _ = self.run_with_config("[isolation]\nenabled = true\n")
        self.assertIn("[container] runtime", r.stderr)

    def test_a_config_that_does_not_parse_fails_the_command_and_starts_nothing(self):
        for text in ('[providers]\nllm = "x"\n[shell\n', "[container\n", "[shell\n"):
            with self.subTest(text=text):
                r, log = self.run_with_config(text)
                self.assertEqual(r.returncode, 1, r.stderr)
                self.assertIn("config.toml", r.stderr)
                self.assertNotIn("hello, Ada!", r.stdout)
                self.assertFalse(log.exists() and " start " in log.read_text(), "the plugin was started")

    def test_a_container_runtime_that_is_not_installed_fails_the_command_with_the_fix(self):
        home = tempfile.mkdtemp(prefix="dock-home-")
        self.addCleanup(__import__("shutil").rmtree, home, True)
        env = {**self.env, "DOCK_HOME": home, "PATH": str(Path(sys.executable).parent)}  # python, no docker or podman
        self.assertEqual(self.dock("plugin", "install", str(ROOT / "plugin" / "hello"), env=env).returncode, 0)
        r = self.dock("hello", "Ada", env=env)
        self.assertEqual(r.returncode, 1, r.stderr)  # AC8
        self.assertEqual(r.stderr.strip(), "dock: no container runtime found (install docker or podman, "
                                           f"or set [container] runtime in {Path(home, 'config.toml')})")
        self.assertEqual(self.dock("help", env=env).returncode, 0)
        self.assertEqual(self.dock("plugin", "info", "dev.dock.hello", env=env).returncode, 0)


if __name__ == "__main__":
    unittest.main()
