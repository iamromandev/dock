"""dm 0.8.0 file selection and resuming: a partial resumes whatever it had picked and whatever is picked now
(files move between their real paths and the hidden side area); a layout that is not ours starts over."""

import os
import shutil
import signal
import subprocess
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "plugin" / "dm"))
sys.path.insert(0, str(ROOT / "tests"))

import test_dm_torrent_local as local  # noqa: E402
import torrentkit  # noqa: E402
from dmcore import Cancelled  # noqa: E402
from dmcore.target import DmError, Target  # noqa: E402
from dmcore.torrent import select, session  # noqa: E402
from dmcore.torrent.handle import TorrentHandle  # noqa: E402

PL = 16384
FILES = [("a/1.bin", torrentkit.content(40 * PL, "a1")), ("a/2.bin", torrentkit.content(40 * PL, "a2")),
         ("b/3.bin", torrentkit.content(40 * PL, "b3"))]


class Base(local.Harness):
    """Helpers: run a selection, interrupt it, run again on what it left."""

    def go(self, t, only=None, skip=None, handle=None, resume=True):
        path = t.write_torrent(self.dir / "x.torrent")
        return session.run_torrent(path, Target(self.cwd, None),
                                   lambda pct, text: self.progress.append((pct, text)),
                                   handle or TorrentHandle(), local.FAST, note=self.notes.append,
                                   resume=resume, selection=select.parse(only, skip))

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
        self.assertTrue(self.wait_for(
            lambda: any(p >= at and "peer" in x for p, x in list(self.progress)), 20))
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
        return {p.relative_to(self.cwd).as_posix(): p.read_bytes() for p in sorted(self.cwd.rglob("*"))
                if p.is_file()}

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


