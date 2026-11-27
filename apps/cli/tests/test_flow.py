"""Flow model: loading, static check, render and the runner with a fake StepRunner. No Docker needed."""

import dataclasses
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import flow, paths
from src.flow import Flow, FlowError, Step
from src.inputs import InputSpec
from src.manifest import Command, ContainerRun, Manifest, ManifestError
from src.process import PluginError

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
    return Manifest(pid, "1.0", "2", "", Path("/x"), ContainerRun("run"), list(commands), list(permissions), list(env))


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
    return FakeStore(manifest("dev.dock.dm", [dm, free], ["network"], ["TOKEN"]), manifest("dev.dock.unzip", [unzip]), home=home)


def write(text):
    d = tempfile.mkdtemp()
    p = Path(d) / "f.flow.toml"
    p.write_text(text, encoding="utf-8")
    return p


class LoadTests(unittest.TestCase):
    def test_good_toml_loads(self):
        f = flow.load_toml(write(GOOD))
        self.assertEqual(f.params, {"url": {"type": "string", "required": True}})
        self.assertEqual(len(f.steps), 2)
        self.assertEqual(f.steps[0], Step("dev.dock.dm", "dm", {"url": "{params.url}", "dir": "{work}"}, "download"))
        self.assertIsNone(f.steps[1].label)

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

    def test_16_steps_load(self):
        self.assertEqual(len(flow.from_json({"steps": [{"plugin": "p", "command": "c"}] * 16}).steps), 16)

    def test_17_steps_rejected_from_toml(self):
        with self.assertRaises(FlowError):
            flow.load_toml(write('[[steps]]\nplugin="p"\ncommand="c"\n' * 17))

    def test_bad_toml_syntax_is_a_flow_error(self):
        with self.assertRaises(FlowError):
            flow.load_toml(write("[[steps"))

    def test_to_toml_round_trips(self):
        f = flow.load_toml(write(GOOD))
        self.assertEqual(flow.load_toml(write(flow.to_toml(f))), f)

    def test_to_toml_round_trips_odd_text(self):
        f = Flow({}, (Step("p", "c", {"a": 'q"\\\n\x7f é 😀', "n": 1, "b": True}, "l\tx"),))
        self.assertEqual(flow.load_toml(write(flow.to_toml(f))), f)


class CheckTests(unittest.TestCase):
    def errors(self, steps, params=None):
        return flow.check(Flow(params or {}, tuple(steps)), make_store())

    def test_good_flow_has_no_errors(self):
        self.assertEqual(flow.check(flow.load_toml(write(GOOD)), make_store()), [])

    def test_all_errors_are_collected_with_step_numbers(self):
        errs = self.errors([
            Step("dev.dock.nope", "x", {}),
            Step("dev.dock.dm", "zzz", {}),
            Step("dev.dock.dm", "dm", {}),  # url missing
            Step("dev.dock.unzip", "unzip", {"file": "f", "count": "many"}),  # wrong literal type
            Step("dev.dock.unzip", "unzip", {"file": "{steps.6.files}"}),  # forward
            Step("dev.dock.unzip", "unzip", {"file": "{steps.6.files}"}),  # self
            Step("dev.dock.unzip", "unzip", {"file": "{params.nope}"}),
            Step("dev.dock.unzip", "unzip", {"file": "f", "bogus": "1"}),
        ])
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

    def test_reference_valued_key_counts_as_present_but_is_not_type_checked(self):
        step = Step("dev.dock.unzip", "unzip", {"file": "f", "count": "{params.n}"})
        self.assertEqual(self.errors([step], {"n": {"type": "int", "required": False}}), [])

    def test_forward_reference_to_next_step(self):
        steps = [Step("dev.dock.dm", "dm", {"url": "{steps.2.x}"}), Step("dev.dock.dm", "dm", {"url": "u"})]
        errs = self.errors(steps)
        self.assertEqual([e.step for e in errs], [1])

    def test_malformed_reference_is_an_error(self):
        errs = self.errors([Step("dev.dock.dm", "dm", {"url": "{params.}"})])
        self.assertEqual(len(errs), 1)
        self.assertIn("reference", errs[0].reason)

    def test_inputs_on_a_command_without_declared_inputs_are_rejected(self):
        errs = self.errors([Step("dev.dock.dm", "free", {"anything": "x"})])
        self.assertEqual(len(errs), 1)
        self.assertEqual(errs[0].step, 1)
        self.assertIn("takes no inputs", errs[0].reason)

    def test_no_inputs_on_a_command_without_declared_inputs_is_fine(self):
        self.assertEqual(self.errors([Step("dev.dock.dm", "free", {})]), [])

    def test_unset_params_are_reported_with_step_numbers_when_params_given(self):
        f = Flow({"u": {"type": "string", "required": False}},
                 (Step("dev.dock.dm", "dm", {"url": "x"}), Step("dev.dock.dm", "dm", {"url": "a{params.u}"})))
        self.assertEqual(flow.check(f, make_store()), [])
        errs = flow.check(f, make_store(), {})
        self.assertEqual([e.step for e in errs], [2])
        self.assertIn("params.u", errs[0].reason)
        self.assertEqual(flow.check(f, make_store(), {"u": "v"}), [])


