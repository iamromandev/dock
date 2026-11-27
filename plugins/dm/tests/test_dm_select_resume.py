"""dm 0.8.0 file selection and resuming: a partial resumes whatever it had picked and whatever is picked now
(files move between their real paths and the hidden side area); a layout that is not ours starts over."""

import shutil
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import test_dm_torrent_local as local
import torrentkit
from dmcore import Cancelled
from dmcore.target import Target
from dmcore.torrent import select, session
from dmcore.torrent.handle import TorrentHandle

PL = 16384
FILES = [
    ("a/1.bin", torrentkit.content(40 * PL, "a1")),
    ("a/2.bin", torrentkit.content(40 * PL, "a2")),
    ("b/3.bin", torrentkit.content(40 * PL, "b3")),
]


class Base(local.Harness):
    """Helpers: run a selection, interrupt it, run again on what it left."""

    def go(self, t, only=None, skip=None, handle=None, resume=True):
        path = t.write_torrent(self.dir / "x.torrent")
        return session.run_torrent(
            path,
            Target(self.cwd, None),
            lambda pct, text: self.progress.append((pct, text)),
            handle or TorrentHandle(),
            local.FAST,
            note=self.notes.append,
            resume=resume,
            selection=select.parse(only, skip),
        )

    def interrupt(self, t, only=None, skip=None, at=30):
        handle, result = TorrentHandle(), {}
        self.notes, self.progress = [], []

        def work():
            try:
                result["ok"] = self.go(t, only, skip, handle)
            except BaseException as e:
                result["err"] = e

        thread = threading.Thread(target=work)
        thread.start()
        self.assertTrue(
            self.wait_for(lambda: any(p >= at and "peer" in x for p, x in list(self.progress)), 20)
        )
        self.assertTrue(handle.abort())
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(result.get("err"), Cancelled, result)

    def torrent_and_swarm(self, delay):
        tr = self.serve_tracker()
        t = self.torrent(FILES, name="t", tr=tr)
        self.first = self.seeder(t, delay=delay)
        self.swarm(tr, self.first)
        return t, tr

    def snapshot(self):
        """Every file under the working folder with its bytes: what a refusal must leave untouched."""
        return {
            p.relative_to(self.cwd).as_posix(): p.read_bytes()
            for p in sorted(self.cwd.rglob("*"))
            if p.is_file()
        }

    def resumes(self, t, tr, only=None, skip=None, expect=None):
        """Run again on the interrupted partial with a fresh seeder: it resumes (never starts over) and the
        result holds exactly the picked files. Returns (the new seeder, the final path)."""
        second = self.seeder(t)
        self.swarm(tr, second)
        self.notes = []
        final, size, nfiles = self.go(t, only, skip)
        self.assertTrue(self.notes and self.notes[0].startswith("resuming from "), self.notes)
        self.assertNotIn("cannot resume: starting over", self.notes)
        self.assertEqual(torrentkit.tree(final), dict(FILES if expect is None else expect))
        self.assertEqual(self.listing(), ["t"])  # no partial, no side area, nothing else
        return second, final


UNALIGNED = [
    ("a/1.bin", torrentkit.content(40 * PL + 7000, "u1")),
    ("a/2.bin", torrentkit.content(40 * PL + 9000, "u2")),
    ("b/3.bin", torrentkit.content(40 * PL + 5000, "u3")),
    ("b/4.bin", b""),
]


class Unaligned(Base):
    """Files that are not multiples of the piece length: boundary pieces are shared with unpicked files, which
    hold their share in the side area. Whatever the selections of the runs, the picked files are exact, no
    piece is fetched twice, and nothing but the picked files is placed."""

    def chain(self, steps, last, expect, drop_side=False):
        tr = self.serve_tracker()
        t = self.torrent(UNALIGNED, name="t", tr=tr)
        seeder = self.seeder(t, delay=0.01)
        self.swarm(tr, seeder)
        for only in steps:
            self.interrupt(t, only, at=50)
        if drop_side:
            shutil.rmtree(next(self.cwd.iterdir()) / ".dm-side", ignore_errors=True)
        self.notes = []
        final, size, nfiles = self.go(t, last)
        self.assertEqual(torrentkit.tree(final), dict(UNALIGNED[i - 1] for i in expect))
        self.assertEqual(nfiles, len(expect))
        self.assertEqual(self.listing(), ["t"])  # no partial, no side area, nothing else
        # a verified piece is never asked for again; only what was in flight when a run was cut (at most
        # `pipeline` pieces per cut) may be, and the half of a file that was verified is far more than that
        again = sum(n - 1 for n in seeder.piece_requests.values())
        self.assertLessEqual(again, len(steps) * local.FAST.pipeline, seeder.piece_requests)
        self.assertEqual([p for p, n in seeder.piece_requests.items() if n > 2], [])
        return seeder

    def test_one_then_one_and_two(self):
        self.chain(["1"], "1-2", [1, 2])

    def test_two_then_only_the_empty_file(self):
        seeder = self.chain(["2"], "4", [4])
        self.assertLess(len(seeder.piece_requests), 40)


CHILD = """
import os, signal, sys, threading
sys.path.insert(0, sys.argv[1])
import dmcore

def notify(method, **p):  # killed outright from inside, mid-download: no handler, no cleanup runs
    if method == "bar" and p.get("id") == "total" and (p.get("percent") or 0) >= 25:
        os.kill(os.getpid(), signal.SIGKILL)

dmcore.run({"url": sys.argv[2], "no_dht": True, "only": sys.argv[3]}, sys.argv[4], notify, threading.Event())
"""


if __name__ == "__main__":
    unittest.main()