class Interrupted(Base):
    def test_a_selection_on_a_full_partial_resumes_it_and_moves_unpicked_files_aside(self):
        t, tr = self.torrent_and_swarm(0.01)
        self.interrupt(t)  # the full layout, like dm 0.7.0 leaves
        second, final = self.resumes(t, tr, "a/*", expect=FILES[:2])
        self.assertLess(sum(second.piece_requests.values()), 80)  # the verified pieces were kept

    def test_a_magnet_with_a_selection_on_a_full_partial_resumes_it(self):
        from dmcore.torrent import magnet

        t, tr = self.torrent_and_swarm(0.01)
        self.interrupt(t)
        seeder = self.seeder(t)
        link = torrentkit.magnet_for(t, peers=[("127.0.0.1", seeder.port)])
        notes = []
        final, size, nfiles, info = session.run_magnet(magnet.parse(link), Target(self.cwd, None),
                                                       lambda p, x: None, TorrentHandle(), local.FAST,
                                                       note=notes.append, selection=select.parse("a/*", None))
        self.assertTrue(notes and notes[0].startswith("resuming from "), notes)
        self.assertEqual(torrentkit.tree(final), dict(FILES[:2]))
        self.assertEqual((size, nfiles), (80 * PL, 2))

    def test_a_selected_partial_resumes_with_the_same_selection(self):
        t, tr = self.torrent_and_swarm(0.01)
        self.interrupt(t, "a/*")
        self.assertEqual(len(self.listing()), 1)
        second, final = self.resumes(t, tr, "a/*", expect=FILES[:2])
        self.assertLess(sum(second.piece_requests.values()), 80)

    def test_a_selected_partial_resumes_with_a_different_selection(self):
        t, tr = self.torrent_and_swarm(0.01)
        self.interrupt(t, "a/1.bin")
        first = self.first
        second, final = self.resumes(t, tr, "b/*", expect=FILES[2:])
        self.assertLess(sum(second.piece_requests.values()), 40 + 5)  # b/3.bin only, plus boundary pieces
        self.assertEqual({p: n for p, n in second.piece_requests.items() if n > 1}, {})
        self.assertEqual(set(second.piece_requests) & set(first.piece_requests), set())  # none fetched twice

    def test_a_selected_partial_resumes_with_no_selection_and_keeps_what_it_has(self):
        t, tr = self.torrent_and_swarm(0.01)
        self.interrupt(t, "a/*")
        second, final = self.resumes(t, tr)
        self.assertLess(sum(second.piece_requests.values()), 120)

    def test_a_partial_cut_short_after_the_side_area_was_removed_resumes(self):
        t, tr = self.torrent_and_swarm(0.01)
        self.interrupt(t, "a/1.bin")
        part = next(self.cwd.iterdir())
        side = part / ".dm-side"
        if side.exists():
            shutil.rmtree(side)
        self.resumes(t, tr, "a/1.bin", expect=FILES[:1])

    def test_a_chain_of_interruptions_with_changing_selections_ends_with_the_right_files(self):
        t, tr = self.torrent_and_swarm(0.02)
        self.interrupt(t, "a/*", at=15)
        self.interrupt(t, "b/*", at=15)
        self.interrupt(t, None, at=15)
        self.interrupt(t, skip="a/2.bin", at=15)
        self.resumes(t, tr, "a/2.bin,b/3.bin", expect=FILES[1:])

    def hostile(self, build, only="a/*"):
        """A partial that looks like ours but is not: it is thrown away, and nothing outside it is touched."""
        t, tr = self.torrent_and_swarm(0.01)
        self.interrupt(t, only)
        outside = self.dir / "outside"
        outside.mkdir()
        (outside / "keep").write_bytes(b"secret")
        build(next(self.cwd.iterdir()), outside)
        self.swarm(tr, self.seeder(t))
        self.notes = []
        final, size, nfiles = self.go(t, only)
        self.assertEqual(self.notes, ["cannot resume: starting over"])
        self.assertEqual(torrentkit.tree(final), dict(FILES[:2]))
        self.assertEqual({p.name for p in outside.iterdir()}, {"keep"})
        self.assertEqual((outside / "keep").read_bytes(), b"secret")

    def test_a_symlink_at_a_real_path_starts_over_without_touching_its_target(self):
        def build(part, outside):
            (part / "a" / "1.bin").unlink()
            (part / "a" / "1.bin").symlink_to(outside / "keep")
        self.hostile(build)

    def test_a_symlink_at_the_side_area_starts_over_without_touching_its_target(self):
        def build(part, outside):
            shutil.rmtree(part / ".dm-side", ignore_errors=True)
            (part / ".dm-side").symlink_to(outside)
        self.hostile(build)

    def test_a_symlinked_side_folder_starts_over_without_touching_its_target(self):
        def build(part, outside):
            (part / ".dm-side").mkdir(exist_ok=True)  # the pieces line up with the files: none made one
            (part / ".dm-side" / "b").symlink_to(outside)
        self.hostile(build)

    def test_a_file_in_both_places_starts_over(self):
        def build(part, outside):
            for base in (part, part / ".dm-side"):
                (base / "b").mkdir(parents=True, exist_ok=True)
                (base / "b" / "3.bin").write_bytes(bytes(40 * PL))
        self.hostile(build)

    def test_an_extra_entry_in_the_side_area_starts_over(self):
        def build(part, outside):
            (part / ".dm-side").mkdir(exist_ok=True)
            (part / ".dm-side" / "stray").write_bytes(b"x")
        self.hostile(build)

    def test_a_fifo_starts_over(self):
        def build(part, outside):
            (part / "a" / "1.bin").unlink()
            os.mkfifo(part / "a" / "1.bin")
        self.hostile(build)

    def test_no_resume_with_a_selection_discards_the_partial_and_starts_fresh(self):
        t, tr = self.torrent_and_swarm(0.01)
        self.interrupt(t)
        self.swarm(tr, self.seeder(t))
        self.notes = []
        final, size, nfiles = self.go(t, "a/*", resume=False)
        self.assertEqual(torrentkit.tree(final), dict(FILES[:2]))
        self.assertEqual(self.listing(), ["t"])

    def test_a_partial_that_is_not_a_layout_of_this_torrent_still_starts_over(self):
        t, tr = self.torrent_and_swarm(0.01)
        self.interrupt(t)
        part = next(self.cwd.iterdir())
        (part / "stray").write_bytes(b"x")
        self.swarm(tr, self.seeder(t))
        self.notes = []
        final, size, nfiles = self.go(t, "a/*")
        self.assertEqual(self.notes, ["cannot resume: starting over"])
        self.assertEqual(torrentkit.tree(final), dict(FILES[:2]))

    def test_a_download_without_a_selection_still_resumes(self):
        t, tr = self.torrent_and_swarm(0.01)
        self.interrupt(t)
        second = self.seeder(t)
        self.swarm(tr, second)
        self.notes, self.progress = [], []
        final, size, nfiles = self.go(t)
        self.assertEqual(len(self.notes), 1)
        self.assertTrue(self.notes[0].startswith("resuming from "), self.notes)
        self.assertEqual(torrentkit.tree(final), dict(FILES))
        self.assertLess(sum(second.piece_requests.values()), 120)

    def test_picking_every_file_resumes_a_full_partial(self):
        t, tr = self.torrent_and_swarm(0.01)
        self.interrupt(t)
        self.swarm(tr, self.seeder(t))
        self.notes = []
        final, size, nfiles = self.go(t, "*")
        self.assertTrue(self.notes[0].startswith("resuming from "), self.notes)
        self.assertEqual(torrentkit.tree(final), dict(FILES))


