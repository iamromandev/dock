"""`dock plugin update`: version parsing, the catalog check, Store.update and the CLI.

Helpers come from test_catalog (imported as a module so unittest does not collect its classes twice).
"""

import os
import re
import shutil
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import test_catalog as tc
from src import catalog
from src.manifest import ManifestError

PID = "dev.t.p"


class VersionParse(unittest.TestCase):
    def test_numbers_compare_part_wise(self):
        self.assertGreater(catalog.parse_version("0.10.0"), catalog.parse_version("0.9.2"))

    def test_trailing_zeros_are_ignored(self):
        self.assertEqual(catalog.parse_version("1.0"), catalog.parse_version("1.0.0"))
        self.assertEqual(catalog.parse_version("1.0.0"), catalog.parse_version("1"))

    def test_not_dotted_numbers_is_none(self):
        for v in ("2.0-beta", "dev", "", "1..2", "1.", ".1", "v1", "1.0 ", "١.٢"):
            with self.subTest(v=v):
                self.assertIsNone(catalog.parse_version(v))

    def test_huge_number_is_none(self):
        self.assertIsNone(catalog.parse_version("1" + "0" * 5000))


class CheckUpdates(tc.TmpCase):
    def put(self, folder, version, pid=PID):
        return tc.make_plugin(self.cat / folder, "p", pid, version)

    def check(self, ids=(), store=None):
        warnings = []
        result = catalog.check_updates(store or self.store(), list(ids), warn=warnings.append)
        return result, warnings

    def setUp(self):
        super().setUp()
        self.catalog_config(self.cat / "a", self.cat / "b")

    def install(self, version, pid=PID):
        store = self.store()
        store.install(tc.make_plugin(self.tmp / "src", "p", pid, version))
        return store

    def test_no_catalog_is_an_error(self):
        self.config("")
        with self.assertRaises(ManifestError) as cm:
            self.check()
        self.assertIn("no catalog folders configured ([catalog] paths in ", str(cm.exception))
        self.assertIn("config.toml)", str(cm.exception))

    def test_newer_catalog_copy_is_an_update(self):
        self.install("0.1.0")
        newer = self.put("a", "0.2.0")
        (c,), _ = self.check()
        self.assertEqual((c.plugin_id, c.status, c.old, c.new, c.folder), (PID, "update", "0.1.0", "0.2.0", newer))

    def test_same_or_older_is_current(self):
        self.install("1.0")
        self.put("a", "1.0.0")
        (c,), _ = self.check()
        self.assertEqual((c.status, c.old), ("current", "1.0"))
        self.put("a", "0.9")
        (c,), _ = self.check()
        self.assertEqual(c.status, "current")

    def test_highest_copy_wins(self):
        self.install("0.1.0")
        self.put("a", "0.2.0")
        best = self.put("b", "0.10.0")
        (c,), _ = self.check()
        self.assertEqual((c.status, c.new, c.folder), ("update", "0.10.0", best))

    def test_tie_on_highest_names_every_folder(self):
        self.install("0.1.0")
        a, b = self.put("a", "0.2.0"), self.put("b", "0.2.0")
        (c,), _ = self.check()
        self.assertEqual(c.status, "error")
        self.assertIn(str(a), c.message)
        self.assertIn(str(b), c.message)

    def test_tie_not_newer_than_installed_is_current(self):
        self.install("0.3.0")
        self.put("a", "0.2.0")
        self.put("b", "0.2.0")
        (c,), _ = self.check()
        self.assertEqual(c.status, "current")

    def test_huge_catalog_version_is_skipped_with_a_warning(self):
        self.install("0.1.0")
        self.put("a", "1" + "0" * 5000)
        self.put("b", "0.2.0")
        (c,), warnings = self.check()
        self.assertEqual((c.status, c.new), ("update", "0.2.0"))
        self.assertEqual(len(warnings), 1)
        self.assertIn("is not dotted numbers", warnings[0])

    def test_tie_below_the_highest_is_fine(self):
        self.install("0.1.0")
        self.put("a", "0.2.0")
        self.put("b", "0.2.0")
        best = tc.make_plugin(self.tmp / "other", "p", PID, "0.3.0")
        self.catalog_config(self.cat / "a", self.cat / "b", best.parent)
        (c,), _ = self.check()
        self.assertEqual((c.status, c.new), ("update", "0.3.0"))

    def test_unparsable_catalog_copy_is_ignored_with_one_warning(self):
        self.install("0.1.0")
        bad = self.put("a", "2.0-beta")
        self.put("b", "0.2.0")
        (c,), warnings = self.check()
        self.assertEqual((c.status, c.new), ("update", "0.2.0"))
        self.assertEqual(len(warnings), 1)
        self.assertIn(f"catalog plugin {bad} skipped: version '2.0-beta' is not dotted numbers", warnings[0])

    def test_unparsable_installed_version_is_skipped_or_an_error_when_named(self):
        self.install("dev")
        self.put("a", "0.2.0")
        result, _ = self.check()
        self.assertEqual([c.status for c in result], ["skip"])
        self.assertIn("dev", result[0].message)
        result, _ = self.check([PID])
        self.assertEqual([c.status for c in result], ["error"])

    def test_no_catalog_copy(self):
        self.install("0.1.0")
        self.put("a", "0.2.0", pid="dev.t.other")
        result, _ = self.check()
        self.assertEqual(result, [])
        (c,), _ = self.check([PID])
        self.assertEqual((c.status, c.message), ("error", f"{PID} is not in the catalog"))

    def test_named_but_not_installed(self):
        self.put("a", "0.2.0")
        (c,), _ = self.check([PID])
        self.assertEqual((c.status, c.message), ("error", f"{PID} is not installed"))

    def test_sorted_by_id_and_deduplicated_and_loaded_once(self):
        self.install("0.1.0", "dev.t.b")
        self.install("0.1.0", "dev.t.a")
        self.put("a", "0.2.0", "dev.t.a")
        self.put("b", "0.2.0", "dev.t.b")
        with mock.patch.object(catalog, "load", wraps=catalog.load) as load:
            result, _ = self.check(["dev.t.b", "dev.t.a", "dev.t.b"])
        self.assertEqual([c.plugin_id for c in result], ["dev.t.a", "dev.t.b"])
        self.assertEqual(load.call_count, 1)


