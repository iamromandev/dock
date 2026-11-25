"""dm file selection end to end (AC1, AC4): dock, the real container runtime, the dm image, and a tracker and
a seeder container. `--no-dht` is added by the shared `dock` helper. Fixtures as in test_dm_torrent_e2e."""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import dockenv
import torrentkit
import test_dm_torrent_e2e as base

DM = base.DM
FILES = [("one.bin", torrentkit.content(70_000, "one")), ("sub/two.bin", torrentkit.content(50_000, "two")),
         ("sub/three.txt", torrentkit.content(30_000, "three"))]  # 150000 bytes, pieces of 32768


class SelectE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rt = dockenv.require_runtime()
        dockenv.prebuild_plugins(DM)
        cls.home = dockenv.make_home("dock-dmsel-", DM)
        cls.addClassCleanup(shutil.rmtree, cls.home, ignore_errors=True)
        cls.base = Path(tempfile.mkdtemp(prefix="dock-dmsel-work-")).resolve()
        cls.addClassCleanup(shutil.rmtree, cls.base, ignore_errors=True)
        cid, cls.tracker_ip = base.start_container(cls.rt, "python:3.15.2-slim",
                                                   ["python3", "-u", "/fx/tracker.py"], base.PY_MOUNTS,
                                                   env=base.PY_ENV)
        cls.addClassCleanup(dockenv.kill_container, cid, cls.rt)
        cls.announce = f"http://{cls.tracker_ip}:8000/announce"
        # the shared `seed` counts on the base class (its setUpClass sets it)
        if not hasattr(base.E2E, "seed_no"):
            base.E2E.seed_no = 0

    env = classmethod(base.E2E.env.__func__)
    dock = classmethod(base.E2E.dock.__func__)
    setUp = base.E2E.setUp
    torrent = base.E2E.torrent
    seed = base.E2E.seed
    dm = base.E2E.dm
    found = base.E2E.found

    def torrent_of_three(self):
        return self.torrent(FILES, name="pack", piece_length=32768)

    # AC1
    def test_list_prints_the_files_and_a_total_and_creates_nothing(self):
        t = self.torrent_of_three()  # no seeder: listing a .torrent needs no network
        r = self.dm(t, "--list")
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stdout.splitlines()
        self.assertEqual([l.split()[-1] for l in lines[1:4]], ["one.bin", "sub/two.bin", "sub/three.txt"])
        self.assertEqual(lines[-1], "3 files, 150.0 kB")
        self.assertEqual(self.found(), [])

    def test_list_with_a_pick_keeps_the_numbers_of_the_whole_torrent(self):
        r = self.dm(self.torrent_of_three(), "--list", "--skip", "one.bin")
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stdout.splitlines()
        self.assertEqual([l.split()[0] for l in lines[1:-1]], ["2", "3"])
        self.assertEqual(lines[-1], "2 files, 80.0 kB")

    # AC4
    def test_only_downloads_just_the_picked_files(self):
        t = self.torrent_of_three()
        self.seed(t)
        r = self.dm(t, "--only", "sub/*")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip().splitlines()[-1], "saved pack (80.0 kB, 2 files)")
        self.assertEqual(torrentkit.tree(self.cwd / "pack"), dict(FILES[1:]))
        self.assertEqual(self.found(), ["pack", "pack/sub", "pack/sub/three.txt", "pack/sub/two.bin"])

    def test_a_bad_pick_fails_with_one_line_and_nothing_is_created(self):
        r = self.dm(self.torrent_of_three(), "--only", "*.iso")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stdout, "")
        self.assertIn("no file matches '*.iso'", r.stderr)
        self.assertEqual(self.found(), [])


if __name__ == "__main__":
    unittest.main()
