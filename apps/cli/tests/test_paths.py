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

from src import isolation, paths
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


class InFolder(PathsCase):
    def test_plugin_list_works_with_a_stale_config_in_and_an_fs_read_in_plugin_names_it(self):
        self.conf(f'[paths]\nin = "{self.user}/nope"\n')
        with (
            mock.patch.dict(os.environ, {"DOCK_HOME": str(self.home)}),
            mock.patch("sys.stdout", new_callable=io.StringIO),
            mock.patch("sys.stderr", new_callable=io.StringIO) as err,
        ):
            self.assertEqual(main_mod.main(["plugin", "list"]), 0, err.getvalue())
        import dataclasses
        from src.manifest import Manifest

        m = dataclasses.replace(
            Manifest.load(tc.make_plugin(self.user, "rd", "dev.t.rd")), permissions=["fs:read:in"]
        )
        store = mock.Mock(dirs=paths.resolve(self.store, None, None, cwd=self.user), home=self.home)
        with self.assertRaises(ManifestError) as cm:
            isolation.in_folder(store, m, str(self.user))
        self.assertIn("[paths] in", str(cm.exception))


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

        with (
            mock.patch.object(main_mod, "cmd_help", probe),
            mock.patch("src.shell.start", probe),
            mock.patch("sys.stderr", new_callable=io.StringIO) as err,
        ):
            code = main_mod.main(argv)
        return code, seen, err.getvalue()

    def test_out_flag_applies_to_the_shell(self):
        out = self.user / "o"
        code, seen, _ = self.run_main(["-v", "--out", str(out), "shell"])
        self.assertEqual((code, seen["cwd"], seen["dirs"].out_source), (0, out, "--out"))

    def test_a_refused_out_prints_the_error_and_exits_1(self):
        code, seen, err = self.run_main(["--out", str(self.user), "shell"])
        self.assertEqual((code, seen), (1, {}))
        self.assertIn("dock: --out", err)
        self.assertEqual(Path(os.getcwd()).resolve(), self.user)

    def test_plugin_install_and_info_of_a_relative_folder_work_with_out(self):
        tc.make_plugin(self.user, "rel", "dev.t.rel")
        for argv in (["plugin", "info", "./rel"], ["plugin", "install", "./rel"]):
            os.chdir(self.user)  # an earlier main() moved into o
            with (
                self.subTest(argv),
                mock.patch("sys.stdout", new_callable=io.StringIO) as out,
                mock.patch("sys.stderr", new_callable=io.StringIO) as err,
            ):
                code = main_mod.main(["--out", "o", *argv])
            self.assertEqual((code, err.getvalue()), (0, ""), out.getvalue())
        self.assertIn("dev.t.rel", self.store.index())

    def test_a_plugin_command_without_fs_read_in_says_that_in_is_ignored(self):
        tc.make_plugin(self.user, "rd", "dev.t.rd")
        self.store.install(self.user / "rd")
        (self.user / "i").mkdir()
        for argv in (["--in", "i"], []):
            with (
                self.subTest(argv),
                mock.patch.object(main_mod.lifecycle, "run", return_value=0),
                mock.patch.object(main_mod.isolation, "sweep"),
                mock.patch("sys.stderr", new_callable=io.StringIO) as err,
            ):
                os.chdir(self.user)
                main_mod.main([*argv, "dev.t.rd:go"])
            self.assertEqual(
                err.getvalue().count("--in ignored: no plugin in this run reads it"),
                1 if argv else 0,
                err.getvalue(),
            )


if __name__ == "__main__":
    unittest.main()