class StoreUpdate(tc.TmpCase):
    def setUp(self):
        super().setUp()
        self.store_ = self.store()
        self.store_.install(tc.make_plugin(self.tmp / "old", "p", PID, "0.1.0"))
        self.new = tc.make_plugin(self.tmp / "new", "p", PID, "0.2.0")

    def test_update_installs_new_and_removes_old_folder(self):
        with mock.patch("src.isolation.forget_image") as forget:
            old, manifest = self.store_.update(PID, self.new)
        self.assertEqual((old, manifest.version), ("0.1.0", "0.2.0"))
        self.assertEqual(self.store_.index()[PID]["version"], "0.2.0")
        self.assertFalse((self.store_.plugin_dir / PID / "0.1.0").exists())
        self.assertTrue((self.store_.plugin_dir / PID / "0.2.0" / "plugin.toml").is_file())
        self.assertEqual([c.args[1].version for c in forget.call_args_list], ["0.2.0", "0.1.0"])

    def test_failed_copy_leaves_index_and_old_folder(self):
        outside = self.tmp / "secret"
        outside.write_text("x")
        (self.new / "link").symlink_to(outside)
        before = self.store_.index()
        with self.assertRaises(ManifestError):
            self.store_.update(PID, self.new)
        self.assertEqual(self.store_.index(), before)
        self.assertTrue((self.store_.plugin_dir / PID / "0.1.0" / "plugin.toml").is_file())

    def test_symlinked_old_folder_is_left_with_a_warning(self):
        target = self.tmp / "target"
        target.mkdir()
        (target / "keep").write_text("x")
        old = self.store_.plugin_dir / PID / "0.1.0"
        for f in old.iterdir():
            f.unlink()
        old.rmdir()
        old.symlink_to(target)
        with mock.patch("src.isolation.forget_image"), mock.patch("builtins.print") as pr:
            self.store_.update(PID, self.new)
        self.assertTrue((target / "keep").is_file())
        self.assertTrue(old.is_symlink())
        self.assertTrue(any("is a symlink" in str(c.args[0]) for c in pr.call_args_list))

    def test_rmtree_failure_is_a_warning(self):
        with mock.patch("src.isolation.forget_image"), mock.patch("shutil.rmtree", side_effect=OSError("busy")), \
                mock.patch("builtins.print") as pr:
            old, manifest = self.store_.update(PID, self.new)
        self.assertEqual(manifest.version, "0.2.0")
        self.assertTrue(any("could not remove old version folder" in str(c.args[0]) for c in pr.call_args_list))

    def stderr_of(self, folder):
        with mock.patch("src.isolation.forget_image"), mock.patch("builtins.print") as pr:
            self.store_.update(PID, folder)
        return [str(c.args[0]) for c in pr.call_args_list]

    @unittest.skipIf(os.geteuid() == 0, "root ignores file modes")
    def test_failed_install_removes_the_half_written_folder(self):
        locked = self.new / "locked"
        locked.write_text("x")
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o600)
        before = self.store_.index()
        with self.assertRaises(OSError):
            self.store_.update(PID, self.new)
        self.assertFalse((self.store_.plugin_dir / PID / "0.2.0").exists())
        self.assertEqual(self.store_.index(), before)
        self.assertTrue((self.store_.plugin_dir / PID / "0.1.0" / "plugin.toml").is_file())

    @unittest.skipIf(os.geteuid() == 0, "root ignores file modes")
    def test_failed_install_keeps_a_new_folder_that_existed_before(self):
        (self.store_.plugin_dir / PID / "0.2.0").mkdir()
        locked = self.new / "locked"
        locked.write_text("x")
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o600)
        with self.assertRaises(OSError):
            self.store_.update(PID, self.new)
        self.assertTrue((self.store_.plugin_dir / PID / "0.1.0" / "plugin.toml").is_file())

    def test_symlinked_plugin_root_is_refused_before_installing(self):
        root = self.store_.plugin_dir / PID
        target = self.tmp / "moved"
        root.rename(target)
        root.symlink_to(target)
        before = tree(self.tmp / "moved")
        with self.assertRaises(ManifestError) as cm:
            self.store_.update(PID, self.new)
        self.assertEqual(str(cm.exception), f"{root} is a symlink; not updating through it")
        self.assertEqual(tree(target), before)
        self.assertEqual(self.store_.index()[PID]["version"], "0.1.0")

    def test_symlinked_old_folder_warning_names_the_link(self):
        target = self.tmp / "target"
        target.mkdir()
        old = self.store_.plugin_dir / PID / "0.1.0"
        shutil.rmtree(old)
        old.symlink_to(target)
        lines = self.stderr_of(self.new)
        self.assertIn(f"dock: left {old} in place: it is a symlink", lines)

    def test_unloadable_old_manifest_warns_the_image_stays(self):
        (self.store_.plugin_dir / PID / "0.1.0" / "plugin.toml").write_text("not [ toml")
        lines = self.stderr_of(self.new)
        self.assertTrue(any(f"dock/{PID}:0.1.0" in ln and "not removed" in ln for ln in lines), lines)

    def test_index_path_that_is_not_the_expected_folder_is_left_alone(self):
        other = self.tmp / "elsewhere"
        shutil.copytree(self.store_.plugin_dir / PID / "0.1.0", other)
        idx = self.store_.index()
        idx[PID]["path"] = str(other)
        self.store_._save_index(idx)
        expected = self.store_.plugin_dir / PID / "0.1.0"
        lines = self.stderr_of(self.new)
        self.assertTrue(other.is_dir())
        self.assertTrue(expected.is_dir())
        self.assertIn(f"dock: left {other} in place: it is not {expected}", lines)


