"""--in/--out and the [paths] table: reading, flags, resolution and the fail-closed cases. No container runtime."""

from __future__ import annotations

import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from src import catalog, flow, flow_cli, isolation, paths
from src import main as main_mod
from src.manifest import ManifestError
from src.store import Store
import test_catalog as tc


class PathsCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dock-paths-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.user = self.tmp / "user"
        self.user.mkdir()
        self.home = self.tmp / "dockhome"
        self.home.mkdir()
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.user)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.store = Store(self.home)

    def conf(self, text):
        (self.home / "config.toml").write_text(text)


class Config(PathsCase):
    def test_no_config_or_no_table_is_empty(self):
        self.assertEqual(paths.config(self.store), {})
        self.conf("[shell]\nidle_timeout = 5\n")
        self.assertEqual(paths.config(self.store), {})

    def test_reads_in_and_out_with_tilde_expanded(self):
        self.conf('[paths]\nin = "~/a"\nout = "~/b"\n')
        self.assertEqual(paths.config(self.store), {"in": self.user / "a", "out": self.user / "b"})

    def test_bad_settings_fail_closed_naming_the_file(self):
        cases = {
            "unknown key": '[paths]\nbogus = "/x"\n',
            "non-string": "[paths]\nout = 3\n",
            "empty": '[paths]\nout = ""\n',
            "relative": '[paths]\nout = "rel/dir"\n',
            "top-level out": 'out = "/x"\n',
            "top-level in": 'in = "/x"\n',
            "not a table": 'paths = "/x"\n',
        }
        for label, text in cases.items():
            with self.subTest(label):
                self.conf(text)
                with self.assertRaises(ManifestError) as cm:
                    paths.config(self.store)
                self.assertIn(str(self.home / "config.toml"), str(cm.exception))

    def test_a_paths_table_does_not_break_the_other_readers(self):
        self.conf('[container]\nruntime = "docker"\n[catalog]\npaths = ["/c"]\n[paths]\nout = "/o"\nin = "/i"\n')
        self.assertEqual(isolation.config(self.store).runtime, "docker")
        self.assertEqual(catalog.paths(self.store), [Path("/c")])
        self.assertEqual(paths.config(self.store), {"in": Path("/i"), "out": Path("/o")})
        self.conf('[paths]\nout = "/o"\n')  # without [catalog] / [container] too
        self.assertEqual(catalog.paths(self.store), [])
        self.assertIsNone(isolation.config(self.store).runtime)


class TakeFlags(unittest.TestCase):
    def test_pops_leading_flags_in_both_forms(self):
        self.assertEqual(paths.take_flags(["--out", "x", "--in=y", "shell"]), ("y", "x", ["shell"]))
        self.assertEqual(paths.take_flags(["--out=x", "do", "f"]), (None, "x", ["do", "f"]))
        self.assertEqual(paths.take_flags(["--in", "y"]), ("y", None, []))

    def test_stops_at_the_first_non_option(self):
        self.assertEqual(paths.take_flags(["do", "--out", "x"]), (None, None, ["do", "--out", "x"]))
        self.assertEqual(paths.take_flags([]), (None, None, []))

    def test_repeated_or_valueless_flag_is_an_error(self):
        for argv in (["--out", "a", "--out", "b"], ["--in=a", "--in", "b"], ["--out"], ["--in", "--out"], ["--out="]):
            with self.subTest(argv), self.assertRaises(ManifestError):
                paths.take_flags(argv)


