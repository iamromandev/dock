"""dm 0.7.0 adds receive-only PEX: no new input or permission, and `plugin update` from 0.6.0 asks nothing."""

import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT.parent.parent / "plugins" / "dm"))

import dockenv
from src.manifest import Manifest
from dmcore.torrent import bencode, metadata, session
from test_dm_torrent_manifest import cli

DM = ROOT.parent.parent / "plugins" / "dm"
COPY_ID = "dev.dock.dmpex"


class Manifest07(unittest.TestCase):
    def test_version_inputs_and_permissions_have_nothing_new_for_pex(self):
        m = Manifest.load(DM)
        self.assertEqual(m.version, "0.8.0")
        (command,) = m.commands
        self.assertEqual([i.name for i in command.inputs],
                         ["url", "output", "dir", "stall", "no_resume", "no_dht", "list", "only", "skip"])
        self.assertEqual(m.permissions, ["network", "fs:write:cwd"])

    def test_the_peer_id_and_the_extension_handshake_name_0_8_0(self):
        s = session.Session(None, None, lambda *a: None, None, session.Options(dht=False),
                            info_hash=b"\x00" * 20)
        self.addCleanup(s.pool.shutdown, wait=False)
        self.addCleanup(s.reader_pool.shutdown, wait=False)
        self.assertTrue(s.peer_id.startswith(b"-DM0800-"))
        self.assertEqual(bencode.decode(metadata.ext_handshake())[0][b"v"], b"dm 0.8.0")


class Update(unittest.TestCase):
    def test_updating_from_0_6_0_asks_nothing(self):
        dockenv.require_runtime()
        tmp = Path(tempfile.mkdtemp(prefix="dock-dmpex-")).resolve()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        old = dockenv.plugin_copy(DM, tmp / "old", COPY_ID)
        text = (old / "plugin.toml").read_text()
        text = re.sub(r'(?m)^version = ".*"$', 'version = "0.6.0"', text, count=1)
        (old / "plugin.toml").write_text(text)
        catalog = tmp / "catalog"
        dockenv.plugin_copy(DM, catalog, COPY_ID)
        home = tmp / "home"
        home.mkdir()
        dockenv.write_config(home, dockenv.require_runtime())
        dockenv.add_config(home, f'[catalog]\npaths = ["{catalog}"]\n')
        for version in ("0.6.0", "0.8.0"):
            self.addCleanup(dockenv.remove_image, f"dock/{COPY_ID}:{version}")
        r = cli("plugin", "install", str(old), "--yes", home=home)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = cli("plugin", "update", COPY_ID, home=home)  # no --yes, no terminal: a prompt would refuse
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.strip(), f"updated {COPY_ID} 0.6.0 → 0.8.0")
        self.assertNotIn("adds", r.stderr)


if __name__ == "__main__":
    unittest.main()
