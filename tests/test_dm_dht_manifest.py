"""AC15: dm 0.6.0 has --no-dht, the same permissions, and `plugin update` from 0.5.0 asks nothing."""

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
COPY_ID = "dev.dock.dmdht"
OLD_SUMMARY = ("Download a URL, a .torrent or a magnet link into the current folder: "
               "dm <url|file.torrent|magnet> [-o name | -d folder] [--no-resume]")


class Manifest06(unittest.TestCase):
    def test_version_and_the_no_dht_bool_input(self):
        m = Manifest.load(DM)
        self.assertEqual(m.version, "0.6.0")
        (command,) = m.commands
        flag = {i.name: i for i in command.inputs}["no_dht"]
        self.assertEqual((flag.type, flag.flag, flag.required), ("bool", "--no-dht", False))
        self.assertIn("[--no-dht]", command.summary)

    def test_the_other_inputs_and_the_permissions_are_unchanged(self):
        m = Manifest.load(DM)
        (command,) = m.commands
        self.assertEqual([i.name for i in command.inputs],
                         ["url", "output", "dir", "stall", "no_resume", "no_dht"])
        self.assertEqual(m.permissions, ["network", "fs:write:cwd"])

    def test_the_peer_id_and_the_extension_handshake_name_0_6_0(self):
        s = session.Session(None, None, lambda *a: None, None, session.Options(dht=False),
                            info_hash=b"\x00" * 20)
        self.addCleanup(s.pool.shutdown, wait=False)
        self.addCleanup(s.reader_pool.shutdown, wait=False)
        self.assertTrue(s.peer_id.startswith(b"-DM0600-"))
        self.assertEqual(bencode.decode(metadata.ext_handshake())[0][b"v"], b"dm 0.6.0")

    def test_the_test_only_address_switches_are_not_inputs_or_manifest_keys(self):
        (command,) = Manifest.load(DM).commands
        text = (DM / "plugin.toml").read_text()
        self.assertNotIn("private", text)
        self.assertNotIn("loopback", text)
        self.assertEqual({i.name for i in command.inputs if "dht" in i.name}, {"no_dht"})


class Update(unittest.TestCase):
    def test_updating_from_0_5_0_asks_nothing(self):
        dockenv.require_runtime()
        tmp = Path(tempfile.mkdtemp(prefix="dock-dmdht-")).resolve()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        old = dockenv.plugin_copy(DM, tmp / "old", COPY_ID)
        text = (old / "plugin.toml").read_text()
        text = re.sub(r'(?m)^version = ".*"$', 'version = "0.5.0"', text, count=1)
        text = re.sub(r'(?m)^summary = ".*"$', f'summary = "{OLD_SUMMARY}"', text, count=1)
        text = text.replace('  { name = "no_dht", type = "bool", flag = "--no-dht" },\n', "")
        (old / "plugin.toml").write_text(text)
        catalog = tmp / "catalog"
        dockenv.plugin_copy(DM, catalog, COPY_ID)
        home = tmp / "home"
        home.mkdir()
        dockenv.write_config(home, dockenv.require_runtime())
        dockenv.add_config(home, f'[catalog]\npaths = ["{catalog}"]\n')
        for version in ("0.5.0", "0.6.0"):
            self.addCleanup(dockenv.remove_image, f"dock/{COPY_ID}:{version}")
        r = cli("plugin", "install", str(old), "--yes", home=home)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = cli("plugin", "update", COPY_ID, home=home)  # no --yes, no terminal: a prompt would refuse
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.strip(), f"updated {COPY_ID} 0.5.0 → 0.6.0")
        self.assertNotIn("adds", r.stderr)


if __name__ == "__main__":
    unittest.main()