class Resolve(PathsCase):
    def test_no_flags_no_config_is_the_current_folder(self):
        cwd = self.tmp / "cwd"
        cwd.mkdir()
        d = paths.resolve(self.store, None, None, cwd=cwd)
        self.assertEqual(d, paths.Dirs(None, cwd, "current folder", "current folder", cwd))

    def test_flag_beats_config_beats_current_folder(self):
        cwd = self.tmp / "cwd"
        cwd.mkdir()
        (self.user / "cin").mkdir()
        (self.user / "fin").mkdir()
        self.conf(f'[paths]\nout = "{self.user}/cfg"\nin = "{self.user}/cin"\n')
        d = paths.resolve(self.store, None, None, cwd=cwd)
        self.assertEqual((d.out_dir, d.out_source, d.in_dir, d.in_source),
                         (self.user / "cfg", "[paths] out", self.user / "cin", "[paths] in"))
        d = paths.resolve(self.store, str(self.user / "fin"), str(self.user / "flag"), cwd=cwd)
        self.assertEqual((d.out_dir, d.out_source, d.in_dir, d.in_source),
                         (self.user / "flag", "--out", self.user / "fin", "--in"))

    def test_relative_flag_is_against_cwd_and_symlinks_resolve_and_parents_are_made(self):
        cwd = self.user / "cwd"
        cwd.mkdir()
        (self.user / "real").mkdir()
        (self.user / "link").symlink_to(self.user / "real")
        d = paths.resolve(self.store, None, "deep/er", cwd=cwd)
        self.assertEqual(d.out_dir, cwd / "deep" / "er")
        self.assertTrue(d.out_dir.is_dir())
        d = paths.resolve(self.store, None, str(self.user / "link" / "sub"), cwd=cwd)
        self.assertEqual(d.out_dir, self.user / "real" / "sub")

    @unittest.skipIf(not hasattr(os, "geteuid") or os.geteuid() == 0, "root can write anywhere")
    def test_an_uncreatable_out_names_the_path_and_the_source(self):
        ro = self.user / "ro"
        ro.mkdir()
        ro.chmod(0o500)
        self.addCleanup(ro.chmod, 0o700)
        with self.assertRaises(ManifestError) as cm:
            paths.resolve(self.store, None, str(ro / "x"), cwd=self.user)
        self.assertIn(str(ro / "x"), str(cm.exception))
        self.assertIn("--out", str(cm.exception))

    def test_a_too_broad_out_is_refused_naming_the_source(self):
        with self.assertRaises(ManifestError) as cm:
            paths.resolve(self.store, None, str(self.user), cwd=self.user)
        self.assertIn("--out", str(cm.exception))
        self.conf(f'[paths]\nout = "{self.home}"\n')
        with self.assertRaises(ManifestError) as cm:
            paths.resolve(self.store, None, None, cwd=self.user)
        self.assertIn("[paths] out", str(cm.exception))


class RefusedLeavesNothing(PathsCase):
    def test_a_refused_out_creates_no_folder(self):
        evil = self.home / "data" / "evil" / "x"
        with self.assertRaises(ManifestError):
            paths.resolve(self.store, None, str(evil), cwd=self.user)
        self.assertFalse((self.home / "data" / "evil").exists())
        self.conf(f'[paths]\nout = "{self.home}/data/evil2/x"\n')
        with self.assertRaises(ManifestError):
            paths.resolve(self.store, None, None, cwd=self.user)
        self.assertFalse((self.home / "data" / "evil2").exists())


class InFolder(PathsCase):
    def test_in_must_be_an_existing_folder_naming_the_source(self):
        (self.user / "file").write_text("x")
        cases = (("missing", "nope"), ("a file", "file"))
        for label, value in cases:
            with self.subTest(label), self.assertRaises(ManifestError) as cm:
                paths.resolve(self.store, str(self.user / value), None, cwd=self.user)
            self.assertIn("--in", str(cm.exception))

    def test_a_stale_or_broad_config_in_is_left_to_launch_time(self):
        # catches: a deleted [paths] in making every command fail, not only the fs:read:in ones
        for value in (self.user / "nope", self.user):
            self.conf(f'[paths]\nin = "{value}"\n')
            d = paths.resolve(self.store, None, None, cwd=self.user)
            self.assertEqual((d.in_dir, d.in_source), (value, "[paths] in"))

    def test_plugin_list_works_with_a_stale_config_in_and_an_fs_read_in_plugin_names_it(self):
        self.conf(f'[paths]\nin = "{self.user}/nope"\n')
        with mock.patch.dict(os.environ, {"DOCK_HOME": str(self.home)}), mock.patch("sys.stdout", new_callable=io.StringIO), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(main_mod.main(["plugin", "list"]), 0, err.getvalue())
        import dataclasses
        from src.manifest import Manifest
        m = dataclasses.replace(Manifest.load(tc.make_plugin(self.user, "rd", "dev.t.rd")), permissions=["fs:read:in"])
        store = mock.Mock(dirs=paths.resolve(self.store, None, None, cwd=self.user), home=self.home)
        with self.assertRaises(ManifestError) as cm:
            isolation.in_folder(store, m, str(self.user))
        self.assertIn("[paths] in", str(cm.exception))

    def test_a_too_broad_in_is_refused_and_a_project_folder_is_kept(self):
        with self.assertRaises(ManifestError) as cm:
            paths.resolve(self.store, str(self.user), None, cwd=self.user)
        self.assertIn("--in", str(cm.exception))
        (self.user / "proj").mkdir()
        d = paths.resolve(self.store, str(self.user / "proj"), None, cwd=self.user)
        self.assertEqual(d.in_dir, self.user / "proj")


