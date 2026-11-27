"""`dock flow run|check`: approval, saving, parameters and exit codes. A fake store and runner: no Docker."""

import contextlib
import dataclasses
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

from src import catalog, flow, flow_cli, paths
from src.main import main

from src.inputs import InputSpec
from src.manifest import Command, Manifest, ManifestError
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
            code = flow_cli.cmd_flow(
                make_store(self.home),
                args,
                runner=self.runner if runner == "default" else runner,
                interactive=interactive,
                ask=ask,
                cwd=self.dir,
            )
        return code, out.getvalue(), err.getvalue(), asked

    def run_args(self, *extra):
        return ["run", str(self.file), *extra]

    def test_flow_check_names_the_out_folder_in_the_permissions(self):
        store = make_store(self.home)
        store.by_id["dev.dock.dm"] = dataclasses.replace(
            store.by_id["dev.dock.dm"], permissions=["fs:write:cwd"]
        )
        store.dirs = paths.Dirs(None, Path.home() / "o", "--out", "--out")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            flow_cli.cmd_flow(store, ["check", str(self.file)], cwd=self.dir)
        self.assertIn("fs:write:cwd → ~/o", out.getvalue())

    def test_in_ignored_is_said_once_when_no_step_reads_it(self):
        for perms, expect in ((["network"], 1), (["fs:read:in"], 0)):
            with self.subTest(perms=perms):
                store = make_store(self.home)
                store.by_id["dev.dock.dm"] = dataclasses.replace(
                    store.by_id["dev.dock.dm"], permissions=perms
                )
                store.dirs = paths.Dirs(Path("/i"), Path("/o"), "--in", "--out")
                self.runner = Recorder({"exit_code": 0, "outputs": {"files": ["/w/a.zip"]}}, {"exit_code": 0})
                err = io.StringIO()
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                    flow_cli.cmd_flow(
                        store,
                        self.run_args("url=u", "--yes"),
                        runner=self.runner,
                        interactive=False,
                        cwd=self.dir,
                    )
                self.assertEqual(err.getvalue().count("--in ignored"), expect)

    def folders(self):
        flows = self.home / "flows"
        return sorted(flows.iterdir()) if flows.exists() else []

    def work_run(self, *extra, answers=None):
        """Run the flow; (code, stderr, the {work} folder the first step was given)."""
        self.runner = Recorder(
            *(answers or [{"exit_code": 0, "outputs": {"files": ["f"]}}, {"exit_code": 0}])
        )
        code, _, err, _ = self.go(self.run_args("url=u", "--yes", *extra))
        return code, err, Path(self.runner.calls[0][2]["dir"])

    def test_the_session_gets_the_work_folder(self):
        made = []

        class FakeSession:
            def __init__(self, store, ui_factory, *, verbose=False, work_dir=None):
                made.append(work_dir)

            cancelled = False

            def begin(self):
                pass

            def invoke_inputs(self, manifest, command, inputs):
                return {"exit_code": 0, "outputs": {"files": ["f"]}}

            def close(self):
                pass

        with (
            mock.patch.object(flow_cli, "Session", FakeSession),
            mock.patch.object(flow_cli.isolation, "sweep"),
        ):
            code, _, err, _ = self.go(self.run_args("url=u", "--yes", "--keep"), runner=None)
        self.assertEqual(code, 0)
        self.assertEqual(len(made), 1)
        self.assertEqual(made[0].parent, self.home / "flows")

    def test_bad_params_are_usage_errors(self):
        for extra in (["url=u", "nope=1"], ["url"], ["url=u", "url=v"]):
            with self.subTest(extra=extra):
                code, _, err, _ = self.go(self.run_args(*extra, "--yes"))
                self.assertEqual(code, 2)
                self.assertEqual(self.runner.calls, [])

    def test_unset_optional_param_is_reported_before_anything_starts(self):
        f = self.dir / "opt.toml"
        f.write_text(
            '[params.u]\ntype="string"\n[[steps]]\nplugin="dev.dock.dm"\ncommand="dm"\n'
            'inputs={url="x"}\n[[steps]]\nplugin="dev.dock.dm"\ncommand="dm"\ninputs={url="{params.u}"}\n',
            encoding="utf-8",
        )
        code, _, err, asked = self.go(["run", str(f), "--yes"])
        self.assertNotEqual(code, 0)
        self.assertIn("step 2:", err)
        self.assertNotIn("flow failed", err)
        self.assertIn("params.u", err)
        self.assertEqual((self.runner.calls, asked), ([], []))

    def test_save_does_not_follow_a_dangling_symlink(self):
        src_file = self.dir / "orig.toml"
        src_file.write_text(GOOD, encoding="utf-8")
        dest = self.dir / "victim.txt"
        (self.dir / "orig.flow.toml").symlink_to(dest)
        answers = ["s\n", "n\n"]
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            flow_cli.cmd_flow(
                make_store(),
                ["run", str(src_file), "url=u"],
                runner=self.runner,
                interactive=True,
                ask=lambda q: answers.pop(0),
                cwd=self.dir,
            )
        self.assertFalse(dest.exists())

    def test_interrupted_step_says_so_and_exits_130(self):
        for runner in (Recorder({"exit_code": 130}), Recorder(KeyboardInterrupt())):
            with self.subTest(runner=runner.__class__):
                code, _, err, _ = self.go(self.run_args("url=u", "--yes"), runner=runner)
                self.assertEqual(code, 130)
                self.assertIn("flow interrupted at step 1", err)
                self.assertNotIn("flow failed", err)
        with self.subTest("cancel between steps"):  # bug: a cancel() between two steps is lost and the rest runs
            f = flow_cli._load(self.file)
            cancelled, calls = [], []

            def step_one_then_cancel(plugin_id, command, inputs):
                calls.append(command)
                cancelled.append(True)  # Session.cancel() arrives as step 1 returns
                return {"exit_code": 0, "outputs": {"files": ["/w/a.zip"]}}

            def run(runner, **kw):
                return flow.run(f, {"url": "u"}, runner, self.dir, lambda line: None, **kw)

            got = run(step_one_then_cancel, cancelled=lambda: bool(cancelled))
            ctrl_c = run(Recorder({"exit_code": 0, "outputs": {"files": ["/w/a.zip"]}}, KeyboardInterrupt()))
            self.assertEqual(calls, ["dm"])  # the second step never ran
            self.assertEqual(got, ctrl_c)
            self.assertTrue(got.interrupted)

    def test_unreadable_or_invalid_file_is_an_error(self):
        code, _, err, _ = self.go(["check", str(self.dir / "missing.toml")])
        self.assertNotEqual(code, 0)
        self.assertTrue(err)

    def test_main_dispatches_flow_and_help_lists_it(self):
        with mock.patch.dict(os.environ, {"DOCK_HOME": str(self.dir / "home")}):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                main(["help"])
            self.assertIn("flow run", out.getvalue())
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(["flow"]), 2)