def tree(home):
    """Every path under home with its mtime: equal before and after means nothing was written."""
    return sorted((str(p.relative_to(home)), p.lstat().st_mtime_ns) for p in Path(home).rglob("*"))


class UpdateCli(tc.CliCase):
    def setUp(self):
        super().setUp()
        self.dock("plugin", "install", str(self.hello))
        self.runtime_reset()

    def runtime_reset(self):
        if self.log.exists():
            self.log.unlink()

    def bump(self, version, folder=None):
        """The catalog copy of the greeter at another version."""
        path = Path(folder or self.hello) / "plugin.toml"
        path.write_text(re.sub(r'(?m)^version = ".*"$', f'version = "{version}"', path.read_text(), count=1))

    def test_updates_every_installed_plugin(self):
        self.bump("0.3.0")
        r = self.dock("plugin", "update")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.splitlines(), ["updated dev.dock.greeter 0.2.0 → 0.3.0"])
        self.assertEqual(self.index()["dev.dock.greeter"]["version"], "0.3.0")
        self.assertFalse((self.home / "plugin" / "dev.dock.greeter" / "0.2.0").exists())
        self.assertIn("image rm", self.runtime_calls())

    def test_nothing_newer(self):
        r = self.dock("plugin", "update")
        self.assertEqual((r.returncode, r.stdout), (0, "all plugins up to date\n"))
        self.assertEqual(self.runtime_calls(), "")

    def test_named_and_current(self):
        r = self.dock("plugin", "update", "dev.dock.greeter")
        self.assertEqual((r.returncode, r.stdout), (0, "dev.dock.greeter 0.2.0 is up to date\n"))

    def test_dry_run_changes_nothing(self):
        self.bump("0.3.0")
        for args in (("--dry-run",), ("--dry-run", "dev.dock.greeter"), ("dev.dock.greeter", "--dry-run")):
            with self.subTest(args=args):
                before = tree(self.home)
                r = self.dock("plugin", "update", *args)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(r.stdout, "would update dev.dock.greeter 0.2.0 → 0.3.0\n")
                self.assertEqual(tree(self.home), before)
                self.assertEqual(self.runtime_calls(), "")

    def test_errors_for_named_ids(self):
        r = self.dock("plugin", "update", "dev.t.nope")
        self.assertEqual(r.returncode, 1)
        self.assertIn("dock: dev.t.nope is not installed", r.stderr)

    def test_one_failure_does_not_stop_the_others(self):
        self.bump("0.3.0")
        other = tc.make_plugin(self.tmp / "o", "p", PID, "0.1.0")
        self.assertEqual(self.dock("plugin", "install", str(other)).returncode, 0)
        newer = tc.make_plugin(self.cat, "p", PID, "0.2.0")
        (newer / "link").symlink_to(self.tmp / "runtime-calls")  # escapes the plugin folder
        r = self.dock("plugin", "update")
        self.assertEqual(r.returncode, 1)
        self.assertIn("updated dev.dock.greeter 0.2.0 → 0.3.0", r.stdout)
        self.assertIn(f"dock: {PID}: ", r.stderr)
        self.assertEqual(self.index()[PID]["version"], "0.1.0")

    @unittest.skipIf(os.geteuid() == 0, "root ignores file modes")
    def test_os_error_in_one_plugin_does_not_stop_the_others(self):
        self.bump("0.3.0")
        other = tc.make_plugin(self.tmp / "o", "p", PID, "0.1.0")
        self.assertEqual(self.dock("plugin", "install", str(other)).returncode, 0)
        newer = tc.make_plugin(self.cat, "p", "dev.aaa.first", "0.2.0")
        first = tc.make_plugin(self.tmp / "f", "p", "dev.aaa.first", "0.1.0")
        self.assertEqual(self.dock("plugin", "install", str(first)).returncode, 0)
        locked = newer / "locked"
        locked.write_text("x")
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o600)
        r = self.dock("plugin", "update")
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("updated dev.dock.greeter 0.2.0 → 0.3.0", r.stdout)
        self.assertIn("dock: dev.aaa.first: ", r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertFalse((self.home / "plugin" / "dev.aaa.first" / "0.2.0").exists())
        self.assertEqual(self.index()["dev.aaa.first"]["version"], "0.1.0")
        self.assertTrue((self.home / "plugin" / "dev.aaa.first" / "0.1.0" / "plugin.toml").is_file())

    def test_unknown_flag_is_a_usage_error(self):
        r = self.dock("plugin", "update", "--force")
        self.assertEqual(r.returncode, 2)
        self.assertIn("dock: plugin update: unknown option '--force'", r.stderr)

    def test_no_catalog_configured(self):
        (self.home / "config.toml").write_text(f'[container]\nruntime = "{self.runtime}"\n')
        r = self.dock("plugin", "update")
        self.assertEqual(r.returncode, 1)
        self.assertIn("dock: no catalog folders configured ([catalog] paths in ", r.stderr)

    def test_no_plugins_installed(self):
        self.dock("plugin", "remove", "dev.dock.greeter")
        r = self.dock("plugin", "update")
        self.assertEqual((r.returncode, r.stdout), (0, "no plugins installed\n"))

    def test_help_lists_it(self):
        self.assertIn("plugin update [--dry-run] [--yes] [<id>...]", self.dock("help").stdout)

    def test_works_without_a_runtime(self):
        self.bump("0.3.0")
        self.config('[container]\nruntime = "/nonexistent/docker"\n')
        self.catalog_config(self.cat)
        r = self.dock("plugin", "update")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("updated dev.dock.greeter 0.2.0 → 0.3.0", r.stdout)
        self.assertIn("may be stale", r.stderr)


if __name__ == "__main__":
    unittest.main()
