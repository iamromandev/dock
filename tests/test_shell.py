"""End-to-end tests for the interactive shell. Run `make test`."""

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cli import __version__  # noqa: E402
from cli.lifecycle import Session  # noqa: E402
from cli.manifest import Manifest  # noqa: E402
from cli.process import PluginError  # noqa: E402
from cli.store import Store  # noqa: E402
from cli.ui import TerminalUI  # noqa: E402

PROMPT = "dock> "


def wait_until(predicate, timeout=15.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class ShellProc:
    """A running `dock shell` (or other dock command) whose output can be waited on."""

    def __init__(self, env, *args):
        self.p = subprocess.Popen(
            [sys.executable, "-m", "cli", *(args or ("shell",))], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, cwd=ROOT,
        )
        self.out = ""
        self.err = ""
        self._threads = [
            threading.Thread(target=self._pump, args=(self.p.stdout, "out"), daemon=True),
            threading.Thread(target=self._pump, args=(self.p.stderr, "err"), daemon=True),
        ]
        for t in self._threads:
            t.start()

    def _pump(self, stream, name):
        for ch in iter(lambda: stream.read(1), ""):
            setattr(self, name, getattr(self, name) + ch)

    def send(self, line):
        self.p.stdin.write(line + "\n")
        self.p.stdin.flush()

    def wait_for(self, text, count=1, stream="out", timeout=15.0):
        ok = wait_until(lambda: getattr(self, stream).count(text) >= count, timeout)
        if not ok:
            self.p.kill()
        assert ok, f"timed out waiting for {text!r} x{count} on {stream}\nstdout: {self.out!r}\nstderr: {self.err!r}"

    def sigint(self):
        self.p.send_signal(signal.SIGINT)

    def finish(self, timeout=30.0):
        try:
            self.p.stdin.close()
        except OSError:
            pass
        try:
            self.p.wait(timeout=timeout)
        finally:
            self.p.kill()
        self._close()
        return self.p.returncode

    def _close(self):
        for t in self._threads:
            t.join(5)
        for stream in (self.p.stdin, self.p.stdout, self.p.stderr):
            stream.close()

    def kill(self):
        self.p.kill()
        self.p.wait()
        self._close()


def pid_alive(pid):
    """True while the process runs (a zombie nobody has reaped yet counts as gone)."""
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(state) and not state.startswith("Z")


class ShellE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.mkdtemp(prefix="dock-shell-home-")
        cls.env = {**os.environ, "DOCK_HOME": cls.home, "PYTHONPATH": str(ROOT)}
        for path in (ROOT / "plugin" / "hello", ROOT / "plugin" / "llm", ROOT / "plugin" / "chat",
                     ROOT / "tests" / "fixtures" / "probe", ROOT / "tests" / "fixtures" / "provprobe",
                     ROOT / "tests" / "fixtures" / "capcaller", ROOT / "tests" / "fixtures" / "capprovider"):
            r = cls.dock("plugin", "install", str(path))
            assert r.returncode == 0, r.stderr

    @classmethod
    def dock(cls, *args, stdin="", env=None):
        return subprocess.run(
            [sys.executable, "-m", "cli", *args],
            input=stdin, capture_output=True, text=True, check=False,
            env=env or cls.env, cwd=ROOT, timeout=60,
        )

    def shell(self, stdin, env=None):
        return self.dock("shell", stdin=stdin, env=env)

    def starts(self, plugin_id):
        log = Path(self.home, "logs", f"{plugin_id}.log")
        return log.read_text().count(" start ") if log.exists() else 0

    def setUp(self):
        for f in Path(self.home, "logs").glob("*.log"):
            f.unlink()
        self.events_path.unlink(missing_ok=True)
        self.provider_events_path.unlink(missing_ok=True)

    events_path = property(lambda self: Path(self.home, "data", "dev.dock.provprobe", "events"))
    provider_events_path = property(lambda self: Path(self.home, "data", "dev.dock.capprovider", "events"))

    def events(self, path=None):
        """Lines of a fixture's event log (provprobe by default), as (pid, event)."""
        path = path or self.events_path
        if not path.exists():
            return []
        return [(int(pid), name) for pid, name in (l.split() for l in path.read_text().splitlines())]

    def provider_events(self):
        return [name for _, name in self.events(self.provider_events_path)]

    def markers(self, home=None):
        return list(Path(home or self.home, "state").glob("*.provisioned"))

    def shell_proc(self, env=None, *args):
        sp = ShellProc(env or self.env, *args)
        self.addCleanup(sp.kill)
        return sp

    def private_home(self, *plugins, config=None):
        """A fresh DOCK_HOME with the named bundled plugins installed; returns (home, env)."""
        home = tempfile.mkdtemp(prefix="dock-shell-priv-")
        env = {**self.env, "DOCK_HOME": home}
        for name in plugins:
            r = self.dock("plugin", "install", str(ROOT / "plugin" / name), env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
        if config:
            Path(home, "config.toml").write_text(config)
        return home, env

    # AC1
    def test_bare_dock_without_tty_prints_help(self):
        r = self.dock(stdin="")
        self.assertEqual(r.returncode, 0)
        self.assertIn("usage: dock", r.stdout)
        self.assertFalse(r.stdout.startswith("dock> "))

    def test_shell_prints_prompt_with_piped_input(self):
        r = self.shell("hello Bob\n")
        self.assertIn("dock> ", r.stdout)
        self.assertIn("hello, Bob!", r.stdout)

    # AC2
    def test_quoted_arg_and_unknown_command(self):
        r = self.shell('hello "Ada Lovelace"\nnosuch\n')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("hello, Ada Lovelace!", r.stdout)
        self.assertIn("unknown command 'nosuch'", r.stderr)

    def test_unbalanced_quote_returns_to_prompt(self):
        r = self.shell('hello "Ada\nhello Bob\n')
        self.assertIn("dock:", r.stderr)
        self.assertIn("hello, Bob!", r.stdout)

    def test_blank_line_and_exit(self):
        r = self.shell("\n  \nexit\nhello Late\n")
        self.assertEqual(r.returncode, 0)
        self.assertNotIn("Late", r.stdout)

    # AC4
    def test_nonzero_exit_code_is_printed_and_shell_continues(self):
        r = self.shell("code 3\ncode 0\nhello Bob\n")
        self.assertEqual(r.stderr.count("exit 3"), 1)
        self.assertNotIn("exit 0", r.stderr)
        self.assertIn("hello, Bob!", r.stdout)

    # AC3 / AC10
    def test_chat_twice_provisions_llm_once(self):
        r = self.shell("chat\n/exit\nchat\n/exit\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.starts("dev.dock.llm"), 1)
        self.assertEqual(self.starts("dev.dock.chat"), 1)

    def test_chat_second_invoke_has_no_history(self):
        r = self.shell("chat\nfirst\n/exit\nchat\nsecond\n/exit\n")
        self.assertEqual(r.stdout.count("message 1 of our conversation"), 2)  # each chat starts at turn 1
        self.assertNotIn("message 2 of our conversation", r.stdout)

    def test_hello_twice_one_process(self):
        r = self.shell("hello a\nhello b\n")
        self.assertIn("hello, a!", r.stdout)
        self.assertIn("hello, b!", r.stdout)
        self.assertEqual(self.starts("dev.dock.hello"), 1)

    # AC5
    def test_crash_reports_error_and_next_use_restarts(self):
        r = self.shell("crash\ncode 2\n")
        self.assertIn("dev.dock.probe", r.stderr)
        self.assertIn("exit 2", r.stderr)
        self.assertEqual(self.starts("dev.dock.probe"), 2)

    # AC6
    def test_sigint_cancels_operation_and_keeps_plugins(self):
        sh = self.shell_proc()
        sh.send("sleep 30")
        sh.wait_for("sleeping")  # inside the invoke
        sh.sigint()
        sh.wait_for(PROMPT, count=2)
        sh.send("hello Bob")
        sh.wait_for("hello, Bob!")
        sh.wait_for(PROMPT, count=3)
        self.assertEqual(sh.finish(), 0, sh.err)
        self.assertEqual(self.starts("dev.dock.probe"), 1)

    # R7: a cancel that reaches a plugin with nothing in flight changes nothing
    def test_sigint_leaves_idle_warm_plugin_working(self):
        sh = self.shell_proc()
        sh.send("hello a")
        sh.wait_for("hello, a!")
        sh.send("sleep 30")  # SIGINT cancels this; hello gets the cancel while idle
        sh.wait_for("sleeping")
        sh.sigint()
        sh.wait_for(PROMPT, count=3)
        sh.send("hello Bob")
        sh.wait_for("hello, Bob!")
        self.assertEqual(sh.finish(), 0, sh.err)
        self.assertNotIn("dev.dock.hello:", sh.err)
        self.assertEqual(self.starts("dev.dock.hello"), 1)

    # AC7
    def test_eof_stops_everything_and_unprovisions(self):
        r = self.shell("chat\n/exit\npid\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(list(Path(self.home, "state").glob("*.provisioned")), [])
        pid = int(Path(self.home, "data", "dev.dock.probe", "pid").read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        self.assertFalse(Path(self.home, "data", "dev.dock.llm", "model.lock").exists())

    # AC8
    def test_plugin_remove_stops_running_plugin_first(self):
        # use a private home so the shared one keeps hello
        home = tempfile.mkdtemp(prefix="dock-shell-rm-")
        env = {**self.env, "DOCK_HOME": home}
        self.dock("plugin", "install", str(ROOT / "plugin" / "hello"), env=env)
        r = self.dock("shell", stdin="hello a\nplugin remove dev.dock.hello\nhello b\n", env=env)
        self.assertIn("removed dev.dock.hello", r.stdout)
        self.assertIn("unknown command 'hello'", r.stderr)
        self.assertNotIn("hello, b!", r.stdout)
        log = Path(home, "logs", "dev.dock.hello.log")
        self.assertEqual(log.read_text().count(" start "), 1)

    # AC11
    def _idle_run(self, timeout):
        home = tempfile.mkdtemp(prefix="dock-shell-idle-")
        env = {**self.env, "DOCK_HOME": home}
        self.dock("plugin", "install", str(ROOT / "plugin" / "hello"), env=env)
        Path(home, "config.toml").write_text(f"[shell]\nidle_timeout = {timeout}\n")
        p = subprocess.Popen([sys.executable, "-m", "cli", "shell"], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             env=env, cwd=ROOT)
        p.stdin.write("hello Bob\n")
        p.stdin.flush()
        time.sleep(2.5)
        out, _ = p.communicate("\nstatus\n", timeout=30)
        return out

    def test_idle_plugin_is_stopped_after_timeout(self):
        self.assertIn("no plugins running", self._idle_run(1))

    def test_idle_timeout_zero_keeps_plugin(self):
        out = self._idle_run(0)
        self.assertNotIn("no plugins running", out)
        self.assertIn("dev.dock.hello", out)

    # AC12
    def test_status_lists_running_plugins_with_provides(self):
        r = self.shell("status\nchat\n/exit\nstatus\n")
        self.assertIn("no plugins running", r.stdout)
        tail = r.stdout.rsplit("no plugins running", 1)[1]
        self.assertRegex(tail, r"dev\.dock\.chat\s+running\s+idle 0m0\ds")
        self.assertRegex(tail, r"dev\.dock\.llm\s+running\s+idle 0m\d\ds\s+provides: llm")

    # AC13
    def _shell_obj(self):
        sys.path.insert(0, str(ROOT))
        from cli.shell import Shell
        from cli.store import Store
        return Shell(Store(Path(self.home)), None)

    def test_candidates_first_word(self):
        c = self._shell_obj().candidates("he", "he")
        self.assertEqual(c, ["hello", "help"])

    def test_candidates_plugin_subcommands_and_ids(self):
        sh = self._shell_obj()
        self.assertEqual(sh.candidates("plugin ", ""), ["list", "info", "install", "build", "remove"])
        self.assertEqual(sh.candidates("plugin info dev.dock.", "dev.dock."),
                         ["dev.dock.capcaller", "dev.dock.capprovider", "dev.dock.chat", "dev.dock.hello",
                          "dev.dock.llm", "dev.dock.probe", "dev.dock.provprobe"])
        self.assertEqual(sh.candidates("plugin build dev.dock.h", "dev.dock.h"), ["dev.dock.hello"])

    def test_complete_uses_line_buffer_and_state(self):
        import types
        sh = self._shell_obj()
        sh.readline = types.SimpleNamespace(get_line_buffer=lambda: "he")
        self.assertEqual(sh.complete("he", 0), "hello")
        self.assertEqual(sh.complete("he", 1), "help")
        self.assertIsNone(sh.complete("he", 2))

    # AC14
    @staticmethod
    def history(home):
        # libedit writes a header line and escapes spaces as \\040
        lines = Path(home, "history").read_text().splitlines()
        return [l.replace("\\040", " ") for l in lines if l != "_HiStOrY_V2_"]

    def test_history_file_records_entered_lines(self):
        home = tempfile.mkdtemp(prefix="dock-shell-hist-")
        env = {**self.env, "DOCK_HOME": home}
        self.dock("plugin", "install", str(ROOT / "plugin" / "hello"), env=env)
        r = self.dock("shell", stdin="hello a\n\nnosuch\n", env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.history(home), ["hello a", "nosuch"])

    def test_history_is_capped_at_1000(self):
        home = tempfile.mkdtemp(prefix="dock-shell-hist-")
        env = {**self.env, "DOCK_HOME": home}
        self.dock("plugin", "install", str(ROOT / "plugin" / "hello"), env=env)
        r = self.dock("shell", stdin="help\n" * 1100, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.history(home)), 1000)

    def test_shell_works_without_readline(self):
        home = tempfile.mkdtemp(prefix="dock-shell-nort-")
        env = {**self.env, "DOCK_HOME": home, "DOCK_NO_READLINE": "1"}
        self.dock("plugin", "install", str(ROOT / "plugin" / "hello"), env=env)
        r = self.dock("shell", stdin='hello "Ada Lovelace"\nnosuch\n', env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("hello, Ada Lovelace!", r.stdout)
        self.assertIn("unknown command 'nosuch'", r.stderr)
        self.assertFalse(Path(home, "history").exists())


    # ---- startup failures and interrupts ---------------------------------------
    def test_ctrl_c_during_slow_provision_returns_to_prompt_and_unprovisions(self):
        sh = self.shell_proc({**self.env, "PROVPROBE_PROVISION": "slow"})
        sh.send("ping")
        self.assertTrue(wait_until(lambda: ("provision-begin" in [e for _, e in self.events()])))
        sh.sigint()
        sh.wait_for(PROMPT, count=2)
        sh.send("code 3")  # the shell is alive and the session is usable
        sh.wait_for("exit 3", stream="err")
        sh.send("status")
        sh.wait_for("dev.dock.probe")
        self.assertNotIn("dev.dock.provprobe", sh.out)
        self.assertEqual(sh.finish(), 0, sh.err)
        names = [e for _, e in self.events()]
        self.assertEqual(names.count("unprovision"), 1, names)
        self.assertEqual(self.markers(), [])
        self.assertFalse(any(pid_alive(pid) for pid, _ in self.events()))

    def test_one_shot_ctrl_c_during_slow_provision_exits_130_and_unprovisions(self):
        sh = self.shell_proc({**self.env, "PROVPROBE_PROVISION": "slow"}, "ping")
        self.assertTrue(wait_until(lambda: ("provision-begin" in [e for _, e in self.events()])))
        sh.sigint()
        self.assertEqual(sh.finish(), 130, sh.err)
        names = [e for _, e in self.events()]
        self.assertEqual(names.count("unprovision"), 1, names)
        self.assertEqual(self.markers(), [])
        self.assertFalse(any(pid_alive(pid) for pid, _ in self.events()))

    def test_failed_provision_twice_leaves_no_process_and_unprovisions_each(self):
        sh = self.shell_proc({**self.env, "PROVPROBE_PROVISION": "fail"})
        sh.send("ping")
        sh.wait_for(PROMPT, count=2)
        sh.send("status")
        sh.wait_for("no plugins running")  # nothing half-started is left registered
        sh.send("ping")
        sh.wait_for(PROMPT, count=4)
        sh.send("status")
        sh.wait_for("no plugins running", count=2)
        self.assertEqual(sh.finish(), 0, sh.err)
        self.assertEqual(sh.err.count("provision failed on purpose"), 2, sh.err)
        names = [e for _, e in self.events()]
        self.assertEqual(names.count("provision-failed"), 2, names)
        self.assertEqual(names.count("unprovision"), 2, names)
        self.assertEqual(self.markers(), [])
        self.assertFalse(any(pid_alive(pid) for pid, _ in self.events()))

    def test_failed_llm_provision_twice_leaves_nothing_running(self):
        sh = self.shell_proc({**self.env, "DOCK_LLM_BASE_URL": "http://127.0.0.1:9/v1"})
        sh.send("chat")
        sh.wait_for(PROMPT, count=2)
        sh.send("chat")
        sh.wait_for(PROMPT, count=3)
        sh.send("status")
        sh.wait_for("no plugins running")
        self.assertEqual(sh.finish(), 0, sh.err)
        self.assertEqual(sh.err.count("DOCK_LLM_MODEL"), 2, sh.err)
        self.assertEqual(self.markers(), [])

    # ---- cancel that is ignored -------------------------------------------------
    def _assert_stubborn_plugin_replaced(self, sh):
        pid = int(Path(self.home, "data", "dev.dock.probe", "pid").read_text())
        sh.send("code 2")  # would hang behind the stuck plugin if it were kept
        sh.wait_for("exit 2", stream="err", timeout=20)
        self.assertEqual(sh.finish(), 0, sh.err)
        self.assertNotIn("stubborn late", sh.out)
        self.assertEqual(self.starts("dev.dock.probe"), 2)
        self.assertFalse(pid_alive(pid))

    def test_second_ctrl_c_stops_a_plugin_that_ignores_cancel(self):
        sh = self.shell_proc()
        sh.send("stubborn 30")
        sh.wait_for("stubborn started")
        sh.sigint()
        sh.wait_for("cancelling", stream="err")
        sh.sigint()
        sh.wait_for(PROMPT, count=2)
        self._assert_stubborn_plugin_replaced(sh)

    def test_plugin_that_ignores_cancel_is_stopped_when_grace_expires(self):
        sh = self.shell_proc({**self.env, "DOCK_CANCEL_GRACE": "1"})
        sh.send("stubborn 30")
        sh.wait_for("stubborn started")
        sh.sigint()
        sh.wait_for(PROMPT, count=2, timeout=10)
        self._assert_stubborn_plugin_replaced(sh)

    # ---- crashed provider, exit paths, built-ins ----------------------------------
    def test_crashed_provider_is_restarted_by_next_use(self):
        sh = self.shell_proc()
        sh.send("chat")
        sh.send("first")
        sh.send("/exit")
        sh.wait_for(PROMPT, count=2)
        pid = json.loads(Path(self.home, "data", "dev.dock.llm", "model.lock").read_text())["pid"]
        os.kill(pid, signal.SIGKILL)
        self.assertTrue(wait_until(lambda: not pid_alive(pid)))
        sh.send("chat")
        sh.send("second")
        sh.send("/exit")
        sh.wait_for("message 1 of our conversation", count=2)
        self.assertEqual(sh.finish(), 0, sh.err)
        self.assertEqual(self.starts("dev.dock.llm"), 2)
        self.assertEqual(self.markers(), [])

    def test_eof_and_exit_clean_up_the_same_way(self):
        for ending in ("", "exit\n"):
            with self.subTest(ending=ending):
                self.events_path.unlink(missing_ok=True)
                r = self.shell("ping\n" + ending)
                self.assertEqual(r.returncode, 0, r.stderr)
                names = [e for _, e in self.events()]
                self.assertEqual(names.count("unprovision"), 1, names)
                self.assertEqual(self.markers(), [])
                self.assertFalse(any(pid_alive(pid) for pid, _ in self.events()))

    def test_builtins_work_inside_the_shell(self):
        r = self.shell("help\nversion\nplugin list\nplugin info dev.dock.hello\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("plugin install", r.stdout)
        self.assertIn(f"dock {__version__}", r.stdout)
        self.assertRegex(r.stdout, r"dev\.dock\.hello\s+0\.1\.0")
        self.assertIn("id:          dev.dock.hello", r.stdout)

    def test_shell_help_lists_shell_builtins_and_not_shell(self):
        r = self.shell("help\n")
        for word in ("status", "exit, quit"):
            self.assertRegex(r.stdout, rf"(?m)^  {word}\b", word)
        self.assertNotRegex(r.stdout, r"(?m)^  shell\b")
        self.assertNotIn("usage: dock <command>", r.stdout)

    def test_plugin_install_of_running_id_stops_it_first(self):
        home, env = self.private_home("hello")
        r = self.dock("shell", stdin=f"hello a\nplugin install {ROOT / 'plugin' / 'hello'}\nhello b\n", env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("installed dev.dock.hello", r.stdout)
        self.assertIn("hello, b!", r.stdout)
        log = Path(home, "logs", "dev.dock.hello.log")
        self.assertEqual(log.read_text().count(" start "), 2)

    def test_missing_provider_error_keeps_shell_alive(self):
        home, env = self.private_home("hello", "chat")
        r = self.dock("shell", stdin="chat\nhello Bob\n", env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("no installed plugin provides 'llm'", r.stderr)
        self.assertIn("hello, Bob!", r.stdout)

    def test_unwritable_history_warns_once_and_exits_cleanly(self):
        home, env = self.private_home("hello")
        Path(home, "history").mkdir()  # a directory cannot be written as a file
        r = self.dock("shell", stdin="hello a\n", env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr.count("cannot write history"), 1, r.stderr)
        self.assertIn("hello, a!", r.stdout)

    def test_history_file_is_private(self):
        home, env = self.private_home("hello")
        self.dock("shell", stdin="hello a\n", env=env)
        self.assertEqual(Path(home, "history").stat().st_mode & 0o777, 0o600)

    # ---- idle reaping ---------------------------------------------------------------
    def test_idle_plugin_is_stopped_before_the_line_that_follows_the_wait(self):
        home, env = self.private_home("hello", config="[shell]\nidle_timeout = 1\n")
        sh = self.shell_proc(env)
        sh.send("hello Bob")
        sh.wait_for("hello, Bob!")
        sh.wait_for(PROMPT, count=2)
        time.sleep(1.5)  # idle at the prompt longer than idle_timeout
        sh.send("status")  # no blank line in between: the line itself triggers the reap
        sh.wait_for("no plugins running")
        self.assertEqual(sh.finish(), 0, sh.err)

    def test_invalid_idle_timeout_warns_once_per_shell(self):
        home, env = self.private_home("hello", config='[shell]\nidle_timeout = "soon"\n')
        r = self.dock("shell", stdin="hello a\nhello b\nhelp\n", env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr.count("invalid [shell] idle_timeout"), 1, r.stderr)
        self.assertIn("hello, b!", r.stdout)

    def test_broken_config_toml_is_reported_once_and_shell_survives(self):
        home, env = self.private_home("hello", config="[shell\n")
        r = self.dock("shell", stdin="hello a\nhello b\nstatus\n", env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr.count("config.toml"), 1, r.stderr)
        self.assertIn("hello, a!", r.stdout)
        self.assertIn("hello, b!", r.stdout)
        self.assertEqual(Path(home, "logs", "dev.dock.hello.log").read_text().count(" start "), 1)

    # ---- Ctrl-C while a command waits on a provider ------------------------------------
    def test_ctrl_c_mid_call_lets_the_provider_finish_cancelling(self):
        sh = self.shell_proc()
        sh.send("callcap")
        sh.wait_for("tick")  # the provider is mid-call and streaming
        sh.sigint()
        sh.wait_for(PROMPT, count=2)
        sh.send("callcap")  # reuses the provider: no second initialize or provision
        self.assertTrue(wait_until(lambda: self.provider_events().count("call-begin") == 2))
        sh.sigint()
        sh.wait_for(PROMPT, count=3)
        self.assertEqual(sh.finish(), 0, sh.err)
        self.assertNotIn("did not stop", sh.err)
        names = self.provider_events()
        self.assertEqual(names.count("initialize"), 1, names)
        self.assertEqual(names.count("provision"), 1, names)
        self.assertEqual(names.count("call-cancelled"), 2, names)
        self.assertEqual(names.count("unprovision"), 1, names)
        self.assertEqual(self.starts("dev.dock.capprovider"), 1)
        self.assertEqual(self.markers(), [])

    def test_one_shot_ctrl_c_mid_call_exits_130_and_unprovisions_the_provider(self):
        sh = self.shell_proc(self.env, "callcap")
        sh.wait_for("tick")
        sh.sigint()
        self.assertEqual(sh.finish(), 130, sh.err)
        self.assertNotIn("did not stop", sh.err)
        names = self.provider_events()
        self.assertEqual(names.count("call-cancelled"), 1, names)
        self.assertEqual(names.count("unprovision"), 1, names)
        self.assertEqual(self.markers(), [])

    # ---- a force-killed provisioned plugin ----------------------------------------------
    def test_force_killed_plugin_keeps_marker_until_the_next_process_unprovisions(self):
        marker = Path(self.home, "state", "dev.dock.provprobe.provisioned")
        sh = self.shell_proc()
        sh.send("ping stuck")
        sh.wait_for("stuck")
        sh.sigint()
        sh.wait_for("cancelling", stream="err")
        sh.sigint()  # stop waiting: the plugin ignores cancel
        sh.wait_for("did not stop", stream="err")
        sh.wait_for(PROMPT, count=2)
        self.assertTrue(marker.exists(), "marker must survive a kill: unprovision was skipped")
        first_marker = marker.read_text()
        first_pid = self.events()[0][0]
        self.assertNotIn((first_pid, "unprovision"), self.events())

        sh.send("ping")  # a new process provisions again and rewrites the marker
        sh.wait_for("pong")
        self.assertTrue(marker.exists())
        self.assertNotEqual(marker.read_text(), first_marker)
        self.assertEqual(sh.finish(), 0, sh.err)

        events = self.events()
        second_pid = next(pid for pid, _ in events if pid != first_pid)
        self.assertIn((second_pid, "provision-begin"), events)
        self.assertIn((second_pid, "unprovision"), events)
        self.assertNotIn((first_pid, "unprovision"), events)
        self.assertFalse(marker.exists())


class SessionUnit(unittest.TestCase):
    """Session behaviour that needs no subprocess to observe."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="dock-session-"))
        self.store = Store(self.home)
        for name in ("hello", "llm"):
            self.store.install(ROOT / "plugin" / name)
        self.session = Session(self.store)
        self.addCleanup(self.session.close)

    def test_call_cleans_up_event_handler_and_survives_a_gone_caller(self):
        def gone(*_a, **_k):
            raise PluginError("caller is gone")

        caller = mock.Mock()
        caller.manifest.id = "dev.dock.caller"
        caller.manifest.requires = ["llm"]
        caller.notify.side_effect = gone
        result = self.session._call(caller, {
            "capability": "llm", "method": "generate",
            "params": {"messages": [{"role": "user", "content": "hello"}]},
        })
        self.assertIn("toy model", result["text"])
        self.assertGreater(caller.notify.call_count, 0)
        self.assertNotIn("event", self.session.providers["llm"].handlers)

    def test_stop_proc_drops_replies_still_queued_for_it(self):
        manifest = self.store.get("dev.dock.hello")
        proc = self.session.start(manifest, TerminalUI(manifest.id))
        other = self.session.start(manifest, TerminalUI(manifest.id))
        self.session.hub.replies[(proc, 99)] = {"id": 99}
        self.session.hub.replies[(other, 99)] = {"id": 99}
        self.session.stop_proc(proc)
        self.assertEqual(list(self.session.hub.replies), [(other, 99)])

    def test_provider_for_refreshes_last_used_when_reusing_a_warm_provider(self):
        provider = self.session.provider_for("llm")
        self.session.last_used[provider] = time.monotonic() - 1000
        self.assertIs(self.session.provider_for("llm"), provider)
        self.assertLess(time.monotonic() - self.session.last_used[provider], 5)

    def test_messages_from_a_forgotten_process_are_ignored(self):
        manifest = self.store.get("dev.dock.hello")
        proc = self.session.start(manifest, TerminalUI(manifest.id))
        self.session.stop_proc(proc)
        self.session.hub.inbox.put((proc, "msg", {"id": 99, "result": {}}))
        self.session.hub.pump(5)
        self.assertEqual(self.session.hub.replies, {})

    def test_drain_pending_returns_once_nothing_is_pending(self):
        self.assertTrue(self.session.drain_pending(time.monotonic() + 5))
        manifest = self.store.get("dev.dock.hello")
        proc = self.session.start(manifest, TerminalUI(manifest.id))
        proc.pending.add(99)  # a reply that never comes
        started = time.monotonic()
        self.assertFalse(self.session.drain_pending(started + 0.3))
        self.assertGreaterEqual(time.monotonic() - started, 0.3)
        proc.pending.clear()
        self.assertTrue(self.session.drain_pending(time.monotonic() + 5))

    def test_drain_pending_collects_a_reply_that_arrives_before_the_deadline(self):
        manifest = self.store.get("dev.dock.hello")
        proc = self.session.start(manifest, TerminalUI(manifest.id))
        proc.pending.add(99)
        threading.Timer(0.2, lambda: self.session.hub.inbox.put((proc, "msg", {"id": 99, "result": {}}))).start()
        self.assertTrue(self.session.drain_pending(time.monotonic() + 5))
        self.assertEqual(proc.pending, set())

    def test_stopping_processes_leaves_no_unclosed_pipes(self):
        import gc
        import warnings

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            for plugin_id in ("dev.dock.hello", "dev.dock.hello"):
                manifest = self.store.get(plugin_id)
                proc = self.session.start(manifest, TerminalUI(manifest.id))
                self.session.stop_proc(proc)  # graceful
            killed = self.session.start(manifest, TerminalUI(manifest.id))
            self.session.stop_proc(killed, force=True)
            del proc, killed
            gc.collect()
        leaks = [str(w.message) for w in caught if issubclass(w.category, ResourceWarning)]
        self.assertEqual(leaks, [])

    def test_stuck_plugins_are_stopped_even_if_a_second_ctrl_c_hits_the_cancel_step(self):
        for step in ("cancel_all", "drain_pending"):
            with self.subTest(step=step), \
                    mock.patch.object(Session, step, side_effect=KeyboardInterrupt), \
                    mock.patch.object(Session, "_stop_unresponsive") as stop:
                self.session._interrupted(TerminalUI("dev.dock.test"))
                stop.assert_called_once()

    def test_close_keeps_stopping_after_one_stop_fails(self):
        for failure in (RuntimeError("boom"), KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__):
                session = Session(self.store)
                session.procs = ["a", "b", "c"]
                stopped = []

                def stop(proc, _stopped=stopped, _session=session, _failure=failure):
                    _stopped.append(proc)
                    _session.procs.remove(proc)
                    if proc == "b":
                        raise _failure

                with mock.patch.object(Session, "stop_proc", side_effect=stop):
                    with self.assertRaises(type(failure)):
                        session.close()
                self.assertEqual(stopped, ["c", "b", "a"])


class ShellUnit(unittest.TestCase):
    """Shell loop behaviour driven by a fake session and scripted input."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="dock-shell-unit-"))
        self.store = Store(self.home)

    def run_loop(self, reap_results, lines):
        from cli.shell import Shell

        session = mock.Mock()
        session.reap_idle.side_effect = list(reap_results) + [[]] * 10
        feed = iter(lines)

        def input_fn(_prompt):
            try:
                return next(feed)
            except StopIteration:
                raise EOFError from None

        shell = Shell(self.store, session, input_fn=input_fn)
        with mock.patch("builtins.print"):
            code = shell.loop()
        return code, session

    def test_ctrl_c_while_reaping_before_the_prompt_keeps_the_shell_running(self):
        code, session = self.run_loop([KeyboardInterrupt()], ["version"])
        self.assertEqual(code, 0)
        session.close.assert_called_once()

    def test_ctrl_c_while_reaping_after_a_line_keeps_the_shell_running(self):
        # the first reap (before the prompt) succeeds, the second (after the line) is interrupted
        code, session = self.run_loop([[], KeyboardInterrupt()], ["version", "version"])
        self.assertEqual(code, 0)
        session.close.assert_called_once()
        self.assertGreaterEqual(session.reap_idle.call_count, 4)


if __name__ == "__main__":
    unittest.main()