class MainWiring(PathsCase):
    """main() resolves the folders once and moves into the out folder before any command runs."""

    def setUp(self):
        super().setUp()
        self.start = Path(os.getcwd())
        self.addCleanup(os.chdir, self.start)
        os.chdir(self.user)
        patcher = mock.patch.dict(os.environ, {"DOCK_HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_main(self, argv):
        seen = {}

        def probe(store, *a, **k):
            seen["cwd"] = Path(os.getcwd()).resolve()
            seen["dirs"] = store.dirs
            return 0

        with mock.patch.object(main_mod, "cmd_help", probe), mock.patch("src.shell.start", probe), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            code = main_mod.main(argv)
        return code, seen, err.getvalue()

    def test_out_flag_moves_the_command_into_the_out_folder(self):
        out = self.user / "o"
        code, seen, _ = self.run_main(["--out", str(out), "shell"])
        self.assertEqual((code, seen["cwd"]), (0, out))
        self.assertEqual(seen["dirs"].out_dir, out)

    def test_out_flag_applies_to_the_shell(self):
        out = self.user / "o"
        code, seen, _ = self.run_main(["-v", "--out", str(out), "shell"])
        self.assertEqual((code, seen["cwd"], seen["dirs"].out_source), (0, out, "--out"))

    def test_config_out_applies_without_a_flag(self):
        self.conf(f'[paths]\nout = "{self.user}/cfg"\n')
        _, seen, _ = self.run_main(["shell"])
        self.assertEqual(seen["cwd"], self.user / "cfg")

    def test_no_flags_leave_the_current_folder_alone(self):
        _, seen, _ = self.run_main(["shell"])
        self.assertEqual(seen["cwd"], self.user)
        self.assertEqual(seen["dirs"].out_source, "current folder")

    def test_a_refused_out_prints_the_error_and_exits_1(self):
        code, seen, err = self.run_main(["--out", str(self.user), "shell"])
        self.assertEqual((code, seen), (1, {}))
        self.assertIn("dock: --out", err)
        self.assertEqual(Path(os.getcwd()).resolve(), self.user)

    def test_flags_and_verbose_in_any_order_before_the_subcommand(self):
        out = self.user / "o"
        for argv in (["--out", str(out), "-v", "shell"], ["-v", "--out", str(out), "--verbose", "shell"]):
            with self.subTest(argv):
                code, seen, _ = self.run_main(argv)
                self.assertEqual((code, seen["cwd"]), (0, out))

    def test_a_bad_paths_table_still_lets_version_and_help_run(self):
        self.conf('[paths]\nbogus = "/x"\n')
        for argv in (["version"], ["--version"], ["help"], ["-h"], ["--help"]):
            with self.subTest(argv), mock.patch("sys.stdout", new_callable=io.StringIO):
                code, _, err = self.run_main(argv)
                self.assertEqual((code, err), (0, ""))

    def test_a_bad_paths_table_fails_other_commands_with_exit_1(self):
        self.conf('[paths]\nbogus = "/x"\n')
        code, _, err = self.run_main(["plugin", "list"])
        self.assertEqual(code, 1)
        self.assertIn("unknown key", err)

    def test_an_out_that_cannot_be_entered_exits_1_naming_it(self):
        out = self.user / "o"
        with mock.patch("os.chdir", side_effect=PermissionError(13, "Permission denied")):
            code, seen, err = self.run_main(["--out", str(out), "shell"])
        self.assertEqual((code, seen), (1, {}))
        self.assertIn(f"dock: --out {out}: cannot enter it: ", err)

    def test_relative_path_arguments_are_against_the_start_folder_not_out(self):
        (self.user / "rel.flow.toml").write_text("x")
        loaded = []

        def fake_load(path):
            loaded.append(Path(path))
            raise flow.FlowError(None, "stop")

        with mock.patch.object(flow_cli, "_load", fake_load), mock.patch("sys.stderr", new_callable=io.StringIO):
            main_mod.main(["--out", "o", "flow", "check", "rel.flow.toml"])
            os.chdir(self.user)  # main() moved into o
            main_mod.main(["flow", "check", "rel.flow.toml"])
        self.assertEqual(loaded, [self.user / "rel.flow.toml"] * 2)

    def test_plugin_install_and_info_of_a_relative_folder_work_with_out(self):
        tc.make_plugin(self.user, "rel", "dev.t.rel")
        for argv in (["plugin", "info", "./rel"], ["plugin", "install", "./rel"]):
            os.chdir(self.user)  # an earlier main() moved into o
            with self.subTest(argv), mock.patch("sys.stdout", new_callable=io.StringIO) as out, \
                    mock.patch("sys.stderr", new_callable=io.StringIO) as err:
                code = main_mod.main(["--out", "o", *argv])
            self.assertEqual((code, err.getvalue()), (0, ""), out.getvalue())
        self.assertIn("dev.t.rel", self.store.index())


    def test_a_plugin_command_without_fs_read_in_says_that_in_is_ignored(self):
        tc.make_plugin(self.user, "rd", "dev.t.rd")
        self.store.install(self.user / "rd")
        (self.user / "i").mkdir()
        for argv in (["--in", "i"], []):
            with self.subTest(argv), mock.patch.object(main_mod.lifecycle, "run", return_value=0), \
                    mock.patch.object(main_mod.isolation, "sweep"), mock.patch("sys.stderr", new_callable=io.StringIO) as err:
                os.chdir(self.user)
                main_mod.main([*argv, "dev.t.rd:go"])
            self.assertEqual(err.getvalue().count("--in ignored: no plugin in this run reads it"), 1 if argv else 0, err.getvalue())


class InIgnored(unittest.TestCase):
    """R10: --in that nothing in the run reads is said once, on stderr."""

    def warned(self, in_dir, *perm_lists, source="--in"):
        from src.manifest import Manifest  # noqa: F401
        from test_flow import manifest
        store = mock.Mock(dirs=paths.Dirs(in_dir, Path("/o"), source, "current folder"))
        ms = [manifest(f"p{i}", [], perms) for i, perms in enumerate(perm_lists)]
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            paths.warn_if_in_unused(store, ms)
        return err.getvalue()

    def test_warns_once_only_when_in_is_set_and_no_plugin_reads_it(self):
        self.assertEqual(self.warned(Path("/i"), ["network"], []), "dock: --in ignored: no plugin in this run reads it\n")
        self.assertEqual(self.warned(Path("/i"), ["network"], ["fs:read:in"]), "")
        self.assertEqual(self.warned(None, ["network"]), "")

    def test_the_text_names_the_flag_and_a_config_default_is_silent(self):
        # catches: [paths] in being reported as --in, or nagging on every run
        self.assertEqual(self.warned(Path("/i"), [], source="--in"), "dock: --in ignored: no plugin in this run reads it\n")
        self.assertEqual(self.warned(Path("/i"), [], source="[paths] in"), "")

if __name__ == "__main__":
    unittest.main()