class RenderTests(unittest.TestCase):
    def test_lines_and_permissions(self):
        out = flow.render(flow.load_toml(write(GOOD)), make_store())
        self.assertEqual(out[0], "1. dev.dock.dm dm (download) url={params.url} dir={work}")
        self.assertEqual(out[1], "2. dev.dock.unzip unzip file={steps.1.files[0]} count=2")
        text = "\n".join(out)
        self.assertIn("Permissions", text)
        self.assertIn("dev.dock.dm: network, env TOKEN", text)
        self.assertIn("dev.dock.unzip: none", text)

    def test_permissions_show_the_out_folder(self):
        store = make_store()
        store.by_id["dev.dock.dm"] = dataclasses.replace(store.by_id["dev.dock.dm"], permissions=["fs:write:cwd"])
        store.dirs = paths.Dirs(None, Path.home() / "o", "--out", "--out")
        text = "\n".join(flow.render(flow.load_toml(write(GOOD)), store))
        self.assertIn("dev.dock.dm: fs:write:cwd → ~/o, env TOKEN", text)

    def test_hostile_text_is_sanitized(self):
        f = Flow({}, (Step("dev.dock.dm", "dm", {"url": "\x1b[31mred\x07"}, "\x1b]0;x\x07evil"),))
        text = "\n".join(flow.render(f, make_store()))
        for ch in ("\x1b", "\x07"):
            self.assertNotIn(ch, text)

    def test_hostile_plugin_and_unknown_plugin_are_sanitized(self):
        f = Flow({}, (Step("a\x1bb", "c\x1bd", {}),))
        text = "\n".join(flow.render(f, make_store()))
        self.assertNotIn("\x1b", text)


class FillTests(unittest.TestCase):
    CTX = {"params": {"n": 3, "u": "http://x"}, "work": "/w", "steps": {1: {"files": ["/w/a.zip"], "size": 7}}}

    def test_exact_reference_keeps_json_type(self):
        self.assertEqual(flow.fill("{params.n}", self.CTX), 3)
        self.assertEqual(flow.fill("{steps.1.size}", self.CTX), 7)
        self.assertEqual(flow.fill("{steps.1.files}", self.CTX), ["/w/a.zip"])

    def test_reference_in_text_becomes_string(self):
        self.assertEqual(flow.fill("n={params.n} in {work}", self.CTX), "n=3 in /w")

    def test_index_and_literals(self):
        self.assertEqual(flow.fill("{steps.1.files[0]}", self.CTX), "/w/a.zip")
        self.assertEqual(flow.fill(5, self.CTX), 5)
        self.assertEqual(flow.fill({"a": ["{work}"]}, self.CTX), {"a": ["/w"]})

    def test_missing_field_and_index_name_the_field(self):
        for ref in ("{steps.1.nope}", "{steps.1.files[3]}", "{params.zzz}", "{steps.2.x}"):
            with self.assertRaises(FlowError) as cm:
                flow.fill(ref, self.CTX)
            self.assertIn(ref[1:-1], cm.exception.reason)


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


