"""Permission prompts for `dock plugin install` and `dock plugin update`."""

import io
import os
import shutil
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import test_catalog as tc
from dock_host import approve
from src import main, stdio
from dock_host import store as store_mod
from dock_host.manifest import Manifest
from src.shell import Shell

PID = "dev.t.g"


def plugin(parent, name, permissions=None, env=None, pid=PID, version="0.1.0"):
    """A plugin folder declaring the given permissions and env names."""
    root = tc.make_plugin(parent, name, pid, version)
    caps = ""
    if permissions is not None:
        caps += "permissions = [" + ", ".join(f'"{p}"' for p in permissions) + "]\n"
    if env is not None:
        caps += "env = [" + ", ".join(f'"{e}"' for e in env) + "]\n"
    if caps:
        with (root / "plugin.toml").open("a") as f:
            f.write("\n[capabilities]\n" + caps)
    return root


def newer(parent, name, version, permissions=None, env=None, pid=PID):
    return plugin(parent, name, permissions, env, pid, version)


def manifest(tmp, permissions=None, env=None):
    return Manifest.load(plugin(tmp, "m", permissions=permissions, env=env))


class Decide(unittest.TestCase):
    def decide(self, yes=False, interactive=True, answer="y"):
        calls = []

        def ask(q):
            calls.append(q)
            return answer

        err = io.StringIO()
        with redirect_stderr(err):
            result = approve.Approver(yes, interactive, ask).decide("LINE", "Q? ")
        return result, calls, err.getvalue()

    def test_undecodable_answer_is_a_decline(self):
        class Bad:
            def readline(self):
                raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

        with mock.patch("sys.stdin", Bad()), redirect_stderr(io.StringIO()):
            self.assertIsNone(stdio.ask_stdin("Q? "))


class Scripted:
    """An `ask` that returns canned answers and records the questions."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.questions = []

    def __call__(self, question):
        self.questions.append(question)
        if not self.answers:
            raise AssertionError("asked more than expected")
        a = self.answers.pop(0)
        if a is KeyboardInterrupt:
            raise KeyboardInterrupt
        return a


def run(fn, *args, **kw):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = fn(*args, **kw)
    return code, out.getvalue(), err.getvalue()


class InstallPrompt(tc.TmpCase):
    def setUp(self):
        super().setUp()
        self.store()  # Store() creates the empty folders of a new DOCK_HOME; snapshots are taken after that

    def install(self, folder, *flags, ask=None, interactive=True, stop=None):
        return run(
            main.cmd_plugin_install,
            self.store(),
            [*flags, str(folder)],
            stop=stop,
            interactive=interactive,
            ask=ask,
        )

    def test_approve_installs(self):
        ask = Scripted("y")
        code, out, err = self.install(plugin(self.tmp, "p", ["network"], ["A_B"]), ask=ask)
        self.assertEqual(code, 0)
        self.assertEqual(err, f"{PID} 0.1.0 asks for: network, env A_B\n")
        self.assertEqual(ask.questions, ["Install it? [y/N] "])
        self.assertIn(PID, self.store().index())

    def test_unreadable_installed_manifest_counts_all_as_new(self):
        store = self.store()
        store.install(plugin(self.tmp, "old", ["network"]))
        (Path(store.index()[PID]["path"]) / "plugin.toml").unlink()
        ask = Scripted("y")
        code, out, err = self.install(plugin(self.tmp, "new", ["network"]), ask=ask)
        self.assertEqual(code, 0)
        self.assertIn("adds: network", err)


class FakeSession:
    def __init__(self):
        self.stopped = []

    def stop_plugin(self, plugin_id):
        self.stopped.append(plugin_id)

    def close(self):
        pass


class ShellPrompt(tc.CliCase):
    def shell(self, *answers, interactive=True):
        self.session = FakeSession()
        self.ask = Scripted(*answers)
        return Shell(self.store(), self.session, interactive=interactive, ask=self.ask)

    def line(self, shell, text):
        return run(shell.run_line, text)

    def test_update_declined_then_approved(self):
        self.store().install(plugin(self.tmp, "old", ["network"]))
        newer(self.cat, "p", "0.2.0", ["network", "fs:write:cwd"])
        shell = self.shell("n", "y")
        _, out, _ = self.line(shell, "plugin update")
        self.assertEqual((out, self.session.stopped), (f"skipped {PID}: new permissions not approved\n", []))
        _, out, _ = self.line(shell, "plugin update")
        self.assertEqual((out, self.session.stopped), (f"updated {PID} 0.1.0 → 0.2.0\n", [PID]))


def widen(folder, permissions=("network", "fs:write:cwd"), env=("SNEAKY",)):
    """Rewrite a plugin folder's grants in place, as a hostile folder would while the prompt is open."""
    path = Path(folder) / "plugin.toml"
    head = path.read_text().split("\n[capabilities]")[0]
    path.write_text(
        head
        + "\n[capabilities]\npermissions = "
        + str(list(permissions)).replace("'", '"')
        + "\nenv = "
        + str(list(env)).replace("'", '"')
        + "\n"
    )