PLANNED = {
    "params": {},
    "steps": [
        {"plugin": "dev.dock.dm", "command": "dm", "inputs": {"url": "http://x", "dir": "{work}"}},
        {"plugin": "dev.dock.unzip", "command": "unzip", "inputs": {"file": "{steps.1.files[0]}"}},
    ],
}


class Do(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="dock-do-"))
        self.home = self.dir / "home"
        self.store = make_store(self.home)
        plan = Command(
            "plan", "", [InputSpec("prompt", "string", True), InputSpec("catalog", "string", True)]
        )
        self.store.by_id["dev.dock.brain"] = manifest("dev.dock.brain", [plan], ["network"])
        patcher = mock.patch.object(
            catalog, "load", return_value=[]
        )  # these tests plan with installed plugins only
        patcher.start()
        self.addCleanup(patcher.stop)

    def go(self, args, planned, answers=(), steps=(), installed=True):
        """`planned`: the brain's result dict, or an exception; the steps' answers follow."""
        if not installed:
            del self.store.by_id["dev.dock.brain"]
        self.runner = Recorder(planned, *steps)
        answers = list(answers)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = flow_cli.cmd_do(
                self.store,
                args,
                runner=self.runner,
                interactive=True,
                ask=lambda q: answers.pop(0) if answers else None,
                cwd=self.dir,
            )
        return code, out.getvalue(), err.getvalue()

    def steps_run(self):
        return [c[:2] for c in self.runner.calls[1:]]

    def folders(self):
        return list((self.home / "flows").glob("*"))

    def test_a_brain_that_gives_no_usable_flow_starts_nothing_and_leaves_no_folder(self):
        bad = {"steps": [{"plugin": "dev.dock.dm", "command": "dm", "bogus": 1}]}
        cases = {
            "exit 1": {"exit_code": 1},
            "missing flow": {"exit_code": 0},
            "malformed": {"exit_code": 0, "flow": bad},
            "17 steps": {"exit_code": 0, "flow": {"steps": [PLANNED["steps"][0]] * 17}},
            "required param": {
                "exit_code": 0,
                "flow": {**PLANNED, "params": {"u": {"type": "string", "required": True}}},
            },
            "unknown plugin": {"exit_code": 0, "flow": {"steps": [{"plugin": "no.such", "command": "x"}]}},
            "plugin error": PluginError("brain crashed"),
            "manifest error": ManifestError("no container runtime"),
            "brain as a step": {
                "exit_code": 0,
                "flow": {
                    "steps": [
                        {
                            "plugin": "dev.dock.brain",
                            "command": "plan",
                            "inputs": {"prompt": "x", "catalog": "y"},
                        }
                    ]
                },
            },
        }
        for name, planned in cases.items():
            with self.subTest(name):
                code, _, err = self.go(["x"], planned, ["y"])
                self.assertEqual(code, 1)
                self.assertIn("brain could not make a flow", err)
                self.assertEqual(self.steps_run(), [])
        self.assertEqual(self.folders(), [])  # only catalog.json was in it

    def test_usage(self):
        self.assertEqual(self.go([], {})[0], 2)
        self.assertEqual(self.go(["a", "b"], {})[0], 2)
        self.assertEqual(self.go(["--bogus", "a"], {})[0], 2)


