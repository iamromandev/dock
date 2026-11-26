"""AC17: the dm manifest announces torrents, takes --stall-seconds, and updates from 0.1.0 silently."""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import dockenv
from src.manifest import Manifest

DM = ROOT.parent.parent / "plugins" / "dm"
COPY_ID = "dev.dock.dmcopy"

# the permissions and inputs of dm 0.1.0, the manifest a user has before the update
OLD_INPUTS = '''inputs = [
  { name = "url", type = "string", required = true },
  { name = "output", type = "string", flag = "--output", short = "-o" },
  { name = "dir", type = "string", flag = "--dir", short = "-d" },
]
'''


def cli(*args, home=None, cwd=ROOT):
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    if home is not None:
        env["DOCK_HOME"] = str(home)
    return subprocess.run([sys.executable, "-m", "src", *args], capture_output=True, text=True, env=env,
                          cwd=cwd, timeout=120, check=False, stdin=subprocess.DEVNULL)


class Manifest02(unittest.TestCase):
    def test_version_summary_and_keywords(self):
        m = Manifest.load(DM)
        self.assertEqual(m.version, "0.8.0")
        (command,) = m.commands
        self.assertEqual(command.summary,
                         "Download a URL, a .torrent or a magnet link into the current folder: "
                         "dm <url|file.torrent|magnet> [-o name | -d folder] [--no-resume] [--no-dht] [--list] "
                         "[--only picks] [--skip picks]")
        for word in ("download", "fetch", "get", "url", "http", "torrent", "bittorrent"):
            self.assertIn(word, command.keywords)

    def test_stall_seconds_is_an_int_input(self):
        (command,) = Manifest.load(DM).commands
        stall = {i.name: i for i in command.inputs}["stall"]
        self.assertEqual((stall.type, stall.flag, stall.required), ("int", "--stall-seconds", False))

    def test_permissions_are_unchanged(self):
        self.assertEqual(Manifest.load(DM).permissions, ["network", "fs:write:cwd"])

    def test_plugin_info_shows_the_new_summary(self):
        r = cli("plugin", "info", str(DM))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("0.8.0", r.stdout)
        self.assertIn(".torrent", r.stdout)
        self.assertIn("bittorrent", r.stdout)


class Update(unittest.TestCase):
    def test_updating_from_0_1_0_asks_nothing(self):
        dockenv.require_runtime()
        tmp = Path(tempfile.mkdtemp(prefix="dock-dmupd-")).resolve()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        old = dockenv.plugin_copy(DM, tmp / "old", COPY_ID)
        text = (old / "plugin.toml").read_text()
        text = re.sub(r'(?m)^version = ".*"$', 'version = "0.1.0"', text, count=1)
        text = re.sub(r"(?s)inputs = \[.*?\n\]\n", OLD_INPUTS, text, count=1)
        (old / "plugin.toml").write_text(text)
        catalog = tmp / "catalog"
        dockenv.plugin_copy(DM, catalog, COPY_ID)
        home = tmp / "home"
        home.mkdir()
        dockenv.write_config(home, dockenv.require_runtime())
        dockenv.add_config(home, f'[catalog]\npaths = ["{catalog}"]\n')
        for version in ("0.1.0", "0.8.0"):
            self.addCleanup(dockenv.remove_image, f"dock/{COPY_ID}:{version}")
        r = cli("plugin", "install", str(old), "--yes", home=home)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = cli("plugin", "update", COPY_ID, home=home)  # no --yes, no terminal: a prompt would refuse
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.strip(), f"updated {COPY_ID} 0.1.0 → 0.8.0")
        self.assertNotIn("adds", r.stderr)


if __name__ == "__main__":
    unittest.main()
