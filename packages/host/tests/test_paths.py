"""isolation.in_folder: a stale [paths] in is refused at launch, naming the setting. No container runtime."""

import dataclasses
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_catalog as tc
from dock_host import isolation
from dock_host.manifest import Manifest, ManifestError


class InFolder(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dock-paths-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.user = self.tmp / "user"
        self.user.mkdir()
        self.home = self.tmp / "dockhome"
        self.home.mkdir()

    def test_an_fs_read_in_plugin_with_a_stale_config_in_names_it(self):
        m = dataclasses.replace(
            Manifest.load(tc.make_plugin(self.user, "rd", "dev.t.rd")), permissions=["fs:read:in"]
        )
        dirs = SimpleNamespace(in_dir=self.user / "nope", in_source="[paths] in")  # what paths.resolve hands over
        store = mock.Mock(dirs=dirs, home=self.home)
        with self.assertRaises(ManifestError) as cm:
            isolation.in_folder(store, m, str(self.user))
        self.assertIn("[paths] in", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
