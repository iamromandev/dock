"""AC15: dm has --no-resume (since 0.3.0), the same permissions, and `plugin update` from 0.2.0 asks nothing."""

import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import dockenv
from src.manifest import Manifest
from test_dm_torrent_manifest import cli

DM = ROOT.parent.parent / "plugins" / "dm"
COPY_ID = "dev.dock.dmresume"
SUMMARY = ("Download a URL, a .torrent or a magnet link into the current folder: "
           "dm <url|file.torrent|magnet> [-o name | -d folder] [--no-resume] [--no-dht] [--list] "
           "[--only picks] [--skip picks]")
# the 0.2.0 inputs: everything but --no-resume
OLD_INPUTS = '''inputs = [
  { name = "url", type = "string", required = true },
  { name = "output", type = "string", flag = "--output", short = "-o" },
  { name = "dir", type = "string", flag = "--dir", short = "-d" },
  { name = "stall", type = "int", flag = "--stall-seconds" },
]
'''


class Manifest03(unittest.TestCase):
    def test_version_summary_and_the_no_resume_flag(self):
        m = Manifest.load(DM)
        self.assertEqual(m.version, "0.8.0")
        (command,) = m.commands
        self.assertEqual(command.summary, SUMMARY)
        flag = {i.name: i for i in command.inputs}["no_resume"]
        self.assertEqual((flag.type, flag.flag, flag.required), ("bool", "--no-resume", False))

    def test_permissions_are_unchanged(self):
        self.assertEqual(Manifest.load(DM).permissions, ["network", "fs:write:cwd"])


class Update(unittest.TestCase):
    def test_updating_from_0_2_0_asks_nothing(self):
        dockenv.require_runtime()
        tmp = Path(tempfile.mkdtemp(prefix="dock-dmres-")).resolve()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        old = dockenv.plugin_copy(DM, tmp / "old", COPY_ID)
        text = (old / "plugin.toml").read_text()
        text = re.sub(r'(?m)^version = ".*"$', 'version = "0.2.0"', text, count=1)
        text = re.sub(r"(?s)inputs = \[.*?\n\]\n", OLD_INPUTS, text, count=1)
        (old / "plugin.toml").write_text(text)
        catalog = tmp / "catalog"
        dockenv.plugin_copy(DM, catalog, COPY_ID)
        home = tmp / "home"
        home.mkdir()
        dockenv.write_config(home, dockenv.require_runtime())
        dockenv.add_config(home, f'[catalog]\npaths = ["{catalog}"]\n')
        for version in ("0.2.0", "0.8.0"):
            self.addCleanup(dockenv.remove_image, f"dock/{COPY_ID}:{version}")
        r = cli("plugin", "install", str(old), "--yes", home=home)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = cli("plugin", "update", COPY_ID, home=home)  # no --yes, no terminal: a prompt would refuse
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.strip(), f"updated {COPY_ID} 0.2.0 → 0.8.0")
        self.assertNotIn("adds", r.stderr)


if __name__ == "__main__":
    unittest.main()
