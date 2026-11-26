"""dm and the DHT end to end (AC2, AC11, AC13): dock, the dm image, a fake DHT network and seeders in containers.

The fixture network (tests/fixtures/torrent/dht.py, 50 nodes) runs in one container on the runtime's default
network, and a seeder (the test fixture or a real aria2) in another. dm gets no tracker and no peer in the
magnet: its DHT node cache is pre-seeded with the fixture nodes, so it bootstraps from there. (The built-in
bootstrap hosts are queried too when the network has internet; nothing depends on them.)

Container networks are private ranges, which the DHT refuses by default. This test installs a COPY of the
plugin under its own id in which `dht_allow_private` defaults to True, edited in the copy's source here. The
shipped plugin has no switch for it at all: nothing in the manifest, the command line or the environment.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(DM := ROOT.parent.parent / "plugins" / "dm"))

import dockenv
import torrentkit
import test_dm_torrent_e2e as base
from dmcore.torrent.dht import cache
from dmcore.torrent.dht.krpc import Node

COPY_ID = "dev.dock.dmdhte2e"
VERSION = "0.8.0"
SWITCH = "    dht_allow_private: bool = False\n"
PY_ENV, PY_MOUNTS = base.PY_ENV, base.PY_MOUNTS
start_container, ARIA2_IMAGE = base.start_container, base.ARIA2_IMAGE


def patched_copy(dest_parent):
    """The plugin under COPY_ID with the private-address switch on, in the copy only. Asserts it found it."""
    copy = dockenv.plugin_copy(DM, dest_parent, COPY_ID)
    path = copy / "dmcore" / "torrent" / "session.py"
    text = path.read_text()
    assert text.count(SWITCH) == 1, "the dht_allow_private option moved: update this test"
    path.write_text(text.replace(SWITCH, SWITCH.replace("False", "True")))
    return copy


class DhtE2E(unittest.TestCase):
    dht = True  # for base.E2E.dock: do not add --no-dht

    @classmethod
    def setUpClass(cls):
        cls.rt = dockenv.require_runtime()
        cls.base = Path(tempfile.mkdtemp(prefix="dock-dmdht-work-")).resolve()
        cls.addClassCleanup(shutil.rmtree, cls.base, ignore_errors=True)
        plugin = patched_copy(cls.base / "plugin")
        cls.addClassCleanup(dockenv.remove_image, f"dock/{COPY_ID}:{VERSION}")
        dockenv.prebuild_plugins(plugin)
        cls.home = dockenv.make_home("dock-dmdht-", plugin)
        cls.addClassCleanup(shutil.rmtree, cls.home, ignore_errors=True)
        cls.data = cls.home / "data" / COPY_ID
        cls.n = 0

    env = classmethod(base.E2E.env.__func__)
    dock = classmethod(base.E2E.dock.__func__)
    aria2_image = base.E2E.aria2_image

    def setUp(self):
        self.cwd = Path(tempfile.mkdtemp(prefix="w-", dir=self.base))

    def files(self):
        return [("one.bin", torrentkit.content(300_000, "one")), ("dir/two.bin", torrentkit.content(200_000, "two"))]

    def torrent(self, name="pack"):
        return torrentkit.make_torrent(self.files(), 65536, name=name)  # no tracker at all

    def seeder(self, t, *flags):
        """A fixture seeder container for t; its IP."""
        DhtE2E.n += 1
        root = self.base / f"seed-{DhtE2E.n}"
        (root / "data").mkdir(parents=True)
        t.write_torrent(root / "x.torrent")
        torrentkit.write_files(root / "data", t)
        cid, ip = start_container(self.rt, "python:3.15.2-slim",
                                  ["python3", "-u", "/fx/peer.py", "seeder", "--torrent", "/seed/x.torrent",
                                   "--data", "/seed/data", *flags], [*PY_MOUNTS, (root, "/seed", "ro")], env=PY_ENV)
        self.addCleanup(dockenv.kill_container, cid, self.rt)
        return ip

    def network(self, t=None, peers=()):
        """The fixture DHT in a container: (container id, [(ip, port)] of its nodes). With t and peers, the
        nodes nearest to t's info-hash name those peers."""
        args = ["python3", "-u", "/fx/dht.py", "--nodes", "50", "--port-base", "7000", "--advertise", "auto"]
        if t is not None and peers:
            args += ["--peers", ",".join(f"{h}:{p}" for h, p in peers), "--info-hash", t.info_hash.hex()]
        cid, _ = start_container(self.rt, "python:3.15.2-slim", args, PY_MOUNTS, env=PY_ENV, ready="dht nodes:")
        self.addCleanup(dockenv.kill_container, cid, self.rt)
        logs = subprocess.run([self.rt, "logs", cid], capture_output=True, text=True, timeout=60, check=False)
        line = next(x for x in (logs.stdout + logs.stderr).splitlines() if x.startswith("dht nodes:"))
        addrs = [(a.rsplit(":", 1)[0], int(a.rsplit(":", 1)[1])) for a in line.split()[2:]]
        self.assertEqual(len(addrs), 50)
        return cid, addrs

    def seed_cache(self, addrs):
        """Pre-seed dm's node cache: how the run bootstraps from this network."""
        self.data.mkdir(parents=True, exist_ok=True)
        cache.save(self.data / cache.CACHE_NAME, [Node(os.urandom(20), a) for a in addrs])
        self.assertEqual(len(cache.load(self.data / cache.CACHE_NAME, allow_private=True)), 50)

    def magnet(self, t):
        return torrentkit.magnet_for(t)  # the hash and a name: no tracker, no peer

    def assertDownloaded(self, r, t):
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        self.assertEqual(r.stdout.strip().splitlines(), [f"saved {t.name} (500.0 kB, 2 files)",
                                                         f"saved {t.name}.torrent"])
        self.assertEqual(torrentkit.tree(self.cwd / t.name), dict(self.files()))
        self.assertEqual(sorted(p.name for p in self.cwd.iterdir()), [t.name, f"{t.name}.torrent"])

    # AC2
    def test_a_magnet_with_only_the_hash_downloads_through_the_dht_and_saves_the_torrent(self):
        t = self.torrent()
        ip = self.seeder(t)
        _, addrs = self.network(t, [(ip, 6881)])
        self.seed_cache(addrs)
        r = self.dock("dm", self.magnet(t), cwd=self.cwd)
        self.assertDownloaded(r, t)
        self.assertNotIn(t.info_hash.hex(), r.stdout + r.stderr)
        self.assertNotIn(ip, r.stdout)
        saved = cache.load(self.data / cache.CACHE_NAME, allow_private=True)
        self.assertTrue(0 < len(saved) <= cache.MAX_CACHE_NODES)  # the nodes that answered this run
        self.assertEqual([p.name for p in self.data.iterdir()], [cache.CACHE_NAME])

    # AC11
    def test_verbose_output_and_the_plugin_log_have_no_address_outside_the_peers_table(self):
        t = self.torrent("verbose")
        ip = self.seeder(t, "--delay", "0.1")  # long enough for -v to print more than the peers table
        _, addrs = self.network(t, [(ip, 6881)])
        self.seed_cache(addrs)
        logs = self.home / "logs"
        before = {p: p.stat().st_size for p in logs.glob("*.log")} if logs.is_dir() else {}
        r = self.dock("-v", "dm", self.magnet(t), cwd=self.cwd)
        self.assertDownloaded(r, t)  # a trackerless magnet: the DHT found the seeder
        address = r"\d+\.\d+\.\d+\.\d+"
        outside = re.sub(r"(?m)^peer +client +down +up\n(?:.+\n?)*", "", r.stderr)
        self.assertIsNone(re.search(address, outside), outside)
        written = "".join(p.read_bytes()[before.get(p, 0):].decode("utf-8", "replace") for p in logs.glob("*.log"))
        self.assertIn("start", written)  # something was written to it
        self.assertIsNone(re.search(address, written), written)

    # AC13
    def test_an_aria2_seeder_known_only_to_the_dht_serves_a_trackerless_magnet(self):
        self.aria2_image()
        t = self.torrent("interop")
        cid, addrs = self.network()  # knows nobody yet: aria2 announces to it
        DhtE2E.n += 1
        root = self.base / f"aria2-{DhtE2E.n}"
        (root / "data").mkdir(parents=True)
        t.write_torrent(root / "x.torrent")
        torrentkit.write_files(root / "data", t)
        entry = addrs[0]
        acid, _ = start_container(
            self.rt, ARIA2_IMAGE,
            ["--enable-dht=true", "--dht-listen-port=6881", f"--dht-entry-point={entry[0]}:{entry[1]}",
             "--dht-file-path=/tmp/dht.dat", "--bt-enable-lpd=false", "--enable-peer-exchange=false",
             "--listen-port=6881", "--seed-ratio=0.0", "--bt-seed-unverified", "--console-log-level=info",
             "-d", "/seed/data", "/seed/x.torrent"],
            [(root, "/seed", "rw")], ready="listening on TCP", wait=40)
        self.addCleanup(dockenv.kill_container, acid, self.rt)
        end = time.monotonic() + 90
        while True:  # aria2 has announced when a fixture node says it stored the announce
            out = subprocess.run([self.rt, "logs", cid], capture_output=True, text=True, timeout=60, check=False)
            if f"announce {t.info_hash.hex()[:8]}" in out.stdout + out.stderr:
                break
            self.assertLess(time.monotonic(), end, "aria2 never announced to the fixture DHT")
            time.sleep(1)
        self.seed_cache(addrs)
        r = self.dock("dm", self.magnet(t), cwd=self.cwd)
        self.assertDownloaded(r, t)


if __name__ == "__main__":
    unittest.main()
