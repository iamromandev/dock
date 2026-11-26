"""Permission prompts for `dock plugin install` and `dock plugin update`."""

import dataclasses
import io
import json
import os
import shutil
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import dockenv
import test_catalog as tc
import test_update as tu
from src import approve, main
from src import store as store_mod
from src.manifest import Manifest
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


class Grants(tc.TmpCase):
    def test_order_is_network_fs_others_then_env(self):
        m = dataclasses.replace(manifest(self.tmp, ["fs:write:cwd", "network"], ["B_ONE", "A_TWO"]),
                                permissions=["x:y", "fs:write:cwd", "network"])  # hand-built: load rejects x:y
        self.assertEqual(approve.grants(m), ["network", "fs:write:cwd", "x:y", "env B_ONE", "env A_TWO"])

    def test_none_declared(self):
        self.assertEqual(approve.grants(manifest(self.tmp)), [])

    def test_duplicates_are_dropped(self):
        m = dataclasses.replace(manifest(self.tmp), permissions=["network", "network", "z:z", "z:z"], env=["A", "A"])
        self.assertEqual(approve.grants(m), ["network", "z:z", "env A"])


class Added(tc.TmpCase):
    def test_all_when_not_installed(self):
        self.assertEqual(approve.added(manifest(self.tmp, ["network"], ["A"]), None), ["network", "env A"])

    def test_only_new_grants(self):
        old = manifest(self.tmp, ["network"])
        new = Manifest.load(plugin(self.tmp, "n", permissions=["network", "fs:write:cwd"], env=["A"]))
        self.assertEqual(approve.added(new, old), ["fs:write:cwd", "env A"])

    def test_dropped_grants_add_nothing(self):
        old = manifest(self.tmp, ["network", "fs:write:cwd"])
        new = Manifest.load(plugin(self.tmp, "n", permissions=["network"]))
        self.assertEqual(approve.added(new, old), [])