class TocTou(tc.TmpCase):
    """The grants shown are the grants installed, whatever happens to the source during the prompt."""

    def setUp(self):
        super().setUp()
        self.stage_root = self.tmp / "stage"
        self.stage_root.mkdir()
        patcher = mock.patch("tempfile.tempdir", str(self.stage_root))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.store()

    def left(self):
        return os.listdir(self.stage_root)

    def install(self, folder, ask, interactive=True):
        return run(main.cmd_plugin_install, self.store(), [str(folder)], interactive=interactive, ask=ask)

    def reset(self):
        """The state setUp makes, afresh: no plugin installed, no catalog, nothing staged."""
        for child in self.tmp.iterdir():
            if child != self.stage_root:
                shutil.rmtree(child)
        for child in self.stage_root.iterdir():
            shutil.rmtree(child)
        self.home.mkdir()
        self.cat.mkdir()
        self.store()

    def test_staging_dir_is_removed_in_every_outcome(self):
        def success(folder):
            self.install(folder, Scripted("y"))

        def decline(folder):
            self.install(folder, Scripted("n"))

        def refusal(folder):
            self.install(folder, Scripted(), interactive=False)

        def failure(folder):
            with mock.patch.object(store_mod.Store, "install", side_effect=OSError("disk")):
                with self.assertRaises(OSError):
                    self.install(folder, Scripted("y"))

        def interrupt(folder):
            with self.assertRaises(KeyboardInterrupt):
                self.install(folder, Scripted(KeyboardInterrupt))

        for case in (success, decline, refusal, failure, interrupt):
            with self.subTest(case.__name__):
                self.reset()
                case(plugin(self.tmp, "p", ["network"]))
                self.assertEqual(self.left(), [])

        def update(*args, answer=None, interactive=False):
            try:
                run(main.cmd_plugin_update, self.store(), list(args), interactive=interactive, ask=Scripted(answer))
            except KeyboardInterrupt:
                pass

        cases = {
            "decline": lambda: update(answer="n", interactive=True),
            "interrupt": lambda: update(answer=KeyboardInterrupt, interactive=True),
            "refusal": lambda: update(),
            "dry run": lambda: update("--dry-run"),
            "yes": lambda: update("--yes"),
        }
        for name, case in cases.items():
            with self.subTest(f"update: {name}"):
                self.reset()
                self.catalog_config(self.cat)
                self.store().install(plugin(self.tmp, "old", ["network"]))
                newer(self.cat, "p", "0.2.0", ["network", "fs:write:cwd"])
                case()
                self.assertEqual(self.left(), [])
                self.assertEqual(self.store().index()[PID]["version"], "0.2.0" if name == "yes" else "0.1.0")


class AlreadyInstalled(tc.TmpCase):
    def setUp(self):
        super().setUp()
        self.catalog_config(self.cat)
        self.store().install(plugin(self.tmp, "old", ["network"]))

    def install(self, target, ask):
        return run(main.cmd_plugin_install, self.store(), [str(target)], interactive=True, ask=ask)

    def test_catalog_id_asks_only_for_added_grants(self):
        plugin(self.cat, "p", ["network", "fs:write:cwd"])
        ask = Scripted("y")
        code, out, err = self.install(PID, ask)
        self.assertEqual(code, 0, err)
        self.assertEqual(err, f"{PID} 0.1.0 → 0.1.0 adds: fs:write:cwd\n")
        self.assertEqual(ask.questions, ["Install it? [y/N] "])


if __name__ == "__main__":
    unittest.main()