def write_dm_folder(root: Path, version: str = "1.0") -> Path:
    """A real plugin folder for dev.dock.dm asking for network and fs:write:cwd."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "plugin.toml").write_text(
        f'id = "dev.dock.dm"\nversion = "{version}"\napi_version = "2"\ndescription = "dm"\n\n'
        '[run]\ncontainer = { cmd = "python3", args = ["/plugin/main.py"] }\n\n'
        '[capabilities]\npermissions = ["network", "fs:write:cwd"]\n\n'
        '[[commands]]\nname = "dm"\nsummary = "Download"\n'
        'inputs = [{ name = "url", type = "string", required = true }, { name = "dir", type = "string" }]\n',
        encoding="utf-8",
    )
    (root / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    return root


DM_STEP = {"plugin": "dev.dock.dm", "command": "dm", "inputs": {"url": "http://x"}}
DM_PLAN = {"params": {}, "steps": [DM_STEP]}


class Available(unittest.TestCase):
    """dock do with a plugin that is in the catalog but not installed (dev.dock.dm here)."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="dock-avail-"))
        self.home = self.dir / "home"
        self.store = make_store(self.home)
        self.store.by_id.pop("dev.dock.dm")
        self.src = write_dm_folder(self.dir / "catalog" / "dm")
        self.dm = Manifest.load(self.src)
        self.store.by_id["dev.dock.brain"] = manifest(
            "dev.dock.brain",
            [
                Command(
                    "plan", "", [InputSpec("prompt", "string", True), InputSpec("catalog", "string", True)]
                )
            ],
            ["network"],
        )
        self.installs = []

        def install(folder):
            m = Manifest.load(folder)  # read while the staged copy still exists
            self.installs.append((Path(folder), m))
            self.store.by_id[m.id] = m
            return m

        self.store.install = install
        self.catalog = [
            catalog.Available(self.dm, self.src),
            catalog.Available(manifest("dev.dock.brain", []), Path("/repo/plugins/brain")),
        ]
        patcher = mock.patch.object(catalog, "load", side_effect=lambda store: self.catalog)
        patcher.start()
        self.addCleanup(patcher.stop)

    def go(self, args, planned=None, answers=(), interactive=True, steps=({"exit_code": 0},)):
        self.runner = Recorder({"exit_code": 0, "flow": DM_PLAN} if planned is None else planned, *steps)
        answers = list(answers)
        self.asked = []

        def ask(q):
            self.asked.append(q)
            return answers.pop(0) if answers else None

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = flow_cli.cmd_do(
                self.store, args, runner=self.runner, interactive=interactive, ask=ask, cwd=self.dir
            )
        return code, out.getvalue(), err.getvalue()

    def steps_run(self):
        return [c[:2] for c in self.runner.calls[1:]]

    def test_refusals_say_not_installed_and_hint_without_rerun_with_yes_advice(self):
        for name, args, interactive in (("--yes", ["--yes", "get it"], True), ("non-tty", ["get it"], False)):
            with self.subTest(name):  # catches: promising an install that will not happen
                code, _, err = self.go(args, answers=["y"], interactive=interactive)
                self.assertEqual(code, 1)
                self.assertIn("Not installed:\n  dev.dock.dm 1.0", err)
                self.assertNotIn("installed if you answer y", err)
                self.assertNotIn("rerun with --yes", err)
                self.assertIn(
                    "dock: do: dev.dock.dm is not installed; run: dock plugin install dev.dock.dm", err
                )
                self.assertEqual((self.installs, self.steps_run()), ([], []))

    def test_a_folder_that_changed_id_after_loading_is_refused_and_nothing_installs(
        self,
    ):  # catches: trusting the scan
        text = (self.src / "plugin.toml").read_text(encoding="utf-8")
        (self.src / "plugin.toml").write_text(text.replace("dev.dock.dm", "dev.evil.x"), encoding="utf-8")
        code, _, err = self.go(["get it"], answers=["y"])
        self.assertEqual(code, 1)
        self.assertIn("dev.evil.x", err)
        self.assertEqual((self.installs, self.steps_run(), self.asked), ([], [], []))

    def test_a_plugin_id_in_several_catalog_folders_warns_once_and_uses_the_first(
        self,
    ):  # catches: silent shadowing
        other = write_dm_folder(self.dir / "other" / "dm", "2.0")
        self.catalog.insert(1, catalog.Available(Manifest.load(other), other))
        _, _, err = self.go(["get it"], answers=["n"])
        self.assertEqual(err.count("dev.dock.dm is in several catalog folders"), 1)
        self.assertIn(f"using {self.src}", err)

    def test_install_failure_runs_no_step_and_exits_1(self):  # catches: running after a failed install
        def boom(folder):
            raise ManifestError("disk full")

        self.store.install = boom
        code, _, err = self.go(["get it"], answers=["y"])
        self.assertEqual(code, 1)
        self.assertIn("dock: do: cannot install dev.dock.dm: disk full", err)
        self.assertEqual(self.steps_run(), [])


