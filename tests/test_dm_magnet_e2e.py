"""dm magnet links end to end (AC14, NFR2, NFR3): dock, the dm image, a test tracker and a real aria2 seeder.

aria2 seeds a three-file torrent and announces it to the tracker container; `dock dm "<magnet>"` finds it
through the magnet's tracker, fetches the metadata from aria2 (BEP 9) and downloads. The `.torrent` dm saves
is then read by aria2 itself. The aria2 interop tests of the .torrent path live in test_dm_torrent_e2e.
"""

import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import torrentkit  # noqa: E402
import test_dm_torrent_e2e as base  # noqa: E402  (by module: the test case itself must not be found here)

ARIA2_IMAGE = base.ARIA2_IMAGE


class MagnetE2E(unittest.TestCase):
    """The pieces of test_dm_torrent_e2e.E2E this needs, without inheriting its tests."""

    @classmethod
    def setUpClass(cls):
        base.E2E.setUpClass.__func__(cls)
        base.E2E.seed_no = getattr(base.E2E, "seed_no", 0)  # the seeding helpers count on the base class

    env = classmethod(base.E2E.env.__func__)
    dock = classmethod(base.E2E.dock.__func__)
    setUp = base.E2E.setUp
    torrent = base.E2E.torrent
    aria2_image = base.E2E.aria2_image
    aria2_seed = base.E2E.aria2_seed

    def magnet(self, t, *trackers):
        return torrentkit.magnet_for(t, trackers or (self.announce,))

    def files(self):
        return [("one.bin", torrentkit.content(1_000_000, "one")),
                ("dir/two.bin", torrentkit.content(1_000_000, "two")),
                ("dir/three.txt", torrentkit.content(97_152, "three"))]  # 2 MiB in all

    def test_dm_downloads_a_magnet_from_an_aria2_seeder_and_saves_a_torrent_aria2_accepts(self):
        self.aria2_image()
        files = self.files()
        t = self.torrent(files, name="interop", piece_length=256 * 1024)
        self.aria2_seed(t)
        r = self.dock("dm", self.magnet(t), cwd=self.cwd)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip().splitlines(), ["saved interop (2.1 MB, 3 files)",
                                                         "saved interop.torrent"])
        self.assertEqual(torrentkit.tree(self.cwd / "interop"), dict(files))  # digests of every file match
        self.assertEqual(sorted(p.name for p in self.cwd.iterdir()), ["interop", "interop.torrent"])
        for secret in (t.info_hash.hex(), self.tracker_ip):  # the link and the peers stay out of the output
            self.assertNotIn(secret, r.stdout + r.stderr)
        saved = self.cwd / "interop.torrent"
        r = subprocess.run([self.rt, "run", "--rm", "--label", "dock-test=dmtorrent", "-v",
                            f"{self.cwd}:/t:ro", ARIA2_IMAGE, "--show-files", "/t/interop.torrent"],
                           capture_output=True, text=True, timeout=120, check=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertRegex(r.stdout, rf"(?i)info hash:\s*{t.info_hash.hex()}")
        self.assertEqual(len(re.findall(r"\|\./interop/", r.stdout)), 3, r.stdout)
        self.assertIn(self.announce, saved.read_bytes().decode("latin-1"))  # kept as given, by design

    def test_a_magnet_with_only_a_dead_tracker_fails_at_the_stall_time(self):
        t = self.torrent(self.files(), name="interop", piece_length=256 * 1024)
        r = self.dock("dm", self.magnet(t, f"http://{self.tracker_ip}:9/announce"), "--stall-seconds", "3",
                      cwd=self.cwd)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertRegex(r.stderr.strip(), r"^\[dev\.dock\.dm\] error: dm: magnet [0-9a-f]{8}…: ")
        self.assertEqual(list(self.cwd.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
