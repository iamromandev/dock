"""Session behaviour seen through the UI it builds per plugin, and its cwd. Run `make test`."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import dockenv
import dock_host.process
from dock_host.lifecycle import Session
from dock_host.store import Store


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
        return {
            "output": self.output,
            "log": self.log,
            "bar": self.bar,
            "table": self.table,
            "panel": self.panel,
            "clear": self.clear,
            "prompt_user": self.prompt_user,
            "form": self.form,
        }


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
        session = Session(self.store, self.make_ui)
        self.addCleanup(session.close)
        return session

    def make_ui(self, plugin_id):
        self.made.append(plugin_id)
        return RecordingUI(plugin_id, self.events)

    def test_warnings_go_through_the_ui(self):
        def stuck(session):
            manifest = self.store.get("dev.dock.uiprobe")
            proc = session.start(manifest, session._ui(manifest.id))
            proc.pending.add(99)  # an answer that never comes
            session._stop_unresponsive()

        def interrupted(session):
            session._interrupted(session._ui("dev.dock.uiprobe"))

        cases = (
            ("stuck plugin", stuck, "did not stop on cancel; stopped it"),
            ("interrupt notice", interrupted, "interrupted, cancelling…"),
        )
        for name, act, message in cases:
            with self.subTest(name):
                self.events.clear()
                act(self.session())
                self.assertIn(("dev.dock.uiprobe", "warn", message), self.events)


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
        session = Session(Store(Path(tempfile.mkdtemp(prefix="dock-seams-inputs-"))), lambda _id: RecordingUI("p", []))
        manifest = mock.Mock(id="p", permissions=[])
        proc = self.FakeProc(result)
        session.commands["p"] = proc
        return session, manifest, proc

    def test_plugin_error_propagates(self):
        session, manifest, _ = self.session_with(dock_host.process.PluginError("boom"))
        with self.assertRaises(dock_host.process.PluginError):
            session.invoke_inputs(manifest, "go", {})


class InvokeBracketsTheUI(unittest.TestCase):
    """No runtime needed: a run's live areas are frozen (ui.end) on every way out of Session.invoke."""

    def run_invoke(self, outcome):
        events = []
        session = Session(Store(Path(tempfile.mkdtemp(prefix="dock-seams-ui-"))), lambda _id: RecordingUI("p", events))

        def fake_invoke(*args):
            events.append(("plugin", "invoke", None))
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        with mock.patch.object(Session, "_invoke", side_effect=fake_invoke):
            try:
                code = session.invoke(mock.Mock(id="p"), "go", [])
            except BaseException as e:
                code = type(e)
        return code, [kind for _, kind, _ in events]

    def test_end_runs_when_the_plugin_fails(self):
        code, kinds = self.run_invoke(dock_host.process.PluginError("boom"))
        self.assertEqual((code, kinds), (1, ["begin", "invoke", "warn", "end"]))


if __name__ == "__main__":
    unittest.main()