class TaggedGrants(tc.TmpCase):
    def hand(self, permissions=(), env=()):
        return dataclasses.replace(manifest(self.tmp), permissions=list(permissions), env=list(env))

    def test_a_permission_cannot_pre_approve_an_env_name(self):
        old = self.hand(permissions=["env SECRET"])
        self.assertEqual(approve.added(self.hand(env=["SECRET"]), old), ["env SECRET"])

    def test_an_env_name_cannot_pre_approve_a_permission(self):
        old = self.hand(env=["network"])
        self.assertEqual(approve.added(self.hand(permissions=["network"]), old), ["network"])

    def test_same_tagged_grant_is_not_added_again(self):
        old = self.hand(permissions=["network"], env=["A"])
        self.assertEqual(approve.added(self.hand(permissions=["network"], env=["A"]), old), [])

    def test_non_printable_characters_are_escaped_for_display(self):
        m = self.hand(permissions=["a\x1b[31mb\nc"], env=["E\x07"])
        self.assertEqual(approve.grants(m), ["a\\x1b[31mb\\nc", "env E\\x07"])

    def test_printable_text_is_left_alone(self):
        self.assertEqual(approve.grants(self.hand(permissions=["é network"])), ["é network"])


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

    def test_yes_flag_approves_without_asking(self):
        for interactive in (True, False):
            with self.subTest(interactive=interactive):
                self.assertEqual(self.decide(yes=True, interactive=interactive), ("yes", [], "LINE\n"))

    def test_no_terminal_refuses_without_asking(self):
        self.assertEqual(self.decide(interactive=False), ("refused", [], "dock: LINE; rerun with --yes to approve\n"))

    def test_answers(self):
        for answer, expected in (("y", "yes"), ("YES\n", "yes"), ("  Y  ", "yes"), ("n", "no"), ("", "no"),
                                 ("yep", "no"), (None, "no")):
            with self.subTest(answer=answer):
                result, calls, err = self.decide(answer=answer)
                self.assertEqual((result, calls, err), (expected, ["Q? "], "LINE\n"))

    def test_ctrl_c_propagates(self):
        def ask(_):
            raise KeyboardInterrupt
        with redirect_stderr(io.StringIO()), self.assertRaises(KeyboardInterrupt):
            approve.Approver(False, True, ask).decide("LINE", "Q? ")

    def test_default_ask_reads_stdin_and_writes_question_to_stderr(self):
        err = io.StringIO()
        for text, expected in (("yes\n", "yes\n"), ("", None)):
            with self.subTest(text=text), mock.patch("sys.stdin", io.StringIO(text)), redirect_stderr(err):
                self.assertEqual(approve._ask_stdin("Q? "), expected)
        self.assertEqual(err.getvalue(), "Q? Q? ")

    def test_interactive_needs_stdin_and_stderr_terminals(self):
        for stdin, stderr, expected in ((False, False, False), (True, False, False), (False, True, False),
                                        (True, True, True)):
            with self.subTest(stdin=stdin, stderr=stderr), mock.patch("sys.stdin") as i, mock.patch("sys.stderr") as e:
                i.isatty.return_value, e.isatty.return_value = stdin, stderr
                self.assertEqual(approve.Approver(False).interactive, expected)

    def test_undecodable_answer_is_a_decline(self):
        class Bad:
            def readline(self):
                raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        with mock.patch("sys.stdin", Bad()), redirect_stderr(io.StringIO()):
            self.assertIsNone(approve._ask_stdin("Q? "))


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
        return run(main.cmd_plugin_install, self.store(), [*flags, str(folder)], stop=stop,
                   interactive=interactive, ask=ask)

    def test_no_grants_installs_without_asking(self):
        ask = Scripted()
        code, out, err = self.install(plugin(self.tmp, "p"), ask=ask)
        self.assertEqual((code, out.splitlines()[0], err, ask.questions), (0, f"installed {PID} 0.1.0", "", []))

    def test_approve_installs(self):
        ask = Scripted("y")
        code, out, err = self.install(plugin(self.tmp, "p", ["network"], ["A_B"]), ask=ask)
        self.assertEqual(code, 0)
        self.assertEqual(err, f"{PID} 0.1.0 asks for: network, env A_B\n")
        self.assertEqual(ask.questions, ["Install it? [y/N] "])
        self.assertIn(PID, self.store().index())

    def test_decline_writes_nothing(self):
        folder = plugin(self.tmp, "p", ["network"])
        before = tu.tree(self.home)
        for answer in ("n", "", None):
            with self.subTest(answer=answer):
                code, out, err = self.install(folder, ask=Scripted(answer))
                self.assertEqual((code, out), (1, "not installed\n"))
                self.assertEqual(tu.tree(self.home), before)

    def test_decline_does_not_stop_the_running_plugin(self):
        stopped = []
        self.install(plugin(self.tmp, "p", ["network"]), ask=Scripted("n"), stop=stopped.append)
        self.assertEqual(stopped, [])

    def test_approve_stops_then_installs(self):
        stopped = []
        self.install(plugin(self.tmp, "p", ["network"]), ask=Scripted("y"), stop=stopped.append)
        self.assertEqual(stopped, [PID])

    def test_yes_flag_in_any_position_skips_question(self):
        for flags in (("--yes",), ("-y",)):
            for after in (False, True):
                with self.subTest(flags=flags, after=after):
                    store = self.store()
                    args = [str(plugin(self.tmp, "p", ["network"]))]
                    args = args + list(flags) if after else list(flags) + args
                    code, out, err = run(main.cmd_plugin_install, store, args, interactive=False, ask=Scripted())
                    self.assertEqual(code, 0)
                    self.assertIn(f"{PID} 0.1.0 asks for: network\n", err)
                    store.remove(PID)

    def test_no_terminal_refuses(self):
        before = tu.tree(self.home)
        code, out, err = self.install(plugin(self.tmp, "p", ["network"]), interactive=False, ask=Scripted())
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, f"dock: {PID} 0.1.0 asks for: network; rerun with --yes to approve\n")
        self.assertEqual(tu.tree(self.home), before)

    def test_reinstall_asks_only_for_added_grants(self):
        self.store().install(plugin(self.tmp, "old", ["network"]))
        ask = Scripted("y")
        code, out, err = self.install(plugin(self.tmp, "new", ["network", "fs:write:cwd"]), ask=ask)
        self.assertEqual(code, 0)
        self.assertEqual(err, f"{PID} 0.1.0 → 0.1.0 adds: fs:write:cwd\n")

    def test_reinstall_without_added_grants_asks_nothing(self):
        self.store().install(plugin(self.tmp, "old", ["network", "fs:write:cwd"]))
        ask = Scripted()
        code, _, err = self.install(plugin(self.tmp, "new", ["network"], ), ask=ask)
        self.assertEqual((code, err, ask.questions), (0, "", []))

    def test_unreadable_installed_manifest_counts_all_as_new(self):
        store = self.store()
        store.install(plugin(self.tmp, "old", ["network"]))
        (Path(store.index()[PID]["path"]) / "plugin.toml").unlink()
        ask = Scripted("y")
        code, out, err = self.install(plugin(self.tmp, "new", ["network"]), ask=ask)
        self.assertEqual(code, 0)
        self.assertIn("adds: network", err)

    def test_unknown_option_is_usage_error(self):
        code, out, err = run(main.cmd_plugin_install, self.store(), ["--force", "x"])
        self.assertEqual((code, out), (2, ""))
        self.assertTrue(err.startswith("dock: plugin install: unknown option '--force'\n"))
        self.assertIn("usage: dock", err)

    def test_needs_exactly_one_target(self):
        for args in ([], ["--yes"], ["a", "b"]):
            with self.subTest(args=args):
                code, out, err = run(main.cmd_plugin_install, self.store(), args)
                self.assertEqual((code, out), (2, ""))
                self.assertIn("usage: dock", err)

    def test_ctrl_c_cancels_and_main_exits_130(self):
        folder = plugin(self.tmp, "p", ["network"])
        before = tu.tree(self.home)
        class Tty(io.StringIO):
            def isatty(self):
                return True
        with mock.patch("src.approve._ask_stdin", side_effect=KeyboardInterrupt), \
                mock.patch("sys.stdin.isatty", return_value=True, create=True), \
                mock.patch.dict("os.environ", {"DOCK_HOME": str(self.home)}), \
                redirect_stdout(io.StringIO()), redirect_stderr(Tty()):
            code = main.main(["plugin", "install", str(folder)])
        self.assertEqual(code, 130)
        self.assertEqual(tu.tree(self.home), before)


