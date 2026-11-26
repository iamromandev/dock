"""`dock flow run|check`: approval, saving, parameters and exit codes. A fake store and runner: no Docker."""

import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from src import flow, flow_cli
from src.main import main
import json

from src.inputs import InputSpec
from src.manifest import Command, ManifestError
from src.process import PluginError
from test_flow import GOOD, Recorder, make_store, manifest


class FlowCli(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="dock-flowcli-"))
        self.file = self.dir / "get.flow.toml"
        self.file.write_text(GOOD, encoding="utf-8")
        self.home = self.dir / "home"
        self.runner = Recorder({"exit_code": 0, "outputs": {"files": ["/w/a.zip"]}}, {"exit_code": 0})

    def go(self, args, answers=(), interactive=True, runner="default"):
        answers = list(answers)
        asked = []

        def ask(question):
            asked.append(question)
            return answers.pop(0) if answers else None

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = flow_cli.cmd_flow(make_store(self.home), args, runner=self.runner if runner == "default" else runner,
                                     interactive=interactive, ask=ask, cwd=self.dir)
        return code, out.getvalue(), err.getvalue(), asked

    def run_args(self, *extra):
        return ["run", str(self.file), *extra]

    def test_no_answer_starts_nothing(self):
        for answers in ([], ["n\n"], ["\n"]):
            with self.subTest(answers=answers):
                self.runner.calls.clear()
                code, _, _, asked = self.go(self.run_args("url=http://x"), answers)
                self.assertNotEqual(code, 0)
                self.assertEqual(self.runner.calls, [])
                self.assertEqual(asked, ["Run this flow? [y/N/s] "])

    def test_ctrl_c_at_the_prompt_starts_nothing(self):
        def ask(_):
            raise KeyboardInterrupt

        with contextlib.redirect_stderr(io.StringIO()):
            code = flow_cli.cmd_flow(make_store(), self.run_args("url=u"), runner=self.runner, interactive=True, ask=ask,
                                     cwd=self.dir)
        self.assertNotEqual(code, 0)
        self.assertEqual(self.runner.calls, [])

    def test_yes_answer_runs_and_reports(self):
        code, out, err, _ = self.go(self.run_args("url=http://x"), ["y\n"])
        self.assertEqual(code, 0)
        self.assertEqual(len(self.runner.calls), 2)
        self.assertEqual(self.runner.calls[0][2]["url"], "http://x")
        self.assertIn("flow done (2/2)", err + out)

    def folders(self):
        flows = self.home / "flows"
        return sorted(flows.iterdir()) if flows.exists() else []

    def work_run(self, *extra, answers=None):
        """Run the flow; (code, stderr, the {work} folder the first step was given)."""
        self.runner = Recorder(*(answers or [{"exit_code": 0, "outputs": {"files": ["f"]}}, {"exit_code": 0}]))
        code, _, err, _ = self.go(self.run_args("url=u", "--yes", *extra))
        return code, err, Path(self.runner.calls[0][2]["dir"])

    def test_work_folder_is_under_the_dock_home_private_and_removed_after_success(self):
        seen = []

        def runner(plugin_id, command, inputs):
            if "dir" in inputs:
                seen.append(Path(inputs["dir"]))
                self.assertTrue(seen[0].is_dir())
                self.assertEqual(seen[0].stat().st_mode & 0o777, 0o700)
            return {"exit_code": 0, "outputs": {"files": ["f"]}}

        code, _, err, _ = self.go(self.run_args("url=u", "--yes"), runner=runner)
        self.assertEqual(code, 0)
        self.assertEqual(seen[0].parent, self.home / "flows")
        self.assertIn(str(seen[0]), err)
        self.assertFalse(seen[0].exists())

    def test_two_runs_get_different_folders(self):
        _, _, one = self.work_run("--keep")
        _, _, two = self.work_run("--keep")
        self.assertNotEqual(one, two)
        self.assertEqual(self.folders(), sorted([one, two]))

    def test_keep_leaves_the_folder_after_success(self):
        code, err, work = self.work_run("--keep")
        self.assertEqual(code, 0)
        self.assertTrue(work.is_dir())
        self.assertIn(str(work), err)

    def test_a_failed_run_keeps_the_folder_and_says_where(self):
        code, err, work = self.work_run(answers=[{"exit_code": 2}])
        self.assertNotEqual(code, 0)
        self.assertTrue(work.is_dir())
        self.assertIn(f"kept {work}", err)

    def test_an_interrupted_run_keeps_the_folder(self):
        code, _, work = self.work_run(answers=[KeyboardInterrupt()])
        self.assertEqual(code, 130)
        self.assertTrue(work.is_dir())

    def test_the_session_gets_the_work_folder(self):
        made = []

        class FakeSession:
            def __init__(self, store, verbose=False, term=None, work_dir=None):
                made.append(work_dir)

            def invoke_inputs(self, manifest, command, inputs):
                return {"exit_code": 0, "outputs": {"files": ["f"]}}

            def close(self):
                pass

        with mock.patch.object(flow_cli, "Session", FakeSession), mock.patch.object(flow_cli.isolation, "sweep"):
            code, _, err, _ = self.go(self.run_args("url=u", "--yes", "--keep"), runner=None)
        self.assertEqual(code, 0)
        self.assertEqual(len(made), 1)
        self.assertEqual(made[0].parent, self.home / "flows")

    def test_yes_flag_skips_the_prompt(self):
        code, _, _, asked = self.go(self.run_args("url=u", "--yes"))
        self.assertEqual((code, asked), (0, []))

    def test_non_interactive_without_yes_is_refused(self):
        code, _, err, _ = self.go(self.run_args("url=u"), interactive=False)
        self.assertNotEqual(code, 0)
        self.assertIn("--yes", err)
        self.assertEqual(self.runner.calls, [])

    def test_failed_step_exits_non_zero_and_says_where(self):
        self.runner = Recorder({"exit_code": 2})
        code, _, err, _ = self.go(self.run_args("url=u", "--yes"))
        self.assertNotEqual(code, 0)
        self.assertIn("flow failed at step 1: exit code 2", err)
        self.assertEqual(len(self.runner.calls), 1)

    def test_save_writes_toml_that_reloads_then_asks_again(self):
        code, _, err, asked = self.go(self.run_args("url=u"), ["s\n", "n\n"])
        saved = self.dir / "get.flow.toml"
        self.assertNotEqual(code, 0)
        self.assertEqual(len(asked), 2)
        self.assertEqual(self.runner.calls, [])
        self.assertIn("exists", err)  # the source file has that name here: not overwritten
        self.assertEqual(flow.load_toml(saved), flow.load_toml(self.file))

    def test_save_to_a_new_file_reloads_to_the_same_flow(self):
        other = self.dir / "sub"
        other.mkdir()
        src_file = other / "orig.toml"
        src_file.write_text(GOOD, encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        answers = ["s\n", "n\n"]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            flow_cli.cmd_flow(make_store(), ["run", str(src_file), "url=u"], runner=self.runner, interactive=True,
                              ask=lambda q: answers.pop(0), cwd=self.dir)
        saved = self.dir / "orig.flow.toml"
        self.assertTrue(saved.is_file())
        self.assertEqual(flow.load_toml(saved), flow.load_toml(src_file))

    def test_save_does_not_overwrite_an_existing_file(self):
        (self.dir / "orig.flow.toml").write_text("keep", encoding="utf-8")
        src_file = self.dir / "orig.toml"
        src_file.write_text(GOOD, encoding="utf-8")
        err = io.StringIO()
        answers = ["s\n", "n\n"]
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            flow_cli.cmd_flow(make_store(), ["run", str(src_file), "url=u"], runner=self.runner, interactive=True,
                              ask=lambda q: answers.pop(0), cwd=self.dir)
        self.assertEqual((self.dir / "orig.flow.toml").read_text(encoding="utf-8"), "keep")
        self.assertIn("exists", err.getvalue())

    def test_missing_required_param_is_a_usage_error_before_anything_starts(self):
        code, _, err, asked = self.go(self.run_args("--yes"))
        self.assertEqual(code, 2)
        self.assertIn("url", err)
        self.assertEqual((self.runner.calls, asked), ([], []))

    def test_bad_params_are_usage_errors(self):
        for extra in (["url=u", "nope=1"], ["url"], ["url=u", "url=v"]):
            with self.subTest(extra=extra):
                code, _, err, _ = self.go(self.run_args(*extra, "--yes"))
                self.assertEqual(code, 2)
                self.assertEqual(self.runner.calls, [])

    def test_unset_optional_param_is_reported_before_anything_starts(self):
        f = self.dir / "opt.toml"
        f.write_text('[params.u]\ntype="string"\n[[steps]]\nplugin="dev.dock.dm"\ncommand="dm"\n'
                     'inputs={url="x"}\n[[steps]]\nplugin="dev.dock.dm"\ncommand="dm"\ninputs={url="{params.u}"}\n',
                     encoding="utf-8")
        code, _, err, asked = self.go(["run", str(f), "--yes"])
        self.assertNotEqual(code, 0)
        self.assertIn("step 2:", err)
        self.assertNotIn("flow failed", err)
        self.assertIn("params.u", err)
        self.assertEqual((self.runner.calls, asked), ([], []))

    def test_save_permission_error_reasks_without_traceback(self):
        src_file = self.dir / "orig.toml"
        src_file.write_text(GOOD, encoding="utf-8")
        answers = ["s\n", "n\n"]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch("builtins.open", side_effect=PermissionError(13, "denied")):
            code = flow_cli.cmd_flow(make_store(), ["run", str(src_file), "url=u"], runner=self.runner, interactive=True,
                                     ask=lambda q: answers.pop(0), cwd=self.dir)
        self.assertNotEqual(code, 0)
        self.assertEqual(answers, [])
        self.assertIn("denied", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())

    def test_save_does_not_follow_a_dangling_symlink(self):
        src_file = self.dir / "orig.toml"
        src_file.write_text(GOOD, encoding="utf-8")
        dest = self.dir / "victim.txt"
        (self.dir / "orig.flow.toml").symlink_to(dest)
        answers = ["s\n", "n\n"]
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            flow_cli.cmd_flow(make_store(), ["run", str(src_file), "url=u"], runner=self.runner, interactive=True,
                              ask=lambda q: answers.pop(0), cwd=self.dir)
        self.assertFalse(dest.exists())

    def test_save_with_unencodable_text_reasks(self):
        f = flow.Flow({}, (flow.Step("p", "c", {"a": "\ud800"}),))
        answers = ["s\n", "n\n"]
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            ok = flow_cli._confirm(f, self.dir / "x.toml", self.dir, False, True, lambda q: answers.pop(0))
        self.assertFalse(ok)
        self.assertEqual(answers, [])
        self.assertNotIn("Traceback", err.getvalue())

    def test_interrupted_step_says_so_and_exits_130(self):
        for runner in (Recorder({"exit_code": 130}), Recorder(KeyboardInterrupt())):
            with self.subTest(runner=runner.__class__):
                code, _, err, _ = self.go(self.run_args("url=u", "--yes"), runner=runner)
                self.assertEqual(code, 130)
                self.assertIn("flow interrupted at step 1", err)
                self.assertNotIn("flow failed", err)

    def test_check_errors_are_printed_with_step_numbers_and_nothing_starts(self):
        bad = self.dir / "bad.toml"
        bad.write_text('[[steps]]\nplugin="dev.dock.nope"\ncommand="x"\n[[steps]]\nplugin="dev.dock.dm"\ncommand="dm"\n',
                       encoding="utf-8")
        code, _, err, asked = self.go(["run", str(bad), "--yes"])
        self.assertNotEqual(code, 0)
        self.assertIn("step 1:", err)
        self.assertIn("step 2:", err)
        self.assertEqual((self.runner.calls, asked), ([], []))

    def test_check_ok_and_failing(self):
        code, out, _, _ = self.go(["check", str(self.file)])
        self.assertEqual(code, 0)
        self.assertIn("1. dev.dock.dm dm (download)", out)
        bad = self.dir / "bad.toml"
        bad.write_text('[[steps]]\nplugin="dev.dock.nope"\ncommand="x"\n', encoding="utf-8")
        code, _, err, _ = self.go(["check", str(bad)])
        self.assertNotEqual(code, 0)
        self.assertIn("step 1:", err)

    def test_unreadable_or_invalid_file_is_an_error(self):
        code, _, err, _ = self.go(["check", str(self.dir / "missing.toml")])
        self.assertNotEqual(code, 0)
        self.assertTrue(err)

    def test_hostile_text_never_reaches_the_output(self):
        f = self.dir / "evil.toml"
        f.write_text('[[steps]]\nplugin="dev.dock.dm"\ncommand="dm"\nlabel="\\u001b[31mx"\n'
                     'inputs={url="\\u001b]0;t\\u0007"}\n', encoding="utf-8")
        _, out, err, _ = self.go(["run", str(f), "--yes"])
        self.assertNotIn("\x1b", out + err)
        self.assertNotIn("\x07", out + err)

    def test_usage_for_bad_subcommand(self):
        self.assertEqual(self.go(["bogus"])[0], 2)
        self.assertEqual(self.go([])[0], 2)

    def test_main_dispatches_flow_and_help_lists_it(self):
        with mock.patch.dict(os.environ, {"DOCK_HOME": str(self.dir / "home")}):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                main(["help"])
            self.assertIn("flow run", out.getvalue())
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(["flow"]), 2)


