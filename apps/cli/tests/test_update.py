"""`dock plugin update`: version parsing, the catalog check, Store.update and the CLI.

Helpers come from test_catalog (imported as a module so unittest does not collect its classes twice).
"""

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

    def test_tie_on_highest_names_every_folder(self):
        self.install("0.1.0")
        a, b = self.put("a", "0.2.0"), self.put("b", "0.2.0")
        (c,), _ = self.check()
        self.assertEqual(c.status, "error")
        self.assertIn(str(a), c.message)
        self.assertIn(str(b), c.message)

    def test_huge_catalog_version_is_skipped_with_a_warning(self):
        self.install("0.1.0")
        self.put("a", "1" + "0" * 5000)
        self.put("b", "0.2.0")
        (c,), warnings = self.check()
        self.assertEqual((c.status, c.new), ("update", "0.2.0"))
        self.assertEqual(len(warnings), 1)
        self.assertIn("is not dotted numbers", warnings[0])

    def test_unparsable_installed_version_is_skipped_or_an_error_when_named(self):
        self.install("dev")
        self.put("a", "0.2.0")
        result, _ = self.check()
        self.assertEqual([c.status for c in result], ["skip"])
        self.assertIn("dev", result[0].message)
        result, _ = self.check([PID])
        self.assertEqual([c.status for c in result], ["error"])


class StoreUpdate(tc.TmpCase):
    def setUp(self):
        super().setUp()
        self.store_ = self.store()
        self.store_.install(tc.make_plugin(self.tmp / "old", "p", PID, "0.1.0"))
        self.new = tc.make_plugin(self.tmp / "new", "p", PID, "0.2.0")

    def test_failed_copy_leaves_index_and_old_folder(self):
        outside = self.tmp / "secret"
        outside.write_text("x")
        (self.new / "link").symlink_to(outside)
        before = self.store_.index()
        with self.assertRaises(ManifestError):
            self.store_.update(PID, self.new)
        self.assertEqual(self.store_.index(), before)
        self.assertTrue((self.store_.plugin_dir / PID / "0.1.0" / "plugin.toml").is_file())

    def stderr_of(self, folder):
        with mock.patch("src.isolation.forget_image"), mock.patch("builtins.print") as pr:
            self.store_.update(PID, folder)
        return [str(c.args[0]) for c in pr.call_args_list]

    def test_unloadable_old_manifest_warns_the_image_stays(self):
        (self.store_.plugin_dir / PID / "0.1.0" / "plugin.toml").write_text("not [ toml")
        lines = self.stderr_of(self.new)
        self.assertTrue(any(f"dock/{PID}:0.1.0" in ln and "not removed" in ln for ln in lines), lines)


def tree(home):
    """Every path under home with its mtime: equal before and after means nothing was written."""
    return sorted((str(p.relative_to(home)), p.lstat().st_mtime_ns) for p in Path(home).rglob("*"))


if __name__ == "__main__":
    unittest.main()