class UpdatePrompt(tc.TmpCase):
    def setUp(self):
        super().setUp()
        self.catalog_config(self.cat)
        self.store().install(plugin(self.tmp, "old", ["network"]))
        self.folder = plugin(self.cat, "p")  # placeholder, rewritten per test

    def put(self, version="0.2.0", permissions=("network", "fs:write:cwd"), env=None, name="p", pid=PID):
        shutil.rmtree(self.cat / name, ignore_errors=True)
        newer(self.cat, name, version, list(permissions), env, pid)

    def update(self, *args, ask=None, interactive=True, stop=None):
        return run(main.cmd_plugin_update, self.store(), list(args), stop=stop, interactive=interactive, ask=ask)

    def test_added_grants_are_asked(self):
        self.put()
        ask = Scripted("y")
        code, out, err = self.update(ask=ask)
        self.assertEqual((code, out), (0, f"updated {PID} 0.1.0 → 0.2.0\n"))
        self.assertEqual(err, f"{PID} 0.1.0 → 0.2.0 adds: fs:write:cwd\n")
        self.assertEqual(ask.questions, ["Update it? [y/N] "])

    def test_no_added_grants_asks_nothing(self):
        self.put(permissions=("network",))
        ask = Scripted()
        code, out, err = self.update(ask=ask)
        self.assertEqual((code, out, err, ask.questions), (0, f"updated {PID} 0.1.0 → 0.2.0\n", "", []))

    def test_dropped_grants_need_no_approval(self):
        self.put(permissions=())
        code, out, err = self.update(ask=Scripted(), interactive=False)
        self.assertEqual((code, err), (0, ""))

    def test_decline_skips_and_is_not_a_failure(self):
        self.put()
        before = tu.tree(self.home)
        stopped = []
        code, out, err = self.update(ask=Scripted("n"), stop=stopped.append)
        self.assertEqual((code, out), (0, f"skipped {PID}: new permissions not approved\n"))
        self.assertEqual((stopped, tu.tree(self.home)), ([], before))
        self.assertEqual(self.store().index()[PID]["version"], "0.1.0")

    def test_decline_continues_with_others(self):
        self.put()
        self.store().install(plugin(self.tmp, "o", None, None, "dev.t.other"))
        newer(self.cat, "o", "0.3.0", None, None, "dev.t.other")
        code, out, err = self.update(ask=Scripted("n"))
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), [f"skipped {PID}: new permissions not approved",
                                            "updated dev.t.other 0.1.0 → 0.3.0"])
        self.assertNotIn("up to date", out)

    def test_yes_flag_approves_without_asking(self):
        self.put()
        for flag in ("--yes", "-y"):
            with self.subTest(flag=flag):
                self.put(version="0.2.0")
                code, out, err = self.update(flag, ask=Scripted(), interactive=False)
                self.assertEqual(code, 0)
                self.assertIn("adds: fs:write:cwd\n", err)
                self.store().remove(PID)
                self.store().install(plugin(self.tmp, "old", ["network"]))

    def test_no_terminal_fails_that_plugin_but_continues(self):
        self.put()
        self.store().install(plugin(self.tmp, "o", None, None, "dev.t.other"))
        newer(self.cat, "o", "0.3.0", None, None, "dev.t.other")
        code, out, err = self.update(interactive=False, ask=Scripted())
        self.assertEqual(code, 1)
        self.assertEqual(out, "updated dev.t.other 0.1.0 → 0.3.0\n")
        self.assertIn(f"dock: {PID} 0.1.0 → 0.2.0 adds: fs:write:cwd; rerun with --yes to approve\n", err)
        self.assertEqual(self.store().index()[PID]["version"], "0.1.0")

    def test_dry_run_shows_adds_and_never_asks(self):
        self.put()
        before = tu.tree(self.home)
        code, out, err = self.update("--dry-run", ask=Scripted(), interactive=False)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, f"would update {PID} 0.1.0 → 0.2.0 (adds: fs:write:cwd)\n")
        self.assertEqual(tu.tree(self.home), before)

    def test_ctrl_c_cancels_the_run(self):
        self.put()
        with self.assertRaises(KeyboardInterrupt):
            self.update(ask=Scripted(KeyboardInterrupt))
        self.assertEqual(self.store().index()[PID]["version"], "0.1.0")


