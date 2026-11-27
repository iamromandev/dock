"""Manifests and config of removed features fail loudly; they are never half-honoured. No runtime needed."""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dock_host.manifest import Manifest, ManifestError
from dock_host.store import Store

BASE = """\
id = "dev.dock.sample"
version = "1.2.3"
api_version = "2"
description = "x"

[run]
{run}

[[commands]]
name = "go"
"""


def make_plugin(run: str, extra: str = "") -> Path:
    root = Path(tempfile.mkdtemp(prefix="dock-removed-"))
    (root / "plugin.toml").write_text(BASE.format(run=run) + extra)
    (root / "Dockerfile").write_text("FROM scratch\n")
    return root


CONTAINER = 'container = { cmd = "python3", args = ["/plugin/main.py"] }'
NATIVE = 'any = { cmd = "python3", args = ["main.py"] }'
BOTH = NATIVE + "\n" + CONTAINER
ONLY_CONTAINER = "dock runs plugins only in a container ([run].container)"


class RemovedCapabilities(unittest.TestCase):
    def load(self, extra: str) -> str:
        with self.assertRaises(ManifestError) as cm:
            Manifest.load(make_plugin(CONTAINER, extra))
        return str(cm.exception)

    def test_removed_manifest_features_fail_the_install(self):
        for name, extra, message in (
            (
                "capabilities.http",
                '\n[capabilities]\nhttp = ["example.com"]\n',
                "dev.dock.sample: [capabilities].http: HTTP through dock is no longer supported",
            ),
            (
                "provides",
                '\n[capabilities]\nprovides = ["llm"]\n',
                "dev.dock.sample: [capabilities].provides: capabilities are no longer supported",
            ),
            (
                "requires",
                '\n[capabilities]\nrequires = ["llm"]\n',
                "dev.dock.sample: [capabilities].requires: capabilities are no longer supported",
            ),
            (
                "lifecycle provision",
                "\n[lifecycle]\nprovision = true\n",
                "dev.dock.sample: [lifecycle].provision is no longer supported",
            ),
            (
                "network:listen permission",
                '\n[capabilities]\npermissions = ["network", "network:listen"]\n',
                "dev.dock.sample: permission 'network:listen' is no longer supported",
            ),
        ):
            with self.subTest(name):
                self.assertEqual(self.load(extra), message)


class ReadOnlyFolders(unittest.TestCase):
    """R15: a folder an earlier WASM install left without write bits can still be removed or replaced."""

    def setUp(self):
        self.store = Store(Path(tempfile.mkdtemp(prefix="dock-removed-home-")))
        self.addCleanup(self.cleanup)
        self.src = make_plugin('container = { cmd = "python3" }')
        (self.src / "sub").mkdir()
        (self.src / "sub" / "f.txt").write_text("x")
        self.store.install(self.src)
        self.dest = self.store.plugin_dir / "dev.dock.sample" / "1.2.3"

    def cleanup(self):
        for here, dirs, files in os.walk(self.store.home):
            for name in dirs + files:
                p = Path(here, name)
                if not p.is_symlink():
                    p.chmod(p.stat().st_mode | 0o200)
        shutil.rmtree(self.store.home, ignore_errors=True)

    def lock(self):
        for here, dirs, files in os.walk(self.dest, topdown=False):
            for name in dirs + files:
                p = Path(here, name)
                if not p.is_symlink():
                    p.chmod(p.stat().st_mode & ~0o222)
        self.dest.chmod(self.dest.stat().st_mode & ~0o222)

    def test_remove_does_not_follow_a_symlinked_plugin_folder(self):
        outside = Path(tempfile.mkdtemp(prefix="dock-outside-"))
        self.addCleanup(shutil.rmtree, outside, True)
        (outside / "f.txt").write_text("x")
        outside.chmod(0o500)
        (outside / "f.txt").chmod(0o400)
        shutil.rmtree(self.store.plugin_dir / "dev.dock.sample")
        (self.store.plugin_dir / "dev.dock.sample").symlink_to(outside)
        self.addCleanup(outside.chmod, 0o700)
        self.store.remove("dev.dock.sample")
        self.assertEqual(outside.stat().st_mode & 0o777, 0o500)
        self.assertEqual((outside / "f.txt").stat().st_mode & 0o777, 0o400)


if __name__ == "__main__":
    unittest.main()
