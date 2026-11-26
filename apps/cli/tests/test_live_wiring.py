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
from src.lifecycle import Session
from src.live import LiveUI
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


class SessionUi(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Store(Path(tmp.name))

    def test_a_session_with_a_terminal_draws_live(self):
        ui = Session(self.store, term=FakeTerminal())._ui("p")
        self.assertIsInstance(ui, LiveUI)

    def test_a_session_without_one_prints_plain(self):
        self.assertIsInstance(Session(self.store)._ui("p"), PlainUI)


class Warnings(unittest.TestCase):
    """While a region is drawn, every line for the user goes through the UI that draws it."""

    def test_a_stuck_plugin_is_reported_through_the_ui_of_the_run_not_a_second_one(self):
        class UI:
            warnings = []

            def warn(self, message):
                self.warnings.append(message)

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        session = Session(Store(Path(tmp.name)), term=FakeTerminal())
        proc = mock.Mock(pending={1})
        proc.alive.return_value = True
        proc.manifest.id = "dev.dock.stuck"
        session.procs = [proc]
        with mock.patch.object(session, "_ui", side_effect=AssertionError("a second UI was built")), \
                mock.patch.object(session, "stop_proc"):
            session._stop_unresponsive(UI())
        self.assertEqual(UI.warnings, ["did not stop on cancel; stopped it"])


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

    def run_main(self, live, result=0):
        def fake_run(*args, **kwargs):
            self.run_args.append(kwargs)
            self.run_calls_seen = list(FakeTerminal.instances[0].calls) if FakeTerminal.instances else None
            if isinstance(result, BaseException):
                raise result
            return result

        with mock.patch.object(main, "live_mode", return_value=live), \
                mock.patch.object(main.lifecycle, "run", side_effect=fake_run):
            return main.main(["cmd"])

    def test_in_live_mode_the_terminal_is_installed_before_the_run_and_closed_after(self):
        self.assertEqual(self.run_main(True, 7), 7)
        [term] = FakeTerminal.instances
        self.assertEqual(self.run_calls_seen, ["install"])
        self.assertEqual(term.calls, ["install", "close"])
        self.assertIs(self.run_args[0]["term"], term)

    def test_the_terminal_is_closed_when_the_run_raises(self):
        with self.assertRaises(RuntimeError):
            self.run_main(True, RuntimeError("boom"))
        self.assertEqual(FakeTerminal.instances[0].calls, ["install", "close"])

    def test_the_terminal_is_closed_on_ctrl_c_and_the_exit_code_is_unchanged(self):
        self.assertEqual(self.run_main(True, KeyboardInterrupt()), 130)
        self.assertEqual(FakeTerminal.instances[0].calls, ["install", "close"])

    def test_not_in_live_mode_there_is_no_terminal(self):
        self.assertEqual(self.run_main(False, 3), 3)
        self.assertEqual(FakeTerminal.instances, [])
        self.assertIsNone(self.run_args[0]["term"])


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

        for p in (mock.patch.object(shell, "Terminal", FakeTerminal),
                  mock.patch.object(shell.Shell, "loop", fake_loop),
                  mock.patch.object(shell.isolation, "sweep")):
            p.start()
            self.addCleanup(p.stop)

    def start(self, live, tty):
        with mock.patch.object(shell, "live_mode", return_value=live), \
                mock.patch.object(shell.sys.stdin, "isatty", return_value=tty):
            return shell.start(self.store, False)

    def test_live_mode_on_a_terminal_opens_the_inline_shell(self):
        self.assertEqual(self.start(True, True), 0)
        [term] = FakeTerminal.instances
        [sh] = self.shells
        self.assertIs(sh.session.term, term)
        self.assertIsInstance(sh.input_fn.__self__, shell.LineEditor)
        self.assertEqual(term.calls, ["install", "close"])

    def test_the_editor_completes_with_the_shells_candidates(self):
        self.start(True, True)
        editor = self.shells[0].input_fn.__self__
        self.assertEqual(editor.complete("pl", "pl"), ["plugin"])

    def test_the_saved_history_is_handed_to_the_editor_oldest_first(self):
        class Readline:
            items = ["first", "second"]

            def get_current_history_length(self):
                return len(self.items)

            def get_history_item(self, i):  # 1-based, as in the readline module
                return self.items[i - 1]

        self.assertEqual(shell._history(Readline()), ["first", "second"])
        self.assertEqual(shell._history(None), [])

    def test_without_live_mode_the_line_based_shell_runs_unchanged(self):
        self.assertEqual(self.start(False, True), 0)
        self.assertEqual(FakeTerminal.instances, [])
        [sh] = self.shells
        self.assertIsNone(sh.session.term)
        self.assertIs(sh.input_fn, input)

    def test_piped_stdin_runs_the_line_based_shell_even_when_stderr_is_a_terminal(self):
        self.start(True, False)
        self.assertEqual(FakeTerminal.instances, [])
        self.assertIs(self.shells[0].input_fn, input)


if __name__ == "__main__":
    unittest.main()
