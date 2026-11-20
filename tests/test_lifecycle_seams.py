"""Session behaviour seen through the UI it builds per plugin, and its cwd. Run `make test`."""

import inspect
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import dockenv  # noqa: E402
import cli.process  # noqa: E402
from cli import isolation  # noqa: E402
from cli.lifecycle import Session  # noqa: E402
from cli.process import PluginProcess  # noqa: E402
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


PLUGINS = (ROOT / "plugin" / "hello", ROOT / "tests" / "fixtures" / "pidprobe")


class SessionSeams(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        dockenv.prebuild_plugins(*PLUGINS)

    def setUp(self):
        self.store = Store(dockenv.make_home("dock-seams-", *PLUGINS))
        self.events = []
        self.made = []

    def session(self):
        patch = mock.patch("cli.lifecycle.TerminalUI",
                           side_effect=lambda plugin_id, verbose=False: self.make_ui(plugin_id))
        patch.start()
        self.addCleanup(patch.stop)
        session = Session(self.store)
        self.addCleanup(session.close)
        return session

    def make_ui(self, plugin_id):
        self.made.append(plugin_id)
        return RecordingUI(plugin_id, self.events)

    def test_default_ui_is_the_terminal_ui(self):
        session = Session(self.store, verbose=True)
        ui = session._ui("dev.dock.hello")
        self.assertIsInstance(ui, TerminalUI)
        self.assertEqual((ui.plugin_id, ui.verbose), ("dev.dock.hello", True))

    def test_invoke_sends_output_and_progress_to_the_ui(self):
        session = self.session()
        code = session.invoke(self.store.get("dev.dock.hello"), "count", ["2"])
        self.assertEqual(code, 0)
        self.assertEqual(self.made, ["dev.dock.hello"])
        self.assertIn(("dev.dock.hello", "progress", 100.0), self.events)
        self.assertIn(("dev.dock.hello", "output", "counted to 2\n"), self.events)

    def test_warnings_go_through_the_ui(self):
        def stuck(session):
            manifest = self.store.get("dev.dock.hello")
            proc = session.start(manifest, session._ui(manifest.id))
            proc.pending.add(99)  # an answer that never comes
            session._stop_unresponsive()

        def interrupted(session):
            session._interrupted(session._ui("dev.dock.hello"))

        cases = (("stuck plugin", stuck, "did not stop on cancel; stopped it"),
                 ("interrupt notice", interrupted, "interrupted, cancelling…"))
        for name, act, message in cases:
            with self.subTest(name):
                self.events.clear()
                act(self.session())
                self.assertIn(("dev.dock.hello", "warn", message), self.events)

    def test_the_process_cwd_is_what_the_plugin_is_told(self):
        project = os.path.realpath(tempfile.mkdtemp(prefix="dock-seams-cwd-"))
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(project)
        session = self.session()
        with mock.patch.object(isolation, "prepare", wraps=isolation.prepare) as prepare:
            session.invoke(self.store.get("dev.dock.pidprobe"), "pid", [])
        self.assertEqual(prepare.call_args.args, (self.store, self.store.get("dev.dock.pidprobe")))
        self.assertNotIn("cwd", prepare.call_args.kwargs)
        self.assertTrue(any(kind == "output" and f"cwd {project}" in text for _, kind, text in self.events), self.events)


class RemovedSeams(unittest.TestCase):
    """No runtime needed: what only `dock serve` used is gone."""

    def test_session_takes_no_ui_factory_or_cwd(self):
        store = Store(Path(tempfile.mkdtemp(prefix="dock-seams-home-")))
        for kw in ({"ui_factory": lambda plugin_id: None}, {"cwd": "/tmp"}):
            with self.subTest(kw=list(kw)):
                with self.assertRaises(TypeError):
                    Session(store, **kw)

    def test_the_cross_thread_cancel_is_gone(self):
        self.assertFalse(hasattr(Session, "invoke_result"))
        self.assertFalse(hasattr(cli.process, "Interrupted"))
        for method in (PluginProcess.wait_for, PluginProcess.request):
            self.assertNotIn("stop", inspect.signature(method).parameters)


class PluginAsksTheHost(unittest.TestCase):
    """AC12: a real plugin that sends `call` and `http` requests gets -32601 for each, and `provision` is never sent."""

    @classmethod
    def setUpClass(cls):
        cls.pidprobe = ROOT / "tests" / "fixtures" / "pidprobe"
        dockenv.prebuild_plugins(cls.pidprobe)

    def test_call_and_http_get_method_not_found_and_the_host_never_sends_provision(self):
        store = Store(dockenv.make_home("dock-seams-ask-", self.pidprobe))
        texts = []
        events = []

        class UI(RecordingUI):
            def output(self, p):
                texts.append(p.get("text"))

        manifest = store.get("dev.dock.pidprobe")
        with mock.patch("cli.lifecycle.TerminalUI", side_effect=lambda plugin_id, verbose=False: UI(plugin_id, events)):
            session = Session(store)
            try:
                self.assertEqual(session.invoke(manifest, "askhost", []), 0)
            finally:
                session.close()
        self.assertIn("call -32601 http -32601\n", texts)
        methods = (store.data_dir("dev.dock.pidprobe") / "methods.log").read_text().split()
        self.assertEqual(methods, ["initialize", "invoke", "shutdown"])  # never provision or unprovision


if __name__ == "__main__":
    unittest.main()
