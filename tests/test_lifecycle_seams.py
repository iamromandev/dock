"""Session seams that `dock serve` relies on: a UI factory and an explicit cwd. Run `make test`."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cli import isolation  # noqa: E402
from cli.lifecycle import Session  # noqa: E402
from cli.store import Store  # noqa: E402
from cli.ui import TerminalUI  # noqa: E402


class RecordingUI:
    """A UI that keeps what it is told, to show where a Session sends its output."""

    def __init__(self, plugin_id, events):
        self.plugin_id = plugin_id
        self.events = events

    def output(self, p):
        self.events.append((self.plugin_id, "output", p.get("text")))

    def log(self, p):
        self.events.append((self.plugin_id, "log", p.get("message")))

    def progress(self, p):
        self.events.append((self.plugin_id, "progress", p.get("percent")))

    def prompt_user(self, p):
        self.events.append((self.plugin_id, "prompt", p.get("prompt")))
        return {"eof": True}

    def warn(self, message):
        self.events.append((self.plugin_id, "warn", message))

    def handlers(self):
        return {"output": self.output, "log": self.log, "progress": self.progress, "prompt_user": self.prompt_user}


class SessionSeams(unittest.TestCase):
    def setUp(self):
        self.store = Store(Path(tempfile.mkdtemp(prefix="dock-seams-")))
        for name in ("hello", "llm"):
            self.store.install(ROOT / "plugin" / name)
        self.store.install(ROOT / "tests" / "fixtures" / "pidprobe")
        self.events = []
        self.made = []

    def factory(self, plugin_id):
        self.made.append(plugin_id)
        return RecordingUI(plugin_id, self.events)

    def session(self, **kw):
        session = Session(self.store, ui_factory=self.factory, **kw)
        self.addCleanup(session.close)
        return session

    def test_default_ui_is_the_terminal_ui(self):
        session = Session(self.store, verbose=True)
        ui = session.ui_factory("dev.dock.hello")
        self.assertIsInstance(ui, TerminalUI)
        self.assertEqual((ui.plugin_id, ui.verbose), ("dev.dock.hello", True))

    def test_invoke_sends_output_and_progress_to_the_factorys_ui(self):
        session = self.session()
        code = session.invoke(self.store.get("dev.dock.hello"), "count", ["2"])
        self.assertEqual(code, 0)
        self.assertEqual(self.made, ["dev.dock.hello"])
        self.assertIn(("dev.dock.hello", "progress", 100.0), self.events)
        self.assertIn(("dev.dock.hello", "output", "counted to 2\n"), self.events)

    def test_a_provider_gets_its_own_ui_from_the_factory(self):
        session = self.session()
        proc = session.provider_for("llm")
        self.assertEqual(self.made, ["dev.dock.llm"])
        self.assertNotIn("prompt_user", proc.handlers)  # only the command's plugin talks to the user

    def test_a_stuck_plugin_is_reported_through_the_factorys_ui(self):
        session = self.session()
        manifest = self.store.get("dev.dock.hello")
        proc = session.start(manifest, session.ui_factory(manifest.id))
        proc.pending.add(99)  # an answer that never comes
        session._stop_unresponsive()
        self.assertIn(("dev.dock.hello", "warn", "did not stop on cancel; stopped it"), self.events)

    def test_interrupt_notice_goes_through_the_given_ui(self):
        session = self.session()
        session._interrupted(session.ui_factory("dev.dock.hello"))
        self.assertIn(("dev.dock.hello", "warn", "interrupted, cancelling…"), self.events)

    def test_cwd_is_what_isolation_mounts_and_what_the_plugin_is_told(self):
        project = tempfile.mkdtemp(prefix="dock-seams-cwd-")
        session = self.session(cwd=project)
        manifest = self.store.get("dev.dock.pidprobe")
        with mock.patch.object(isolation, "prepare", wraps=isolation.prepare) as prepare:
            session.invoke(manifest, "pid", [])
        self.assertEqual(prepare.call_args.kwargs["cwd"], project)
        self.assertIn(("dev.dock.pidprobe", "output", f"pid {session.commands[manifest.id].proc.pid}\n"),
                      self.events)
        self.assertEqual(session.cwd, project)

    def test_without_a_cwd_the_process_cwd_is_used_as_before(self):
        session = self.session()
        manifest = self.store.get("dev.dock.pidprobe")
        with mock.patch.object(isolation, "prepare", wraps=isolation.prepare) as prepare:
            session.invoke(manifest, "pid", [])
        self.assertIsNone(prepare.call_args.kwargs["cwd"])


if __name__ == "__main__":
    unittest.main()
