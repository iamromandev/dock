"""How live mode is wired in: Session, one-shot runs and the shell choose the renderer and own the Terminal (R14, R17)."""

import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import main, shell
from src.store import Store
from src.ui import PlainUI


class FakeTerminal:
    instances = []

    def __init__(self, *a, **k):
        self.calls = []
        self.lock = threading.RLock()
        FakeTerminal.instances.append(self)

    def write(self, s):
        pass

    def install(self):
        self.calls.append("install")

    def close(self):
        self.calls.append("close")


class OneShot(unittest.TestCase):
    def setUp(self):
        FakeTerminal.instances.clear()
        self.run_args = []
        patches = [
            mock.patch.object(main, "Terminal", FakeTerminal),
            mock.patch.object(main, "Store"),
            mock.patch.object(main, "parse_for"),
            mock.patch.object(main.isolation, "sweep"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        main.Store.return_value.resolve.return_value = (mock.Mock(), "cmd")
        main.Store.return_value.config.return_value = {}  # no config.toml: no [paths]

    def run_main(self, live, result=0):
        def fake_run(*args, **kwargs):
            self.run_args.append(kwargs)
            self.run_calls_seen = list(FakeTerminal.instances[0].calls) if FakeTerminal.instances else None
            if isinstance(result, BaseException):
                raise result
            return result

        with (
            mock.patch.object(main, "live_mode", return_value=live),
            mock.patch.object(main.lifecycle, "run", side_effect=fake_run),
        ):
            return main.main(["cmd"])

    def test_the_terminal_is_closed_on_ctrl_c_and_the_exit_code_is_unchanged(self):
        self.assertEqual(self.run_main(True, KeyboardInterrupt()), 130)
        self.assertEqual(FakeTerminal.instances[0].calls, ["install", "close"])


class ShellStart(unittest.TestCase):
    def setUp(self):
        FakeTerminal.instances.clear()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Store(Path(tmp.name))
        self.shells = []

        def fake_loop(shell_self):
            self.shells.append(shell_self)
            return 0

        for p in (
            mock.patch.object(shell, "Terminal", FakeTerminal),
            mock.patch.object(shell.Shell, "loop", fake_loop),
            mock.patch.object(shell.isolation, "sweep"),
        ):
            p.start()
            self.addCleanup(p.stop)

    def start(self, live, tty):
        with (
            mock.patch.object(shell, "live_mode", return_value=live),
            mock.patch.object(shell.sys.stdin, "isatty", return_value=tty),
        ):
            return shell.start(self.store, False)

    def test_the_editor_completes_with_the_shells_candidates(self):
        self.start(True, True)
        editor = self.shells[0].input_fn.__self__
        self.assertEqual(editor.complete("pl", "pl"), ["plugin"])

    def test_without_live_mode_the_line_based_shell_runs_unchanged(self):
        self.assertEqual(self.start(False, True), 0)
        self.assertEqual(FakeTerminal.instances, [])
        [sh] = self.shells
        self.assertIsInstance(sh.session.ui_factory("p"), PlainUI)
        self.assertIs(sh.input_fn, input)


if __name__ == "__main__":
    unittest.main()
