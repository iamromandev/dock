"""AC16: dm 0.4.0 downloads magnet links, has the same permissions, and `plugin update` from 0.3.0 asks nothing."""

import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "plugin" / "dm"))

import dockenv  # noqa: E402
from cli.manifest import Manifest  # noqa: E402
from dmcore.torrent import bencode, metadata, session  # noqa: E402
from test_dm_torrent_manifest import cli  # noqa: E402

DM = ROOT / "plugin" / "dm"
COPY_ID = "dev.dock.dmmagnet"
SUMMARY = ("Download a URL, a .torrent or a magnet link into the current folder: "
           "dm <url|file.torrent|magnet> [-o name | -d folder] [--no-resume] [--no-dht] [--list] "
           "[--only picks] [--skip picks]")


class Manifest04(unittest.TestCase):
    def test_version_summary_and_description_mention_magnet_links(self):
        m = Manifest.load(DM)
        self.assertEqual(m.version, "0.8.0")
        (command,) = m.commands
        self.assertEqual(command.summary, SUMMARY)
        self.assertIn("magnet", m.description)

    def test_the_inputs_are_the_old_ones_plus_no_dht_and_the_selection(self):
        (command,) = Manifest.load(DM).commands
        by_name = {i.name: i for i in command.inputs}
        self.assertEqual(list(by_name), ["url", "output", "dir", "stall", "no_resume", "no_dht", "list",
                                         "only", "skip"])
        self.assertEqual((by_name["url"].type, by_name["url"].required), ("string", True))

    def test_permissions_are_unchanged(self):
        self.assertEqual(Manifest.load(DM).permissions, ["network", "fs:write:cwd"])

    def test_the_peer_id_and_the_extension_handshake_name_the_version(self):
        s = session.Session(None, None, lambda *a: None, None, session.Options(), info_hash=b"\x00" * 20)
        self.addCleanup(s.pool.shutdown, wait=False)
        self.addCleanup(s.reader_pool.shutdown, wait=False)
        self.assertTrue(s.peer_id.startswith(b"-DM0800-"))
        self.assertEqual(len(s.peer_id), 20)
        self.assertEqual(bencode.decode(metadata.ext_handshake())[0][b"v"], b"dm 0.8.0")


class Update(unittest.TestCase):
    def test_updating_from_0_3_0_asks_nothing(self):
        dockenv.require_runtime()
        tmp = Path(tempfile.mkdtemp(prefix="dock-dmmag-")).resolve()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        old = dockenv.plugin_copy(DM, tmp / "old", COPY_ID)
        text = (old / "plugin.toml").read_text()
        text = re.sub(r'(?m)^version = ".*"$', 'version = "0.3.0"', text, count=1)
        text = re.sub(r'(?m)^summary = ".*"$', 'summary = "Download a URL or a .torrent into the current '
                      'folder: dm <url|file.torrent> [-o name | -d folder] [--no-resume]"', text, count=1)
        (old / "plugin.toml").write_text(text)
        catalog = tmp / "catalog"
        dockenv.plugin_copy(DM, catalog, COPY_ID)
        home = tmp / "home"
        home.mkdir()
        dockenv.write_config(home, dockenv.require_runtime())
        dockenv.add_config(home, f'[catalog]\npaths = ["{catalog}"]\n')
        for version in ("0.3.0", "0.8.0"):
            self.addCleanup(dockenv.remove_image, f"dock/{COPY_ID}:{version}")
        r = cli("plugin", "install", str(old), "--yes", home=home)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = cli("plugin", "update", COPY_ID, home=home)  # no --yes, no terminal: a prompt would refuse
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.strip(), f"updated {COPY_ID} 0.3.0 → 0.8.0")
        self.assertNotIn("adds", r.stderr)


if __name__ == "__main__":
    unittest.main()