class PromptCli(tc.CliCase):
    def setUp(self):
        super().setUp()
        self.store()  # a new DOCK_HOME gets its empty folders on first use; snapshots are taken after that
        self.grant = plugin(self.tmp, "grant", ["network"])

    def test_refused_without_terminal(self):
        before = tu.tree(self.home)
        r = self.dock("plugin", "install", str(self.grant))
        self.assertEqual((r.returncode, r.stdout), (1, ""))
        self.assertEqual(r.stderr, f"dock: {PID} 0.1.0 asks for: network; rerun with --yes to approve\n")
        self.assertEqual(tu.tree(self.home), before)
        self.assertEqual(self.runtime_calls(), "")

    def test_yes_before_or_after(self):
        for args in (("--yes", str(self.grant)), (str(self.grant), "-y")):
            with self.subTest(args=args):
                r = self.dock("plugin", "install", *args)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(r.stderr, f"{PID} 0.1.0 asks for: network\n")
                self.assertEqual(r.stdout.splitlines()[0], f"installed {PID} 0.1.0")
                self.dock("plugin", "remove", PID)

    def test_unknown_flag_exits_2(self):
        r = self.dock("plugin", "install", "--force", str(self.grant))
        self.assertEqual(r.returncode, 2)
        self.assertIn("unknown option '--force'", r.stderr)

    def test_hello_installs_without_a_prompt(self):
        r = self.dock("plugin", "install", str(self.hello))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")
        self.assertTrue(r.stdout.startswith("installed dev.dock.hello 0.2.0\n"))

    def test_update_refuses_one_and_updates_the_other(self):
        self.store().install(plugin(self.tmp, "a", ["network"]))
        self.store().install(plugin(self.tmp, "b", None, None, "dev.t.b"))
        shutil.rmtree(self.hello)
        newer(self.cat, "a", "0.2.0", ["network", "fs:write:cwd"])
        newer(self.cat, "b", "0.2.0", None, None, "dev.t.b")
        r = self.dock("plugin", "update")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stdout, "updated dev.t.b 0.1.0 → 0.2.0\n")
        self.assertIn("rerun with --yes to approve", r.stderr)
        self.assertEqual(self.index()[PID]["version"], "0.1.0")
        r = self.dock("plugin", "update", "--yes")
        self.assertEqual((r.returncode, r.stdout), (0, f"updated {PID} 0.1.0 → 0.2.0\n"))

    def test_dry_run_needs_no_terminal(self):
        self.store().install(plugin(self.tmp, "a", ["network"]))
        shutil.rmtree(self.hello)
        newer(self.cat, "a", "0.2.0", ["network", "fs:write:cwd"])
        r = self.dock("plugin", "update", "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, f"would update {PID} 0.1.0 → 0.2.0 (adds: fs:write:cwd)\n")

    def test_no_terminal_never_reads_stdin(self):
        p = subprocess.Popen([sys.executable, "-m", "src", "plugin", "install", str(self.grant)],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             env=self.env, cwd=self.tmp, **dockenv.SIGINT_DEFAULT)
        try:
            out, err = p.communicate(timeout=20)  # stdin stays open: a read would block until the timeout
        except subprocess.TimeoutExpired:
            p.kill()
            p.communicate()
            self.fail("dock blocked on stdin")
        finally:
            p.stdin and p.stdin.close()
        self.assertEqual(p.returncode, 1)


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

    def test_install_declined_does_not_stop_plugin(self):
        shell = self.shell("n")
        self.line(shell, f"plugin install {plugin(self.tmp, 'g', ['network'])}")
        self.assertEqual((self.session.stopped, self.ask.questions), ([], ["Install it? [y/N] "]))
        self.assertEqual(self.index(), {})

    def test_install_approved_stops_then_installs(self):
        shell = self.shell("y")
        self.line(shell, f"plugin install {plugin(self.tmp, 'g', ['network'])}")
        self.assertEqual(self.session.stopped, [PID])
        self.assertIn(PID, self.index())

    def test_install_without_terminal_is_refused(self):
        shell = self.shell(interactive=False)
        _, _, err = self.line(shell, f"plugin install {plugin(self.tmp, 'g', ['network'])}")
        self.assertIn("rerun with --yes to approve", err)
        self.assertEqual((self.session.stopped, self.index()), ([], {}))

    def test_update_declined_then_approved(self):
        self.store().install(plugin(self.tmp, "old", ["network"]))
        newer(self.cat, "p", "0.2.0", ["network", "fs:write:cwd"])
        shell = self.shell("n", "y")
        _, out, _ = self.line(shell, "plugin update")
        self.assertEqual((out, self.session.stopped), (f"skipped {PID}: new permissions not approved\n", []))
        _, out, _ = self.line(shell, "plugin update")
        self.assertEqual((out, self.session.stopped), (f"updated {PID} 0.1.0 → 0.2.0\n", [PID]))

    def test_piped_shell_refuses_and_next_line_still_runs(self):
        r = self.dock("shell", stdin=f"plugin install {plugin(self.tmp, 'g', ['network'])}\nversion\nexit\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("rerun with --yes to approve", r.stderr)
        self.assertIn("dock ", r.stdout)
        self.assertEqual(self.index(), {})


def widen(folder, permissions=("network", "fs:write:cwd"), env=("SNEAKY",)):
    """Rewrite a plugin folder's grants in place, as a hostile folder would while the prompt is open."""
    path = Path(folder) / "plugin.toml"
    head = path.read_text().split("\n[capabilities]")[0]
    path.write_text(head + "\n[capabilities]\npermissions = " + str(list(permissions)).replace("'", '"')
                    + "\nenv = " + str(list(env)).replace("'", '"') + "\n")


class Staged(tc.TmpCase):
    def setUp(self):
        super().setUp()
        self.stage_root = self.tmp / "stage"
        self.stage_root.mkdir()
        patcher = mock.patch("tempfile.tempdir", str(self.stage_root))
        patcher.start()
        self.addCleanup(patcher.stop)

    def left(self):
        return os.listdir(self.stage_root)

    def test_yields_a_private_copy_outside_the_source_and_removes_it(self):
        src = plugin(self.tmp, "p", ["network"])
        (src / ".git").mkdir()
        (src / ".git" / "x").write_text("x")
        with store_mod.staged(src) as copy:
            self.assertTrue(copy.is_relative_to(self.stage_root))
            self.assertEqual(os.stat(copy.parent).st_mode & 0o777, 0o700)
            self.assertEqual(Manifest.load(copy).permissions, ["network"])
            self.assertFalse((copy / ".git").exists())
            widen(src)  # a later change to the source does not reach the copy
            self.assertEqual(Manifest.load(copy).permissions, ["network"])
        self.assertEqual(self.left(), [])

    def test_removed_when_the_body_raises(self):
        for exc in (RuntimeError, KeyboardInterrupt):
            with self.subTest(exc=exc), self.assertRaises(exc):
                with store_mod.staged(plugin(self.tmp, "p")):
                    raise exc
            self.assertEqual(self.left(), [])

    def test_escaping_symlink_is_refused_and_leaves_nothing(self):
        src = plugin(self.tmp, "p")
        (src / "leak").symlink_to("/etc/passwd")
        with self.assertRaises(store_mod.ManifestError), store_mod.staged(src):
            pass
        self.assertEqual(self.left(), [])

    def test_removal_never_follows_a_symlink(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "keep").write_text("k")
        src = plugin(self.tmp, "p")
        (src / "inner").mkdir()
        (src / "link").symlink_to("inner")
        with store_mod.staged(src) as copy:
            (copy / "inner" / "out").symlink_to(outside)
        self.assertEqual((outside / "keep").read_text(), "k")
        self.assertEqual(self.left(), [])


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

    def test_install_installs_only_what_was_shown(self):
        folder = plugin(self.tmp, "p", ["network"])

        def ask(question):
            widen(folder)
            return "y"
        code, out, err = self.install(folder, ask)
        self.assertEqual(code, 0)
        self.assertEqual(err, f"{PID} 0.1.0 asks for: network\n")
        m = self.store().get(PID)
        self.assertEqual((m.permissions, m.env), (["network"], []))
        self.assertEqual(out.splitlines()[0], f"installed {PID} 0.1.0")
        self.assertNotIn("dock-stage", out + err)
        self.assertNotIn("dock-stage", json.dumps(self.store().index()))

    def test_update_installs_only_what_was_shown(self):
        self.catalog_config(self.cat)
        self.store().install(plugin(self.tmp, "old", ["network"]))
        newer(self.cat, "p", "0.2.0", ["network", "fs:write:cwd"])

        def ask(question):
            widen(self.cat / "p")
            return "y"
        code, out, err = run(main.cmd_plugin_update, self.store(), [], interactive=True, ask=ask)
        self.assertEqual((code, out), (0, f"updated {PID} 0.1.0 → 0.2.0\n"))
        m = self.store().get(PID)
        self.assertEqual((m.version, m.permissions, m.env), ("0.2.0", ["network", "fs:write:cwd"], []))
        self.assertEqual(self.left(), [])

    def test_install_without_a_prompt_records_the_store_path(self):
        code, out, err = self.install(plugin(self.tmp, "p"), Scripted())
        self.assertEqual(code, 0)
        self.assertTrue(Path(self.store().index()[PID]["path"]).is_relative_to(self.home))
        self.assertEqual(self.left(), [])

    def test_staging_dir_is_removed_in_every_outcome(self):
        folder = plugin(self.tmp, "p", ["network"])
        cases = {
            "success": lambda: self.install(folder, Scripted("y")),
            "decline": lambda: self.install(folder, Scripted("n")),
            "refusal": lambda: self.install(folder, Scripted(), interactive=False),
        }
        for name, fn in cases.items():
            with self.subTest(name):
                fn()
                self.assertEqual(self.left(), [])
                if name == "success":
                    self.store().remove(PID)
        with self.subTest("failure"), mock.patch.object(store_mod.Store, "install", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                self.install(folder, Scripted("y"))
            self.assertEqual(self.left(), [])
        with self.subTest("interrupt"), self.assertRaises(KeyboardInterrupt):
            self.install(folder, Scripted(KeyboardInterrupt))
        self.assertEqual(self.left(), [])

    def test_decline_and_refusal_leave_dock_home_identical(self):
        folder = plugin(self.tmp, "p", ["network"])
        before = tu.tree(self.home)
        self.install(folder, Scripted("n"))
        self.install(folder, Scripted(), interactive=False)
        self.assertEqual(tu.tree(self.home), before)

    def test_update_staging_dir_is_removed_in_every_outcome(self):
        self.catalog_config(self.cat)
        self.store().install(plugin(self.tmp, "old", ["network"]))
        newer(self.cat, "p", "0.2.0", ["network", "fs:write:cwd"])
        for answer in ("n", KeyboardInterrupt):
            with self.subTest(answer=answer):
                try:
                    run(main.cmd_plugin_update, self.store(), [], interactive=True, ask=Scripted(answer))
                except KeyboardInterrupt:
                    pass
                self.assertEqual(self.left(), [])
        run(main.cmd_plugin_update, self.store(), [], interactive=False, ask=Scripted())
        self.assertEqual(self.left(), [])
        run(main.cmd_plugin_update, self.store(), ["--dry-run"], interactive=False, ask=Scripted())
        self.assertEqual(self.left(), [])
        run(main.cmd_plugin_update, self.store(), ["--yes"], interactive=False, ask=Scripted())
        self.assertEqual(self.left(), [])
        self.assertEqual(self.store().index()[PID]["version"], "0.2.0")


class Validation(tc.CliCase):
    def setUp(self):
        super().setUp()
        self.store()

    def raw(self, caps, name="raw"):
        root = tc.make_plugin(self.tmp, name, PID)
        with (root / "plugin.toml").open("a") as f:
            f.write("\n[capabilities]\n" + caps)
        return root

    def test_bad_permissions_are_rejected_and_nothing_is_written(self):
        before = tu.tree(self.home)
        for label, caps in (
            ("control character", 'permissions = ["net\\u0007work"]\n'),
            ("escape sequence", 'permissions = ["\\u001b[2Jnetwork"]\n'),
            ("env as a permission", 'permissions = ["env SECRET"]\n'),
            ("unknown", 'permissions = ["root"]\n'),
            ("a string", 'permissions = "network"\n'),
            ("a number in a list", "permissions = [1]\n"),
        ):
            with self.subTest(label):
                r = self.dock("plugin", "install", "--yes", str(self.raw(caps)))
                self.assertEqual((r.returncode, r.stdout), (1, ""), r.stderr)
                self.assertTrue("unknown permission" in r.stderr or "must be a list of strings" in r.stderr, r.stderr)
                self.assertEqual(tu.tree(self.home), before)

    def test_message_names_the_known_permissions(self):
        r = self.dock("plugin", "install", "--yes", str(self.raw('permissions = ["root"]\n')))
        self.assertIn("unknown permission 'root' (known: network, fs:write:cwd)", r.stderr)

    def test_control_characters_are_shown_escaped_in_the_error(self):
        r = self.dock("plugin", "install", "--yes", str(self.raw('permissions = ["a\\u001bb"]\n')))
        self.assertNotIn("\x1b", r.stderr)

    def test_network_listen_keeps_its_own_message(self):
        r = self.dock("plugin", "install", "--yes", str(self.raw('permissions = ["network:listen"]\n')))
        self.assertIn("'network:listen' is no longer supported", r.stderr)

    def test_known_permissions_still_install(self):
        r = self.dock("plugin", "install", "--yes", str(self.raw('permissions = ["network", "fs:write:cwd"]\n')))
        self.assertEqual(r.returncode, 0, r.stderr)


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

    def test_other_version_from_another_folder_asks_only_for_added_grants(self):
        other = newer(self.tmp, "other", "0.3.0", ["network"], ["A_KEY"])
        ask = Scripted("y")
        code, out, err = self.install(other, ask)
        self.assertEqual(code, 0, err)
        self.assertEqual(err, f"{PID} 0.1.0 → 0.3.0 adds: env A_KEY\n")

    def test_catalog_id_with_nothing_added_asks_nothing(self):
        plugin(self.cat, "p", ["network"])
        code, out, err = self.install(PID, Scripted())
        self.assertEqual((code, err), (0, ""))


if __name__ == "__main__":
    unittest.main()
