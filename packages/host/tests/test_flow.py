"""Flow model: loading, static check, render and the runner with a fake StepRunner. No Docker needed."""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dock_host import flow
from dock_host.flow import Flow, FlowError, Step
from dock_host.inputs import InputSpec
from dock_host.manifest import Command, ContainerRun, Manifest, ManifestError

GOOD = """
[params.url]
type = "string"
required = true

[[steps]]
plugin = "dev.dock.dm"
command = "dm"
inputs = { url = "{params.url}", dir = "{work}" }
label = "download"

[[steps]]
plugin = "dev.dock.unzip"
command = "unzip"
inputs = { file = "{steps.1.files[0]}", count = 2 }
"""


def manifest(pid, commands, permissions=(), env=()):
    return Manifest(
        pid, "1.0", "2", "", Path("/x"), ContainerRun("run"), list(commands), list(permissions), list(env)
    )


class FakeStore:
    def __init__(self, *manifests, home=None):
        self.home = Path(home) if home is not None else Path(tempfile.mkdtemp(prefix="dock-home-"))
        self.by_id = {m.id: m for m in manifests}
        self.dirs = None

    def index(self):
        return {pid: {} for pid in self.by_id}

    def get(self, plugin_id, any_api=False):
        if plugin_id not in self.by_id:
            raise ManifestError(f"{plugin_id} is not installed")
        return self.by_id[plugin_id]


def make_store(home=None):
    dm = Command("dm", "", [InputSpec("url", "string", True), InputSpec("dir", "string")])
    unzip = Command("unzip", "", [InputSpec("file", "string", True), InputSpec("count", "int")])
    free = Command("free")  # inputs undeclared
    return FakeStore(
        manifest("dev.dock.dm", [dm, free], ["network"], ["TOKEN"]),
        manifest("dev.dock.unzip", [unzip]),
        home=home,
    )


def write(text):
    d = tempfile.mkdtemp()
    p = Path(d) / "f.flow.toml"
    p.write_text(text, encoding="utf-8")
    return p


class LoadTests(unittest.TestCase):
    def bad(self, obj, text, step=None):
        with self.assertRaises(FlowError) as cm:
            flow.from_json(obj)
        self.assertIn(text, cm.exception.reason)
        self.assertEqual(cm.exception.step, step)

    def test_rejections(self):
        s = {"plugin": "p", "command": "c"}
        self.bad({"steps": [s], "extra": 1}, "unknown key 'extra'")
        self.bad({"steps": []}, "at least one step")
        self.bad({"steps": [s] * 17}, "at most 16")
        self.bad({"steps": [{**s, "bogus": 1}]}, "unknown key 'bogus'", 1)
        self.bad({"steps": [s, {**s, "inputs": [1]}]}, "inputs must be a table", 2)
        self.bad({"steps": [{"plugin": "p"}]}, "command", 1)
        self.bad({"params": {"a": {"type": "float"}}, "steps": [s]}, "type")
        self.bad({"params": {"a": {"type": "int", "x": 1}}, "steps": [s]}, "unknown key 'x'")


class CheckTests(unittest.TestCase):
    def errors(self, steps, params=None):
        return flow.check(Flow(params or {}, tuple(steps)), make_store())

    def test_all_errors_are_collected_with_step_numbers(self):
        errs = self.errors(
            [
                Step("dev.dock.nope", "x", {}),
                Step("dev.dock.dm", "zzz", {}),
                Step("dev.dock.dm", "dm", {}),  # url missing
                Step("dev.dock.unzip", "unzip", {"file": "f", "count": "many"}),  # wrong literal type
                Step("dev.dock.unzip", "unzip", {"file": "{steps.6.files}"}),  # forward
                Step("dev.dock.unzip", "unzip", {"file": "{steps.6.files}"}),  # self
                Step("dev.dock.unzip", "unzip", {"file": "{params.nope}"}),
                Step("dev.dock.unzip", "unzip", {"file": "f", "bogus": "1"}),
            ]
        )
        got = {(e.step, e.reason) for e in errs}
        steps = sorted(e.step for e in errs)
        self.assertEqual(steps, [1, 2, 3, 4, 5, 6, 7, 8])
        self.assertTrue(any(s == 1 and "not installed" in r for s, r in got))
        self.assertTrue(any(s == 2 and "zzz" in r for s, r in got))
        self.assertTrue(any(s == 3 and "missing required input 'url'" in r for s, r in got))
        self.assertTrue(any(s == 4 and "integer" in r for s, r in got))
        self.assertTrue(any(s == 5 and "step 6" in r for s, r in got))
        self.assertTrue(any(s == 6 and "step 6" in r for s, r in got))
        self.assertTrue(any(s == 7 and "params.nope" in r for s, r in got))
        self.assertTrue(any(s == 8 and "unknown input 'bogus'" in r for s, r in got))


class Recorder:
    """A fake StepRunner: records calls, answers from a script."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, plugin_id, command, inputs):
        self.calls.append((plugin_id, command, inputs))
        a = self.answers.pop(0) if self.answers else {"exit_code": 0}
        if isinstance(a, BaseException):
            raise a
        return a


if __name__ == "__main__":
    unittest.main()