BRAIN_TOML = (
    'id = "dev.dock.brain"\nversion = "0.1.0"\napi_version = "2"\ndescription = "b"\n\n'
    '[run]\ncontainer = { cmd = "python3", args = ["/plugin/main.py"] }\n\n'
    '[[commands]]\nname = "plan"\nsummary = "plan"\n'
    'inputs = [{ name = "prompt", type = "string", required = true }, { name = "catalog", type = "string", required = true }]\n'
)


class RealStoreInstall(unittest.TestCase):
    """dock do with a real Store, real staging and a real plugin folder copied from plugins/dm."""

    def setUp(self):
        from src.store import Store

        self.dir = Path(tempfile.mkdtemp(prefix="dock-real-")).resolve()
        self.addCleanup(__import__("shutil").rmtree, self.dir, ignore_errors=True)
        self.home = self.dir / "home"
        self.store = Store(self.home)
        brain = self.dir / "brain"
        brain.mkdir()
        (brain / "plugin.toml").write_text(BRAIN_TOML, encoding="utf-8")
        (brain / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
        self.store.install(brain)
        self.src = self.dir / "cat" / "dm"
        __import__("shutil").copytree(
            ROOT.parent.parent / "plugins" / "dm",
            self.src,
            ignore=__import__("shutil").ignore_patterns(".venv", "__pycache__", "coverage*", ".git"),
        )
        (self.home / "config.toml").write_text(
            f'[catalog]\npaths = ["{self.dir / "cat"}"]\n', encoding="utf-8"
        )
        patcher = mock.patch.object(catalog, "builtin_dir", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def go(self, args, ask, steps=({"exit_code": 0},)):
        self.runner = Recorder({"exit_code": 0, "flow": DM_PLAN}, *steps)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = flow_cli.cmd_do(
                self.store, args, runner=self.runner, interactive=True, ask=ask, cwd=self.dir
            )
        return code, err.getvalue()

    def test_y_installs_the_plugin_and_the_step_runs(self):  # catches: install skipped or run before install
        code, err = self.go(["get it"], lambda q: "y")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.store.get("dev.dock.dm").id, "dev.dock.dm")
        self.assertEqual([c[:2] for c in self.runner.calls[1:]], [("dev.dock.dm", "dm")])


if __name__ == "__main__":
    unittest.main()
