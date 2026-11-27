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
PROBE = ROOT / "tests" / "fixtures" / "probe"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import dockenv
from src.store import Store

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
            [sys.executable, "-m", "src", *(args or ("shell",))],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            cwd=ROOT,
            **dockenv.SIGINT_DEFAULT,
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
        assert ok, (
            f"timed out waiting for {text!r} x{count} on {stream}\nstdout: {self.out!r}\nstderr: {self.err!r}"
        )

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
        cls.plugins = (ROOT / "tests" / "fixtures" / "probe", ROOT / "tests" / "fixtures" / "uiprobe")
        dockenv.prebuild_plugins(*cls.plugins)
        cls.home = tempfile.mkdtemp(prefix="dock-shell-home-")
        cls.env = {**os.environ, "DOCK_HOME": cls.home, "PYTHONPATH": str(ROOT)}
        for path in cls.plugins:
            r = cls.dock("plugin", "install", str(path))
            assert r.returncode == 0, r.stderr
        dockenv.write_config(
            cls.home, dockenv.require_runtime()
        )  # a first install asks the runtime nothing, so setting the runtime here is only the last step of the setup

    @staticmethod
    def cid(home):
        """The container id the probe wrote when it last ran its `pid` or `stubborn` command."""
        return Path(home, "data", "dev.dock.probe", "cid").read_text()

    @classmethod
    def dock(cls, *args, stdin="", env=None):
        return subprocess.run(
            [sys.executable, "-m", "src", *args],
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
            env=env or cls.env,
            cwd=ROOT,
            timeout=120,
            **dockenv.SIGINT_DEFAULT,
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
        """A fresh DOCK_HOME with the named fixture plugins installed; returns (home, env)."""
        home = tempfile.mkdtemp(prefix="dock-shell-priv-")
        env = {**self.env, "DOCK_HOME": home}
        for name in plugins:
            r = self.dock("plugin", "install", str(ROOT / "tests" / "fixtures" / name), env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
        dockenv.write_config(
            home, dockenv.require_runtime()
        )  # a first install asks the runtime nothing, so setting the runtime here is only the last step of the setup
        if config:
            dockenv.add_config(home, config)
        return home, env

    # AC1

    # AC2

    # AC4

    # AC5

    # AC6

    # R7: a cancel that reaches a plugin with nothing in flight changes nothing

    # AC8

    # AC11
    def _idle_run(self, timeout):
        home = tempfile.mkdtemp(prefix="dock-shell-idle-")
        env = {**self.env, "DOCK_HOME": home}
        self.dock("plugin", "install", str(PROBE), env=env)
        dockenv.write_config(
            home, dockenv.require_runtime()
        )  # a first install asks the runtime nothing, so setting the runtime here is only the last step of the setup
        dockenv.add_config(home, f"[shell]\nidle_timeout = {timeout}\n")
        p = subprocess.Popen(
            [sys.executable, "-m", "src", "shell"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            cwd=ROOT,
            **dockenv.SIGINT_DEFAULT,
        )
        p.stdin.write("echoargs Bob\n")
        p.stdin.flush()
        seen = ""
        while (
            '"args": ["Bob"]' not in seen
        ):  # the container start is not instant: idle time counts from the reply
            line = p.stdout.readline()
            if line == "":
                p.kill()
                self.fail(f"the shell ended before echoing: {p.stderr.read()}")
            seen += line
        time.sleep(2.5)
        out, _ = p.communicate("\nstatus\n", timeout=60)
        return out

    # AC12

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
            self.assertEqual(sh.candidates("ec", "ec"), ["echoargs"])
        with self.subTest("plugin subcommands and ids"):
            self.assertEqual(
                sh.candidates("plugin ", ""), ["list", "info", "install", "build", "remove", "update"]
            )
            self.assertEqual(sh.candidates("plugin update ", ""), ["dev.dock.probe", "dev.dock.uiprobe"])
            self.assertEqual(sh.candidates("plugin update dev.dock.probe ", ""), ["dev.dock.uiprobe"])
            self.assertEqual(
                sh.candidates("plugin info dev.dock.", "dev.dock."), ["dev.dock.probe", "dev.dock.uiprobe"]
            )
            self.assertEqual(sh.candidates("plugin build dev.dock.u", "dev.dock.u"), ["dev.dock.uiprobe"])
        with self.subTest("complete uses line buffer and state"):
            sh.readline = types.SimpleNamespace(get_line_buffer=lambda: "he")
            self.assertEqual(sh.complete("he", 0), "help")
            self.assertIsNone(sh.complete("he", 1))

    # AC14
    @staticmethod
    def history(home):
        # libedit writes a header line and escapes spaces as \\040
        lines = Path(home, "history").read_text().splitlines()
        return [l.replace("\\040", " ") for l in lines if l != "_HiStOrY_V2_"]

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

    # ---- exit paths, built-ins ----------------------------------

    # ---- idle reaping ---------------------------------------------------------------


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
