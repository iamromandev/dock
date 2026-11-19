"""Session seams that `dock serve` relies on: a UI factory and an explicit cwd. Run `make test`."""

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
            result = session.invoke_result(manifest, "pid", [])
        self.assertEqual(prepare.call_args.kwargs["cwd"], project)
        self.assertEqual(result["cwd"], project)  # what the plugin was told in `invoke`
        self.assertTrue(any(who == "dev.dock.pidprobe" and kind == "output" and " cid " in text
                            for who, kind, text in self.events))
        self.assertEqual(session.cwd, project)

    def test_without_a_cwd_the_process_cwd_is_used_as_before(self):
        session = self.session()
        manifest = self.store.get("dev.dock.pidprobe")
        with mock.patch.object(isolation, "prepare", wraps=isolation.prepare) as prepare:
            session.invoke(manifest, "pid", [])
        self.assertIsNone(prepare.call_args.kwargs["cwd"])


class PluginRequests(unittest.TestCase):
    """A plugin may only ask the host for output, log, progress and prompt_user: nothing else is answered."""

    def test_a_plugin_request_for_call_or_http_gets_method_not_found(self):
        store = Store(Path(tempfile.mkdtemp(prefix="dock-seams-")))
        store.install(ROOT / "plugin" / "hello")
        proc = PluginProcess(store.get("dev.dock.hello"), os.devnull, {"output": lambda p: None},
                             isolation.Launch(argv=[], env={}, runtime="docker", container="c"))
        for method in ("call", "http"):
            with self.subTest(method=method), mock.patch.object(proc, "_send") as send:
                proc._handle_incoming({"id": 7, "method": method, "params": {"x": 1}})
                send.assert_called_once_with({"id": 7, "error": {"code": -32601, "message": f"host has no '{method}'"}})


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

        session = Session(store, ui_factory=lambda plugin_id: UI(plugin_id, events))
        manifest = store.get("dev.dock.pidprobe")
        try:
            self.assertEqual(session.invoke(manifest, "askhost", []), 0)
        finally:
            session.close()
        self.assertIn("call -32601 http -32601\n", texts)
        methods = (store.data_dir("dev.dock.pidprobe") / "methods.log").read_text().split()
        self.assertEqual(methods, ["initialize", "invoke", "shutdown"])  # never provision or unprovision


if __name__ == "__main__":
    unittest.main()
