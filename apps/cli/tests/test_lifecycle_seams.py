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

import dockenv
import src.process
from src import isolation
from src.lifecycle import Session
from src.process import PluginProcess
from src.store import Store
from src.ui import PlainUI


class RecordingUI:
    """A UI that keeps what it is told, to show where a Session sends its output."""

    def __init__(self, plugin_id, events):
        self.plugin_id = plugin_id
        self.events = events

    def output(self, p):
        self.events.append((self.plugin_id, "output", p.get("text")))

    def log(self, p):
        self.events.append((self.plugin_id, "log", p.get("message")))

    def bar(self, p):
        self.events.append((self.plugin_id, "bar", p.get("percent")))

    def table(self, p):
        self.events.append((self.plugin_id, "table", p.get("id")))

    def panel(self, p):
        self.events.append((self.plugin_id, "panel", p.get("id")))

    def clear(self, p):
        self.events.append((self.plugin_id, "clear", p.get("id")))

    def form(self, p):
        self.events.append((self.plugin_id, "form", p.get("title")))
        return {"eof": True}

    def begin(self):
        self.events.append((self.plugin_id, "begin", None))

    def end(self):
        self.events.append((self.plugin_id, "end", None))

    def prompt_user(self, p):
        self.events.append((self.plugin_id, "prompt", p.get("prompt")))
        return {"eof": True}

    def warn(self, message):
        self.events.append((self.plugin_id, "warn", message))

    def handlers(self):
        return {"output": self.output, "log": self.log, "bar": self.bar, "table": self.table, "panel": self.panel,
                "clear": self.clear, "prompt_user": self.prompt_user, "form": self.form}


PLUGINS = (ROOT / "tests" / "fixtures" / "uiprobe", ROOT / "tests" / "fixtures" / "pidprobe")


