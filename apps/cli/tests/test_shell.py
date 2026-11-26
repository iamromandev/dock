"""End-to-end tests for the interactive shell. Run `make test`."""

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
sys.path.insert(0, str(ROOT / "tests"))

import dockenv
from src import __version__
from src.lifecycle import Session
from src.store import Store
from src.ui import PlainUI

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
            [sys.executable, "-m", "src", *(args or ("shell",))], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, cwd=ROOT, **dockenv.SIGINT_DEFAULT,
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


class ShellE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plugins = (ROOT.parent.parent / "plugins" / "hello", ROOT / "tests" / "fixtures" / "probe")
        dockenv.prebuild_plugins(*cls.plugins)
        cls.home = tempfile.mkdtemp(prefix="dock-shell-home-")
        cls.env = {**os.environ, "DOCK_HOME": cls.home, "PYTHONPATH": str(ROOT)}
        for path in cls.plugins:
            r = cls.dock("plugin", "install", str(path))
            assert r.returncode == 0, r.stderr
        dockenv.write_config(cls.home, dockenv.require_runtime())  # a first install asks the runtime nothing, so setting the runtime here is only the last step of the setup

    @staticmethod
    def cid(home):
        """The container id the probe wrote when it last ran its `pid` or `stubborn` command."""
        return Path(home, "data", "dev.dock.probe", "cid").read_text()

    @classmethod
    def dock(cls, *args, stdin="", env=None):
        return subprocess.run(
            [sys.executable, "-m", "src", *args],
            input=stdin, capture_output=True, text=True, check=False,
            env=env or cls.env, cwd=ROOT, timeout=120, **dockenv.SIGINT_DEFAULT,
        )

    def shell(self, stdin, env=None):
        return self.dock("shell", stdin=stdin, env=env)

    def starts(self, plugin_id):
        log = Path(self.home, "logs", f"{plugin_id}.log")
        return log.read_text().count(" start ") if log.exists() else 0

    def setUp(self):
        for f in Path(self.home, "logs").glob("*.log"):
            f.unlink()

    def shell_proc(self, env=None, *args):
        sp = ShellProc(env or self.env, *args)
        self.addCleanup(sp.kill)
        return sp

    def private_home(self, *plugins, config=None):
        """A fresh DOCK_HOME with the named bundled plugins installed; returns (home, env)."""
        home = tempfile.mkdtemp(prefix="dock-shell-priv-")
        env = {**self.env, "DOCK_HOME": home}
        for name in plugins:
            r = self.dock("plugin", "install", str(ROOT.parent.parent / "plugins" / name), env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
        dockenv.write_config(home, dockenv.require_runtime())  # a first install asks the runtime nothing, so setting the runtime here is only the last step of the setup
        if config:
            dockenv.add_config(home, config)
        return home, env

    # AC1
    def test_bare_dock_without_tty_prints_help(self):
        r = self.dock(stdin="")
        self.assertEqual(r.returncode, 0)
        self.assertIn("usage: dock", r.stdout)
        self.assertFalse(r.stdout.startswith("dock> "))

    # AC2
    def test_quoted_arg_and_unknown_command(self):
        script = 'hello "Ada Lovelace"\nnosuch\n'
        with self.subTest("readline on"):
            r = self.shell(script)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("hello, Ada Lovelace!", r.stdout)
            self.assertIn("unknown command 'nosuch'", r.stderr)
        with self.subTest("readline off"):
            home = tempfile.mkdtemp(prefix="dock-shell-nort-")
            env = {**self.env, "DOCK_HOME": home, "DOCK_NO_READLINE": "1"}
            self.dock("plugin", "install", str(ROOT.parent.parent / "plugins" / "hello"), env=env)
            dockenv.write_config(home, dockenv.require_runtime())  # a first install asks the runtime nothing, so setting the runtime here is only the last step of the setup
            r = self.dock("shell", stdin=script, env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("hello, Ada Lovelace!", r.stdout)
            self.assertIn("unknown command 'nosuch'", r.stderr)
            self.assertFalse(Path(home, "history").exists())

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

    def test_plugin_update_stops_a_running_plugin_first_but_not_on_dry_run(self):
        tmp = Path(tempfile.mkdtemp(prefix="dock-shell-upd-"))
        copy = dockenv.plugin_copy(ROOT.parent.parent / "plugins" / "hello", tmp / "v1", "dev.dock.helloupd")
        self.addCleanup(dockenv.remove_image, "dock/dev.dock.helloupd:0.2.0")
        self.addCleanup(dockenv.remove_image, "dock/dev.dock.helloupd:0.3.0")
        home = tempfile.mkdtemp(prefix="dock-shell-upd-home-")
        env = {**self.env, "DOCK_HOME": home}
        self.assertEqual(self.dock("plugin", "install", str(copy), env=env).returncode, 0)
        newer = dockenv.plugin_copy(ROOT.parent.parent / "plugins" / "hello", tmp / "cat", "dev.dock.helloupd")
        toml = newer / "plugin.toml"
        toml.write_text(toml.read_text().replace('version = "0.2.0"', 'version = "0.3.0"', 1))
        dockenv.write_config(home, dockenv.require_runtime())
        dockenv.add_config(Path(home), f'[catalog]\npaths = ["{newer}"]\n')
        r = self.dock("shell", stdin="hello a\nplugin update --dry-run\nhello b\nplugin update\nhello c\n", env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("would update dev.dock.helloupd 0.2.0 → 0.3.0", r.stdout)
        self.assertIn("updated dev.dock.helloupd 0.2.0 → 0.3.0", r.stdout)
        self.assertIn("hello, c!", r.stdout)
        log = Path(home, "logs", "dev.dock.helloupd.log").read_text()
        self.assertEqual(log.count(" start "), 2)  # a and b shared one process; the update stopped it; c started anew

    # AC8
    def test_plugin_remove_or_install_of_a_running_id_stops_it_first(self):
        # a copy of hello under its own id: a remove or reinstall drops only that copy's image, never the shared hello one
        with self.subTest("remove"):
            tmp = Path(tempfile.mkdtemp(prefix="dock-shell-copy-"))
            copy = dockenv.plugin_copy(ROOT.parent.parent / "plugins" / "hello", tmp, "dev.dock.hellorm")
            self.addCleanup(dockenv.remove_image, "dock/dev.dock.hellorm:0.1.0")
            home = tempfile.mkdtemp(prefix="dock-shell-rm-")
            env = {**self.env, "DOCK_HOME": home}
            self.dock("plugin", "install", str(copy), env=env)
            dockenv.write_config(home, dockenv.require_runtime())
            r = self.dock("shell", stdin="hello a\nplugin remove dev.dock.hellorm\nhello b\n", env=env)
            self.assertIn("removed dev.dock.hellorm", r.stdout)
            self.assertIn("unknown command 'hello'", r.stderr)
            self.assertNotIn("hello, b!", r.stdout)
            log = Path(home, "logs", "dev.dock.hellorm.log")
            self.assertEqual(log.read_text().count(" start "), 1)
            self.assertFalse(dockenv.image_exists("dock/dev.dock.hellorm:0.1.0"))
        with self.subTest("install"):
            tmp = Path(tempfile.mkdtemp(prefix="dock-shell-copy-"))
            copy = dockenv.plugin_copy(ROOT.parent.parent / "plugins" / "hello", tmp, "dev.dock.hellocopy")
            self.addCleanup(dockenv.remove_image, "dock/dev.dock.hellocopy:0.1.0")
            home = tempfile.mkdtemp(prefix="dock-shell-priv-")  # its own home and logs: nothing carries over from the case before
            env = {**self.env, "DOCK_HOME": home}
            self.assertEqual(self.dock("plugin", "install", str(copy), env=env).returncode, 0)
            dockenv.write_config(home, dockenv.require_runtime())
            r = self.dock("shell", stdin=f"hello a\nplugin install {copy}\nhello b\n", env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("installed dev.dock.hellocopy", r.stdout)
            self.assertIn("hello, b!", r.stdout)
            log = Path(home, "logs", "dev.dock.hellocopy.log")
            self.assertEqual(log.read_text().count(" start "), 2)

    # AC11
    def _idle_run(self, timeout):
        home = tempfile.mkdtemp(prefix="dock-shell-idle-")
        env = {**self.env, "DOCK_HOME": home}
        self.dock("plugin", "install", str(ROOT.parent.parent / "plugins" / "hello"), env=env)
        dockenv.write_config(home, dockenv.require_runtime())  # a first install asks the runtime nothing, so setting the runtime here is only the last step of the setup
        dockenv.add_config(home, f"[shell]\nidle_timeout = {timeout}\n")
        p = subprocess.Popen([sys.executable, "-m", "src", "shell"], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             env=env, cwd=ROOT, **dockenv.SIGINT_DEFAULT)
        p.stdin.write("hello Bob\n")
        p.stdin.flush()
        seen = ""
        while "hello, Bob!" not in seen:  # the container start is not instant: idle time counts from the reply
            line = p.stdout.readline()
            if line == "":
                p.kill()
                self.fail(f"the shell ended before greeting: {p.stderr.read()}")
            seen += line
        time.sleep(2.5)
        out, _ = p.communicate("\nstatus\n", timeout=60)
        return out

    def test_idle_timeout_zero_keeps_plugin(self):
        out = self._idle_run(0)
        self.assertNotIn("no plugins running", out)
        self.assertIn("dev.dock.hello", out)

    # AC12
    def test_status_lists_running_plugins(self):
        r = self.shell("status\nhello Bob\nstatus\n")
        self.assertIn("no plugins running", r.stdout)
        tail = r.stdout.rsplit("no plugins running", 1)[1]
        self.assertRegex(tail, r"dev\.dock\.hello\s+running\s+idle 0m\d\ds")
        for removed in ("provides:", "isolated:", "wasm"):  # AC15
            self.assertNotIn(removed, tail)

    # AC13
    def _shell_obj(self):
        sys.path.insert(0, str(ROOT))
        from src.shell import Shell
        from src.store import Store
        return Shell(Store(Path(self.home)), None)

    def test_completion(self):
        import types
        sh = self._shell_obj()
        with self.subTest("first word"):
            self.assertEqual(sh.candidates("he", "he"), ["hello", "help"])
        with self.subTest("plugin subcommands and ids"):
            self.assertEqual(sh.candidates("plugin ", ""), ["list", "info", "install", "build", "remove", "update"])
            self.assertEqual(sh.candidates("plugin update ", ""), ["dev.dock.hello", "dev.dock.probe"])
            self.assertEqual(sh.candidates("plugin update dev.dock.hello ", ""), ["dev.dock.probe"])
            self.assertEqual(sh.candidates("plugin info dev.dock.", "dev.dock."),
                             ["dev.dock.hello", "dev.dock.probe"])
            self.assertEqual(sh.candidates("plugin build dev.dock.h", "dev.dock.h"), ["dev.dock.hello"])
        with self.subTest("complete uses line buffer and state"):
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

    def test_history_file_records_entered_lines_and_is_private(self):
        home = tempfile.mkdtemp(prefix="dock-shell-hist-")
        env = {**self.env, "DOCK_HOME": home}
        self.dock("plugin", "install", str(ROOT.parent.parent / "plugins" / "hello"), env=env)
        dockenv.write_config(home, dockenv.require_runtime())  # a first install asks the runtime nothing, so setting the runtime here is only the last step of the setup
        r = self.dock("shell", stdin="hello a\n\nnosuch\n", env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.history(home), ["hello a", "nosuch"])
        self.assertEqual(Path(home, "history").stat().st_mode & 0o777, 0o600)

    def test_history_is_capped_at_1000(self):
        home = tempfile.mkdtemp(prefix="dock-shell-hist-")
        env = {**self.env, "DOCK_HOME": home}
        self.dock("plugin", "install", str(ROOT.parent.parent / "plugins" / "hello"), env=env)
        dockenv.write_config(home, dockenv.require_runtime())  # a first install asks the runtime nothing, so setting the runtime here is only the last step of the setup
        r = self.dock("shell", stdin="help\n" * 1100, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.history(home)), 1000)


    # ---- startup failures and interrupts ---------------------------------------
    # ---- cancel that is ignored -------------------------------------------------
    def _assert_stubborn_plugin_replaced(self, sh):
        cid = self.cid(self.home)
        sh.send("code 2")  # would hang behind the stuck plugin if it were kept
        sh.wait_for("exit 2", stream="err", timeout=20)
        self.assertEqual(sh.finish(), 0, sh.err)
        self.assertNotIn("stubborn late", sh.out)
        self.assertEqual(self.starts("dev.dock.probe"), 2)
        self.assertTrue(dockenv.wait_container_gone(cid), "the stuck plugin container is still running")

    def test_second_ctrl_c_stops_a_plugin_that_ignores_cancel(self):
        sh = self.shell_proc()
        sh.send("stubborn 30")
        sh.wait_for("stubborn started")
        sh.sigint()
        sh.wait_for("cancelling", stream="err")
        sh.sigint()
        sh.wait_for(PROMPT, count=2)
        self._assert_stubborn_plugin_replaced(sh)

    # ---- exit paths, built-ins ----------------------------------
    def test_eof_and_exit_clean_up_the_same_way(self):
        for ending in ("", "exit\n"):
            with self.subTest(ending=ending):
                r = self.shell("pid\n" + ending)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertTrue(dockenv.wait_container_gone(self.cid(self.home)))

    def test_builtins_work_inside_the_shell(self):
        r = self.shell("help\nversion\nplugin list\nplugin info dev.dock.hello\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("plugin install", r.stdout)
        self.assertIn(f"dock {__version__}", r.stdout)
        self.assertRegex(r.stdout, r"dev\.dock\.hello\s+0\.2\.0")
        self.assertIn("id:          dev.dock.hello", r.stdout)
        # the help it printed lists the shell built-ins and not `shell`
        for word in ("status", "exit, quit"):
            self.assertRegex(r.stdout, rf"(?m)^  {word}\b", word)
        self.assertNotRegex(r.stdout, r"(?m)^  shell\b")
        self.assertNotIn("usage: dock <command>", r.stdout)

    def test_unwritable_history_warns_once_and_exits_cleanly(self):
        home, env = self.private_home("hello")
        Path(home, "history").mkdir()  # a directory cannot be written as a file
        r = self.dock("shell", stdin="hello a\n", env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr.count("cannot write history"), 1, r.stderr)
        self.assertIn("hello, a!", r.stdout)

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

    def test_a_broken_config_toml_stops_every_plugin_start_but_the_shell_survives(self):
        home, env = self.private_home("hello", config="[shell\n")
        r = self.dock("shell", stdin="hello a\nhello b\nstatus\nversion\n", env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertGreaterEqual(r.stderr.count("config.toml"), 2, r.stderr)  # each start is refused, loudly
        self.assertNotIn("hello, a!", r.stdout)
        self.assertNotIn("hello, b!", r.stdout)
        self.assertIn("no plugins running", r.stdout)
        self.assertIn(f"dock {__version__}", r.stdout)  # built-ins that start no plugin still work
        self.assertFalse(Path(home, "logs", "dev.dock.hello.log").exists(), "a plugin process was started")


class SessionUnit(unittest.TestCase):
    """Session behaviour that needs no subprocess of the test to observe."""

    @classmethod
    def setUpClass(cls):
        dockenv.prebuild_plugins(ROOT.parent.parent / "plugins" / "hello")

    def setUp(self):
        self.home = dockenv.make_home("dock-session-", ROOT.parent.parent / "plugins" / "hello")
        self.store = Store(self.home)
        self.session = Session(self.store)
        self.addCleanup(self.session.close)

    def test_stop_proc_forgets_the_process(self):
        manifest = self.store.get("dev.dock.hello")
        with self.subTest("drops replies still queued for it"):
            proc = self.session.start(manifest, PlainUI(manifest.id))
            other = self.session.start(manifest, PlainUI(manifest.id))
            self.session.hub.replies[(proc, 99)] = {"id": 99}
            self.session.hub.replies[(other, 99)] = {"id": 99}
            self.session.stop_proc(proc)
            self.assertEqual(list(self.session.hub.replies), [(other, 99)])
        with self.subTest("messages from a forgotten process are ignored"):
            self.session.hub.replies.clear()
            proc = self.session.start(manifest, PlainUI(manifest.id))
            self.session.stop_proc(proc)
            self.session.hub.inbox.put((proc, "msg", {"id": 99, "result": {}}))
            self.session.hub.pump(5)
            self.assertEqual(self.session.hub.replies, {})

    def test_drain_pending(self):
        manifest = self.store.get("dev.dock.hello")
        with self.subTest("returns once nothing is pending"):
            self.assertTrue(self.session.drain_pending(time.monotonic() + 5))
            proc = self.session.start(manifest, PlainUI(manifest.id))
            proc.pending.add(99)  # a reply that never comes
            started = time.monotonic()
            self.assertFalse(self.session.drain_pending(started + 0.3))
            self.assertGreaterEqual(time.monotonic() - started, 0.3)
            proc.pending.clear()
            self.assertTrue(self.session.drain_pending(time.monotonic() + 5))
        with self.subTest("collects a reply that arrives before the deadline"):
            proc = self.session.start(manifest, PlainUI(manifest.id))
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
                proc = self.session.start(manifest, PlainUI(manifest.id))
                self.session.stop_proc(proc)  # graceful
            killed = self.session.start(manifest, PlainUI(manifest.id))
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
                self.session._interrupted(PlainUI("dev.dock.test"))
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
        from src.shell import Shell

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

    def test_ctrl_c_while_reaping_keeps_the_shell_running(self):
        with self.subTest("before the prompt"):
            code, session = self.run_loop([KeyboardInterrupt()], ["version"])
            self.assertEqual(code, 0)
            session.close.assert_called_once()
        with self.subTest("after a line"):
            # the first reap (before the prompt) succeeds, the second (after the line) is interrupted
            code, session = self.run_loop([[], KeyboardInterrupt()], ["version", "version"])
            self.assertEqual(code, 0)
            session.close.assert_called_once()
            self.assertGreaterEqual(session.reap_idle.call_count, 4)


if __name__ == "__main__":
    unittest.main()