class RunTests(unittest.TestCase):
    def go(self, f, runner, params=None):
        lines = []
        res = flow.run(f, params or {}, runner, Path("/w"), lines.append, make_store())
        return res, lines

    def test_references_are_passed_through(self):
        f = flow.load_toml(write(GOOD))
        runner = Recorder({"exit_code": 0, "outputs": {"files": ["/w/a.zip"]}}, {"exit_code": 0})
        res, lines = self.go(f, runner, {"url": "http://x"})
        self.assertTrue(res.ok)
        self.assertEqual(res.done, 2)
        self.assertEqual(runner.calls[0], ("dev.dock.dm", "dm", {"url": "http://x", "dir": "/w"}))
        self.assertEqual(runner.calls[1], ("dev.dock.unzip", "unzip", {"file": "/w/a.zip", "count": 2}))
        self.assertEqual(lines, ["[1/2] dev.dock.dm dm (download)", "[2/2] dev.dock.unzip unzip"])
        self.assertEqual(res.outputs[0], {"files": ["/w/a.zip"]})

    def test_a_manifest_error_from_the_runner_is_a_failed_step(self):
        f = flow.load_toml(write(GOOD))
        res, _ = self.go(f, Recorder(ManifestError("no container runtime")), {"url": "u"})
        self.assertFalse(res.ok)
        self.assertEqual((res.done, res.failed_step, res.reason), (0, 1, "no container runtime"))

    def test_missing_output_field_fails_before_starting_that_step(self):
        f = flow.load_toml(write(GOOD))
        runner = Recorder({"exit_code": 0})
        res, _ = self.go(f, runner, {"url": "u"})
        self.assertFalse(res.ok)
        self.assertEqual((res.done, res.failed_step), (1, 2))
        self.assertIn("steps.1.files[0]", res.reason)
        self.assertEqual(len(runner.calls), 1)

    def test_non_dict_outputs_become_empty_with_a_warning(self):
        f = Flow({}, (Step("dev.dock.dm", "dm", {"url": "u"}),))
        res, lines = self.go(f, Recorder({"exit_code": 0, "outputs": ["x"]}))
        self.assertTrue(res.ok)
        self.assertEqual(res.outputs, [{}])
        self.assertEqual(len(lines), 2)
        self.assertIn("outputs", lines[1])
        self.assertIn("ignored", lines[1])

    def test_nonzero_exit_stops_later_steps(self):
        f = Flow({}, tuple(Step("dev.dock.dm", "dm", {"url": "u"}) for _ in range(3)))
        runner = Recorder({"exit_code": 2})
        res, _ = self.go(f, runner)
        self.assertEqual((res.ok, res.done, res.failed_step), (False, 0, 1))
        self.assertIn("2", res.reason)
        self.assertEqual(len(runner.calls), 1)

    def test_plugin_error_stops(self):
        f = Flow({}, tuple(Step("dev.dock.dm", "dm", {"url": "u"}) for _ in range(2)))
        runner = Recorder({"exit_code": 0}, PluginError("boom"))
        res, _ = self.go(f, runner)
        self.assertEqual((res.done, res.failed_step), (1, 2))
        self.assertIn("boom", res.reason)

    def test_keyboard_interrupt_stops_the_flow(self):
        f = Flow({}, tuple(Step("dev.dock.dm", "dm", {"url": "u"}) for _ in range(3)))
        runner = Recorder({"exit_code": 0}, KeyboardInterrupt())
        res, _ = self.go(f, runner)
        self.assertEqual((res.ok, res.done, res.failed_step), (False, 1, 2))
        self.assertEqual(len(runner.calls), 2)

    def test_bad_filled_type_fails_before_starting(self):
        f = Flow({"n": {"type": "string", "required": True}},
                 (Step("dev.dock.unzip", "unzip", {"file": "f", "count": "{params.n}"}),))
        runner = Recorder()
        res, _ = self.go(f, runner, {"n": "abc"})
        self.assertEqual(res.failed_step, 1)
        self.assertEqual(runner.calls, [])

    def test_undeclared_inputs_are_passed_as_none(self):
        f = Flow({}, (Step("dev.dock.dm", "free", {}),))
        runner = Recorder()
        res, _ = self.go(f, runner)
        self.assertTrue(res.ok)
        self.assertIsNone(runner.calls[0][2])

    def test_exit_code_130_is_an_interruption(self):
        f = Flow({}, (Step("dev.dock.dm", "dm", {"url": "u"}),))
        res, _ = self.go(f, Recorder({"exit_code": 130}))
        self.assertTrue(res.interrupted)

    def test_keyboard_interrupt_is_an_interruption_and_plain_failure_is_not(self):
        f = Flow({}, (Step("dev.dock.dm", "dm", {"url": "u"}),))
        res, _ = self.go(f, Recorder(KeyboardInterrupt()))
        self.assertTrue(res.interrupted)
        res, _ = self.go(f, Recorder({"exit_code": 2}))
        self.assertFalse(res.interrupted)

    def test_sixteen_fake_steps_are_fast(self):
        f = Flow({}, tuple(Step("dev.dock.dm", "dm", {"url": "u"}) for _ in range(16)))
        t = time.monotonic()
        res, _ = self.go(f, Recorder())
        self.assertTrue(res.ok)
        self.assertEqual(res.done, 16)
        self.assertLess(time.monotonic() - t, 3.2)


if __name__ == "__main__":
    unittest.main()