class SessionSeams(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        dockenv.prebuild_plugins(*PLUGINS)

    def setUp(self):
        self.store = Store(dockenv.make_home("dock-seams-", *PLUGINS))
        self.events = []
        self.made = []

    def session(self):
        patch = mock.patch("src.lifecycle.make_ui",
                           side_effect=lambda plugin_id, verbose=False, term=None: self.make_ui(plugin_id))
        patch.start()
        self.addCleanup(patch.stop)
        session = Session(self.store)
        self.addCleanup(session.close)
        return session

    def make_ui(self, plugin_id):
        self.made.append(plugin_id)
        return RecordingUI(plugin_id, self.events)

    def test_default_ui_is_the_plain_ui_when_not_live(self):
        session = Session(self.store, verbose=True)
        ui = session._ui("dev.dock.uiprobe")
        self.assertIsInstance(ui, PlainUI)
        self.assertEqual((ui.plugin_id, ui.verbose), ("dev.dock.uiprobe", True))

    def test_invoke_sends_output_and_bars_to_the_ui(self):
        session = self.session()
        code = session.invoke(self.store.get("dev.dock.uiprobe"), "show", ["basic"])
        self.assertEqual(code, 0)
        self.assertEqual(self.made, ["dev.dock.uiprobe"])
        self.assertIn(("dev.dock.uiprobe", "bar", 100.0), self.events)
        self.assertIn(("dev.dock.uiprobe", "output", "basic done\n"), self.events)

    def test_warnings_go_through_the_ui(self):
        def stuck(session):
            manifest = self.store.get("dev.dock.uiprobe")
            proc = session.start(manifest, session._ui(manifest.id))
            proc.pending.add(99)  # an answer that never comes
            session._stop_unresponsive()

        def interrupted(session):
            session._interrupted(session._ui("dev.dock.uiprobe"))

        cases = (("stuck plugin", stuck, "did not stop on cancel; stopped it"),
                 ("interrupt notice", interrupted, "interrupted, cancelling…"))
        for name, act, message in cases:
            with self.subTest(name):
                self.events.clear()
                act(self.session())
                self.assertIn(("dev.dock.uiprobe", "warn", message), self.events)

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


class InvokeInputs(unittest.TestCase):
    """No runtime needed: a flow hands ready-made inputs to a running plugin and gets the whole result back."""

    class FakeProc:
        def __init__(self, result):
            self.result = result
            self.sent = []
            self.handlers = {}

        def alive(self):
            return True

        def send_request(self, method, params):
            self.sent.append((method, params))
            return 1

        def wait_for(self, invoke_id, method, timeout=None):
            if isinstance(self.result, BaseException):
                raise self.result
            return self.result

    def session_with(self, result):
        session = Session(Store(Path(tempfile.mkdtemp(prefix="dock-seams-inputs-"))))
        manifest = mock.Mock(id="p")
        proc = self.FakeProc(result)
        session.commands["p"] = proc
        patch = mock.patch("src.lifecycle.make_ui", side_effect=lambda *a, **k: RecordingUI("p", []))
        patch.start()
        self.addCleanup(patch.stop)
        return session, manifest, proc

    def test_inputs_are_sent_as_given_without_parsing_args(self):
        session, manifest, proc = self.session_with({"exit_code": 0, "outputs": {"files": ["a"]}})
        with mock.patch("src.lifecycle.parse_for", side_effect=AssertionError("must not parse")):
            result = session.invoke_inputs(manifest, "go", {"n": 2})
        self.assertEqual(result, {"exit_code": 0, "outputs": {"files": ["a"]}})
        method, params = proc.sent[0]
        self.assertEqual((method, params["command"], params["args"], params["inputs"]), ("invoke", "go", [], {"n": 2}))

    def test_work_is_sent_only_when_the_session_has_a_work_folder(self):
        session, manifest, proc = self.session_with({"exit_code": 0})
        session.invoke_inputs(manifest, "go", {"n": 1})
        self.assertNotIn("work", proc.sent[0][1])
        session.work_dir = Path("/some/work")
        session.invoke_inputs(manifest, "go", {"n": 1})
        self.assertEqual(proc.sent[1][1]["work"], "/some/work")

    def test_none_inputs_omit_the_inputs_key_and_send_no_args(self):
        from src.manifest import Command

        session, manifest, proc = self.session_with({"exit_code": 0})
        manifest.command.return_value = Command("go")  # declares no inputs
        session.invoke_inputs(manifest, "go", None)
        params = proc.sent[0][1]
        self.assertNotIn("inputs", params)
        self.assertEqual(params["args"], [])

    def test_plugin_error_propagates(self):
        session, manifest, _ = self.session_with(src.process.PluginError("boom"))
        with self.assertRaises(src.process.PluginError):
            session.invoke_inputs(manifest, "go", {})

    def test_invoke_still_returns_the_exit_code(self):
        session, manifest, _ = self.session_with({"exit_code": 4, "outputs": {}})
        with mock.patch("src.lifecycle.parse_for", return_value=None):
            self.assertEqual(session.invoke(manifest, "go", []), 4)


class InvokeBracketsTheUI(unittest.TestCase):
    """No runtime needed: a run's live areas are frozen (ui.end) on every way out of Session.invoke."""

    def run_invoke(self, outcome):
        events = []
        session = Session(Store(Path(tempfile.mkdtemp(prefix="dock-seams-ui-"))))

        def fake_invoke(*args):
            events.append(("plugin", "invoke", None))
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        with mock.patch("src.lifecycle.make_ui", side_effect=lambda *a, **k: RecordingUI("p", events)), \
                mock.patch.object(Session, "_invoke", side_effect=fake_invoke):
            try:
                code = session.invoke(mock.Mock(id="p"), "go", [])
            except BaseException as e:
                code = type(e)
        return code, [kind for _, kind, _ in events]

    def test_begin_then_invoke_then_end(self):
        self.assertEqual(self.run_invoke({"exit_code": 3}), (3, ["begin", "invoke", "end"]))

    def test_end_runs_when_the_plugin_fails(self):
        code, kinds = self.run_invoke(src.process.PluginError("boom"))
        self.assertEqual((code, kinds), (1, ["begin", "invoke", "warn", "end"]))

    def test_end_runs_on_an_unexpected_exception(self):
        code, kinds = self.run_invoke(RuntimeError("bug"))
        self.assertEqual((code, kinds), (RuntimeError, ["begin", "invoke", "end"]))


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
        self.assertFalse(hasattr(src.process, "Interrupted"))
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
        with mock.patch("src.lifecycle.make_ui",
                        side_effect=lambda plugin_id, verbose=False, term=None: UI(plugin_id, events)):
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