PLANNED = {"params": {}, "steps": [{"plugin": "dev.dock.dm", "command": "dm", "inputs": {"url": "http://x", "dir": "{work}"}},
                                   {"plugin": "dev.dock.unzip", "command": "unzip", "inputs": {"file": "{steps.1.files[0]}"}}]}


class Do(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="dock-do-"))
        self.home = self.dir / "home"
        self.store = make_store(self.home)
        plan = Command("plan", "", [InputSpec("prompt", "string", True), InputSpec("catalog", "string", True)])
        self.store.by_id["dev.dock.planner"] = manifest("dev.dock.planner", [plan], ["network"])

    def go(self, args, planned, answers=(), steps=(), installed=True):
        """`planned`: the planner's result dict, or an exception; the steps' answers follow."""
        if not installed:
            del self.store.by_id["dev.dock.planner"]
        self.runner = Recorder(planned, *steps)
        answers = list(answers)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = flow_cli.cmd_do(self.store, args, runner=self.runner, interactive=True,
                                   ask=lambda q: answers.pop(0) if answers else None, cwd=self.dir)
        return code, out.getvalue(), err.getvalue()

    def steps_run(self):
        return [c[:2] for c in self.runner.calls[1:]]

    def folders(self):
        return list((self.home / "flows").glob("*"))

    def test_planned_flow_is_shown_and_runs_after_yes(self):
        code, _, err = self.go(["get it"], {"exit_code": 0, "flow": PLANNED}, ["y"],
                               [{"exit_code": 0, "outputs": {"files": ["/w/a"]}}, {"exit_code": 0}])
        self.assertEqual(code, 0)
        self.assertIn("dev.dock.unzip", err)
        self.assertEqual(self.steps_run(), [("dev.dock.dm", "dm"), ("dev.dock.unzip", "unzip")])
        plugin, command, inputs = self.runner.calls[0]
        self.assertEqual((plugin, command, inputs["prompt"]), ("dev.dock.planner", "plan", "get it"))
        self.assertEqual(self.folders(), [])  # cleaned up like flow run

    def test_the_catalog_is_in_the_work_folder_the_planner_is_given(self):
        seen = []

        def planner(plugin, command, inputs):
            seen.append(json.loads(Path(inputs["catalog"]).read_text(encoding="utf-8")))
            return {"exit_code": 1}

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            flow_cli.cmd_do(self.store, ["x"], runner=planner, interactive=True, ask=lambda q: None, cwd=self.dir)
        self.assertEqual(sorted(p["id"] for p in seen[0]["plugins"]), ["dev.dock.dm", "dev.dock.unzip"])

    def test_no_answer_starts_no_step(self):
        code, _, _ = self.go(["get it"], {"exit_code": 0, "flow": PLANNED}, ["n"])
        self.assertEqual(code, 1)
        self.assertEqual(self.steps_run(), [])

    def test_save_writes_plan_flow_toml_in_the_cwd(self):
        self.go(["get it"], {"exit_code": 0, "flow": PLANNED}, ["s", "n"])
        self.assertEqual(flow.load_toml(self.dir / "plan.flow.toml"), flow.from_json(PLANNED))

    def test_a_planner_that_gives_no_usable_flow_starts_nothing_and_leaves_no_folder(self):
        bad = {"steps": [{"plugin": "dev.dock.dm", "command": "dm", "bogus": 1}]}
        cases = {
            "exit 1": {"exit_code": 1},
            "missing flow": {"exit_code": 0},
            "malformed": {"exit_code": 0, "flow": bad},
            "17 steps": {"exit_code": 0, "flow": {"steps": [PLANNED["steps"][0]] * 17}},
            "required param": {"exit_code": 0, "flow": {**PLANNED, "params": {"u": {"type": "string", "required": True}}}},
            "unknown plugin": {"exit_code": 0, "flow": {"steps": [{"plugin": "no.such", "command": "x"}]}},
            "plugin error": PluginError("planner crashed"),
            "manifest error": ManifestError("no container runtime"),
            "planner as a step": {"exit_code": 0, "flow": {"steps": [{"plugin": "dev.dock.planner", "command": "plan",
                                  "inputs": {"prompt": "x", "catalog": "y"}}]}},
        }
        for name, planned in cases.items():
            with self.subTest(name):
                code, _, err = self.go(["x"], planned, ["y"])
                self.assertEqual(code, 1)
                self.assertIn("planner could not make a flow", err)
                self.assertEqual(self.steps_run(), [])
        self.assertEqual(self.folders(), [])  # only catalog.json was in it

    def test_the_planner_as_a_step_is_refused_with_its_reason(self):
        planned = {"exit_code": 0, "flow": {"steps": [{"plugin": "dev.dock.planner", "command": "plan",
                                                       "inputs": {"prompt": "x", "catalog": "y"}}]}}
        _, _, err = self.go(["x"], planned, ["y"])
        self.assertIn("planner could not make a flow: a step may not use the planner", err)

    def test_a_manifest_error_says_why_the_planner_could_not_run(self):
        _, _, err = self.go(["x"], ManifestError("no container runtime"))
        self.assertIn("planner could not make a flow: no container runtime", err)

    def test_the_planner_is_stopped_before_the_first_step_and_after_a_failed_plan(self):
        for planned in ({"exit_code": 0, "flow": PLANNED}, {"exit_code": 1}):
            with self.subTest(planned=planned):
                events = []

                class FakeSession:
                    def stop_plugin(self, pid):
                        events.append(("stop", pid))

                    def close(self):
                        events.append(("close",))

                def runner(plugin, command, inputs):
                    events.append(("run", plugin))
                    return planned if plugin == "dev.dock.planner" else {"exit_code": 0, "outputs": {"files": ["/w/a"]}}

                with mock.patch.object(flow_cli, "_open", return_value=(runner, FakeSession())), \
                        contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    flow_cli.cmd_do(self.store, ["x"], interactive=True, ask=lambda q: "y", cwd=self.dir)
                self.assertEqual(events[:2], [("run", "dev.dock.planner"), ("stop", "dev.dock.planner")])

    def test_a_declined_plan_removes_the_work_folder_but_a_failed_step_keeps_it(self):
        self.go(["get it"], {"exit_code": 0, "flow": PLANNED}, ["n"])
        self.assertEqual(self.folders(), [])
        self.go(["get it"], {"exit_code": 0, "flow": PLANNED}, ["y"], [{"exit_code": 1}])
        self.assertEqual(len(self.folders()), 1)

    def test_a_prompt_starting_with_a_dash_is_accepted_after_double_dash(self):
        code, _, _ = self.go(["--", "-5 degrees"], {"exit_code": 0, "flow": PLANNED}, ["n"])
        self.assertEqual(self.runner.calls[0][2]["prompt"], "-5 degrees")
        self.assertEqual(code, 1)  # declined, not a usage error

    def test_planner_not_installed_says_how_to_install_it(self):
        code, _, err = self.go(["x"], {"exit_code": 0}, installed=False)
        self.assertNotEqual(code, 0)
        self.assertIn("dock plugin install plugins/planner", err)
        self.assertEqual(self.runner.calls, [])

    def test_help_lists_do(self):
        with mock.patch.dict(os.environ, {"DOCK_HOME": str(self.home)}), contextlib.redirect_stdout(io.StringIO()) as out:
            main(["help"])
        self.assertIn("do [--yes] [--keep]", out.getvalue())

    def test_usage(self):
        self.assertEqual(self.go([], {})[0], 2)
        self.assertEqual(self.go(["a", "b"], {})[0], 2)
        self.assertEqual(self.go(["--bogus", "a"], {})[0], 2)


class WriteCatalog(unittest.TestCase):
    def test_shape_and_planner_excluded(self):
        home = Path(tempfile.mkdtemp(prefix="dock-cat-"))
        store = make_store(home)
        plan = Command("plan", "", [InputSpec("prompt", "string", True)])
        store.by_id["dev.dock.planner"] = manifest("dev.dock.planner", [plan], ["network"])
        path = flow_cli.write_catalog(store, home)
        self.assertEqual(path, home / "catalog.json")
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual([p["id"] for p in data["plugins"]], ["dev.dock.dm", "dev.dock.unzip"])
        dm = data["plugins"][0]
        self.assertEqual(dm["permissions"], ["network"])
        self.assertEqual(dm["commands"][0], {"name": "dm", "summary": "", "inputs": [
            {"name": "url", "type": "string", "required": True}, {"name": "dir", "type": "string", "required": False}]})
        self.assertIsNone(dm["commands"][1]["inputs"])
        self.assertEqual(set(dm), {"id", "description", "permissions", "commands"})


if __name__ == "__main__":
    unittest.main()