UNALIGNED = [("a/1.bin", torrentkit.content(40 * PL + 7000, "u1")),
             ("a/2.bin", torrentkit.content(40 * PL + 9000, "u2")),
             ("b/3.bin", torrentkit.content(40 * PL + 5000, "u3")), ("b/4.bin", b"")]


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

    def test_one_and_two_then_one(self):
        self.chain(["1-2"], "1", [1])

    def test_two_then_everything(self):
        self.chain(["2"], None, [1, 2, 3, 4])

    def test_three_and_the_empty_file_then_the_empty_file_and_one(self):
        self.chain(["3,4"], "4,1", [1, 4])

    def test_two_with_the_side_area_gone_then_one_to_three(self):
        self.chain(["2"], "1-3", [1, 2, 3], drop_side=True)

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


class HardKill(Base):
    """NFR3: a process killed outright (no cleanup of any kind) mid-download with a selection leaves a partial
    the next run resumes into exactly the picked files."""

    def test_a_sigkilled_selected_download_resumes_and_completes(self):
        t, tr = self.torrent_and_swarm(0.05)
        t.write_torrent(self.cwd / "x.torrent")
        cmd = [sys.executable, "-I", "-c", CHILD, str(ROOT / "plugin" / "dm"), "x.torrent", "a/*",
               str(self.cwd)]
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=self.cwd)
        self.addCleanup(lambda: (p.poll() is None and p.kill(), p.communicate()))
        _, err = p.communicate(timeout=60)
        self.assertEqual(p.returncode, -signal.SIGKILL, err)
        asked = dict(self.first.piece_requests)
        self.assertTrue(any(x.name.endswith(".dm-part") for x in self.cwd.iterdir()))
        self.notes = []
        torrent = self.cwd / "x.torrent"
        final, size, nfiles = session.run_torrent(torrent, Target(self.cwd, None), lambda pct, text: None,
                                                  TorrentHandle(), local.FAST, note=self.notes.append,
                                                  selection=select.parse("a/*", None))
        self.assertTrue(self.notes and self.notes[0].startswith("resuming from "), self.notes)
        self.assertEqual(torrentkit.tree(final), dict(FILES[:2]))
        self.assertEqual(sorted(x.name for x in self.cwd.iterdir()), ["t", "x.torrent"])
        again = sum(n - 1 for n in self.first.piece_requests.values())
        self.assertLessEqual(again, local.FAST.pipeline, (asked, self.first.piece_requests))


if __name__ == "__main__":
    unittest.main()
