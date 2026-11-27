"""A Session that runs several invokes on one plugin process: each invoke's messages go to that invoke's UI (C1)."""

import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from src import lifecycle
from src.lifecycle import Session
from src.live import LiveUI
from src.process import PluginProcess
from src.store import Store
from src.ui import PlainUI
from test_live_unit import FakeTerm


class FakeProc:
    """A plugin process that speaks through the real hub and the real `_handle_incoming`, from a script."""

    scripts: list = []  # per invoke: the notifications the plugin sends
    instances: list = []

    def __init__(self, manifest, log, handlers, launch, hub):
        self.manifest, self.handlers, self.hub = manifest, handlers, hub
        self.pending, self.forgotten, self.exited = set(), False, False
        self._log = io.StringIO()
        self.invokes = 0
        FakeProc.instances.append(self)

    _handle_incoming = PluginProcess._handle_incoming

    def start(self):
        pass

    def request(self, *a, **k):
        return {}

    def alive(self):
        return True

    def send_request(self, *a, **k):
        return 1

    def notify(self, *a, **k):
        pass

    def wait_for(self, *a, **k):
        for method, params in FakeProc.scripts[self.invokes]:
            self.hub.inbox.put((self, "msg", {"method": method, "params": params}))
        self.invokes += 1
        while self.hub.pump(0):
            pass
        return {"exit_code": 0}

    def stop(self):
        pass

    def kill(self):
        pass


def script(tag):
    return [
        ("output", {"text": f"out {tag}\n"}),
        ("bar", {"id": "b", "label": f"bar {tag}", "percent": 50}),
        ("table", {"id": "t", "title": f"table {tag}", "columns": ["a"], "rows": [[tag]]}),
    ]


class TwoInvokes(unittest.TestCase):
    def setUp(self):
        FakeProc.instances.clear()
        FakeProc.scripts = [script("one"), script("two")]
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.make_ui = None
        self.session = Session(Store(Path(tmp.name)), lambda _id: self.make_ui())
        self.manifest = mock.Mock(permissions=[])
        self.manifest.id = "p"
        for patch in (
            mock.patch.object(lifecycle, "PluginProcess", FakeProc),
            mock.patch.object(lifecycle.isolation, "prepare"),
            mock.patch.object(lifecycle, "parse_for", return_value=None),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.out, self.err = io.StringIO(), io.StringIO()
        for name, stream in (("stdout", self.out), ("stderr", self.err)):
            patch = mock.patch.object(sys, name, stream)
            patch.start()
            self.addCleanup(patch.stop)

    def run_both(self, make_ui):
        self.make_ui = make_ui
        for _ in range(2):
            self.session.invoke(self.manifest, "c", [])
        self.assertEqual(len(FakeProc.instances), 1, "the process was meant to be reused")

    def test_live_each_invoke_shows_its_own_output_and_one_final_copy(self):
        term = FakeTerm(cols=60, rows=10)
        self.run_both(lambda: LiveUI("p", False, term, out_is_tty=False))
        self.assertEqual(self.out.getvalue(), "out one\nout two\n")
        text = term.screen.text()
        for tag in ("one", "two"):
            self.assertEqual(text.count(f"table {tag}"), 1, text)
            self.assertEqual(text.count(f"bar {tag}"), 1, text)

    def test_a_message_left_over_from_an_earlier_invoke_is_not_shown_by_the_next_one(self):
        FakeProc.scripts = [script("one"), []]
        with mock.patch.object(self.session, "_ui", side_effect=lambda _id: PlainUI("p")):
            self.session.invoke(self.manifest, "c", [])
            proc = FakeProc.instances[0]
            proc.hub.inbox.put((proc, "msg", {"method": "output", "params": {"text": "late\n"}}))
            self.session.invoke(self.manifest, "c", [])
        self.assertNotIn("late", self.out.getvalue())
        self.assertEqual(self.out.getvalue(), "out one\n")


if __name__ == "__main__":
    unittest.main()
