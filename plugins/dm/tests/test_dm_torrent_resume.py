"""Resume for torrents (AC8-AC10, AC12, AC14, AC17, NFR1, NFR2): re-check, reuse, keep and delete.

In one process: the fixture tracker and seeders of test_dm_torrent_local on 127.0.0.1, no container needed.
"""

import hashlib
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import torrentkit
from dmcore import Cancelled
from dmcore.target import DmError, Target
from dmcore.torrent import layout, metainfo, session
from dmcore.torrent.handle import TorrentHandle
from dmcore.torrent.recheck import recheck
from dmcore.torrent.storage import Storage
from test_dm_torrent_local import FAST, PL, Harness


class Recheck(unittest.TestCase):
    """AC8, AC14: only pieces that pass their hash count; Ctrl-C keeps the data."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="dm-recheck-")).resolve()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def open(self, t):
        meta = metainfo.load(t.write_torrent(self.dir / "t.torrent"))
        specs = layout.plan_files(meta)
        st = Storage(self.dir / "part", specs, meta.piece_length, meta.total, single=True)
        st.prepare()
        self.addCleanup(st.close)
        return meta, st

    def test_a_corrupt_or_missing_piece_is_not_counted(self):
        t = torrentkit.make_torrent([("a.bin", torrentkit.content(6 * PL + 10))], PL, name="a.bin")
        meta, st = self.open(t)
        for i in range(7):
            if i not in (2, 6):  # piece 2 stays zeros (missing), piece 6 (the short one) is also absent
                st.write_piece(i, t.blob[i * PL:(i + 1) * PL])
        st.write_piece(4, b"\x01" * PL)  # present but wrong
        reports = []
        got = recheck(st, meta, lambda pct, text: reports.append((pct, text)), TorrentHandle())
        self.assertEqual(got, {0, 1, 3, 5})
        self.assertTrue(reports)
        self.assertEqual({text for _, text in reports}, {"checking existing data"})
        self.assertTrue(all(0 <= pct <= 99 for pct, _ in reports))

    def test_the_short_last_piece_counts_when_it_matches(self):
        t = torrentkit.make_torrent([("a.bin", torrentkit.content(2 * PL + 10))], PL, name="a.bin")
        meta, st = self.open(t)
        st.write_piece(2, t.blob[2 * PL:])
        self.assertEqual(recheck(st, meta, lambda *a: None, TorrentHandle()), {2})

    def test_an_aborted_handle_stops_the_check_with_cancelled_and_keeps_the_data(self):
        t = torrentkit.make_torrent([("a.bin", torrentkit.content(8 * PL))], PL, name="a.bin")
        meta, st = self.open(t)
        st.write_piece(0, t.blob[:PL])
        handle = TorrentHandle()
        seen = []

        def report(pct, text):
            seen.append(pct)
            handle.aborted = True

        with self.assertRaises(Cancelled):
            recheck(st, meta, report, handle)
        self.assertEqual(st.read_block(0, 0, PL), t.blob[:PL])

    def test_checking_a_512_mib_partial_runs_at_200_mb_per_s_or_more(self):
        size = 512 * 1024 * 1024
        t, path = torrentkit.make_big_torrent(self.dir, "big.bin", size, 1024 * 1024)
        meta = metainfo.load(t.write_torrent(self.dir / "big.torrent"))
        st = Storage(path, layout.plan_files(meta), meta.piece_length, meta.total, single=True)
        st.open_existing()
        self.addCleanup(st.close)
        start = time.monotonic()
        got = recheck(st, meta, lambda *a: None, TorrentHandle())
        rate = size / (time.monotonic() - start) / 1e6
        print(f"\n[perf] recheck of 512 MiB: {rate:.0f} MB/s", file=sys.stderr)
        self.assertEqual(len(got), 512)
        self.assertGreaterEqual(rate, 200)


class Resume(Harness):
    """AC8-AC10, AC12, AC17, NFR1, NFR2: run_torrent keeps, reuses and deletes partials."""

    def part_path(self, t, fixed_name=None):
        from dmcore import resume as r

        target = Target(self.cwd, fixed_name)
        meta = metainfo.load(t.write_torrent(self.dir / "k.torrent"))
        base = fixed_name or layout.item_name(meta)
        return r.paths(self.cwd, base, r.torrent_key(meta.info_hash, target)).part

    def go(self, t, fixed_name=None, handle=None, resume=True, opts=FAST, reset=True):
        """run_torrent with the resume arguments; the notes and progress it sent are kept in self."""
        path = t.write_torrent(self.dir / "x.torrent")
        if reset:
            self.notes, self.progress = [], []
        return session.run_torrent(path, Target(self.cwd, fixed_name),
                                   lambda pct, text: self.progress.append((pct, text)),
                                   handle or TorrentHandle(), opts, None, resume=resume,
                                   note=self.notes.append)

    def interrupt(self, t, seeder, at=40, fixed_name=None):
        """Run until `at` percent are verified, then abort. Returns the exception the run ended with."""
        handle, result = TorrentHandle(), {}
        self.notes, self.progress = [], []

        def work():
            try:
                result["ok"] = self.go(t, fixed_name, handle, reset=False)
            except BaseException as e:
                result["err"] = e

        thread = threading.Thread(target=work)
        thread.start()
        self.assertTrue(self.wait_for(lambda: any(p >= at and "peer" in x for p, x in list(self.progress)), 20))
        start = time.monotonic()
        self.assertTrue(handle.abort())
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertLess(time.monotonic() - start, 2)
        self.assertIsInstance(result.get("err"), Cancelled, result)
        return result["err"]

    def on_disk(self, t, part):
        """The piece indexes a partial holds that match the torrent (computed here, not by dm)."""
        data = Path(part).read_bytes() if not t.multi else b"".join(
            (Path(part) / rel).read_bytes() for rel, _ in t.files)
        return {i for i in range(-(-len(data) // t.piece_length))
                if data[i * t.piece_length:(i + 1) * t.piece_length]
                == t.blob[i * t.piece_length:(i + 1) * t.piece_length]
                and any(data[i * t.piece_length:(i + 1) * t.piece_length])}

    def slow_swarm(self, tr, t, delay=0.01):
        seeder = self.seeder(t, delay=delay)
        self.swarm(tr, seeder)
        return seeder

    def test_cancel_keeps_the_partial_under_its_key_name_and_drops_the_lock(self):
        tr = self.serve_tracker()
        t = self.single(size=64 * PL, tr=tr, piece_length=PL)
        self.slow_swarm(tr, t)
        self.interrupt(t, None)
        self.assertEqual(self.listing(), [self.part_path(t).name])
        self.assertTrue(self.part_path(t).name.startswith(".a.bin."))

    def test_single_file_resumes_without_asking_for_verified_pieces(self):
        tr = self.serve_tracker()
        t = self.single(size=64 * PL, tr=tr, piece_length=PL)
        self.slow_swarm(tr, t)
        self.interrupt(t, None)
        have = self.on_disk(t, self.part_path(t))
        self.assertTrue(0 < len(have) < 64)
        second = self.seeder(t)
        self.swarm(tr, second)
        final, size, nfiles = self.go(t)
        self.assertEqual(final.read_bytes(), t.blob)
        self.assertEqual(self.listing(), ["a.bin"])
        self.assertEqual(set(second.piece_requests) & have, set())
        self.assertEqual(set(second.piece_requests), set(range(64)) - have)
        self.assertEqual(self.notes, [f"resuming from {dmfmt(len(have) * PL)} of {dmfmt(64 * PL)}"])
        self.assertIn("checking existing data", [text for _, text in self.progress])

    def test_multi_file_resumes_and_matches_the_source(self):
        tr = self.serve_tracker()
        files = [("x/one.bin", torrentkit.content(20 * PL + 5, "1")), ("two.bin", torrentkit.content(30 * PL, "2")),
                 ("empty", b"")]
        t = self.torrent(files, name="set", tr=tr)
        self.slow_swarm(tr, t)
        self.interrupt(t, None)
        have = self.on_disk(t, self.part_path(t))
        self.assertTrue(have)
        second = self.seeder(t)
        self.swarm(tr, second)
        final, size, nfiles = self.go(t)
        self.assertEqual(torrentkit.tree(final), dict(files))
        self.assertEqual(set(second.piece_requests) & have, set())
        self.assertEqual(self.listing(), ["set"])

    def test_two_interruptions_then_a_complete_download_is_byte_identical(self):
        tr = self.serve_tracker()
        t = self.single(size=80 * PL + 123, tr=tr, piece_length=PL)
        self.slow_swarm(tr, t)
        self.interrupt(t, None, at=25)
        first = self.on_disk(t, self.part_path(t))
        self.slow_swarm(tr, t)
        self.interrupt(t, None, at=60)
        second_have = self.on_disk(t, self.part_path(t))
        self.assertGreater(len(second_have), len(first))
        third = self.seeder(t)
        self.swarm(tr, third)
        final, *_ = self.go(t)
        self.assertEqual(hashlib.sha256(final.read_bytes()).hexdigest(), hashlib.sha256(t.blob).hexdigest())
        self.assertEqual(set(third.piece_requests) & second_have, set())

    def test_a_fully_downloaded_partial_finishes_without_any_request(self):
        tr = self.serve_tracker()
        t = self.single(size=8 * PL, tr=tr, piece_length=PL)
        part = self.part_path(t)
        part.write_bytes(t.blob)
        seeder = self.seeder(t)
        self.swarm(tr, seeder)
        final, *_ = self.go(t)
        self.assertEqual(final.read_bytes(), t.blob)
        self.assertEqual(seeder.requests, 0)
        self.assertEqual(self.listing(), ["a.bin"])

    def test_the_bitfield_sent_to_peers_has_the_verified_pieces(self):
        t = self.single(size=8 * PL, piece_length=PL)
        meta = metainfo.load(t.write_torrent(self.dir / "b.torrent"))
        s = session.Session(meta, None, lambda *a: None, TorrentHandle(), FAST, have={0, 3, 7})
        self.assertEqual(s.bitfield(), bytes([0b10010001]))
        self.assertEqual(s.picker.verified_bytes(), 3 * PL)

    def test_a_partial_that_does_not_match_is_discarded_and_nothing_outside_is_written(self):
        tr = self.serve_tracker()
        t = self.single(size=16 * PL, tr=tr, piece_length=PL)
        outside = self.dir / "outside"
        outside.write_bytes(b"precious")
        os.symlink(outside, self.part_path(t))
        self.swarm(tr, self.seeder(t))
        final, *_ = self.go(t)
        self.assertEqual(final.read_bytes(), t.blob)
        self.assertEqual(outside.read_bytes(), b"precious")
        self.assertEqual(self.notes, ["cannot resume: starting over"])
        self.assertEqual(self.listing(), ["a.bin"])

    def test_a_folder_partial_with_an_extra_file_or_a_symlinked_file_starts_over(self):
        for hostile in ("extra", "link"):
            with self.subTest(hostile):
                tr = self.serve_tracker()
                files = [("a", torrentkit.content(4 * PL, "a")), ("b", torrentkit.content(4 * PL, "b"))]
                t = self.torrent(files, name="set", tr=tr, piece_length=PL)
                part = self.part_path(t)
                part.mkdir()
                outside = self.dir / f"outside-{hostile}"
                outside.write_bytes(b"precious")
                (part / "a").write_bytes(files[0][1])
                if hostile == "extra":
                    (part / "b").write_bytes(files[1][1])
                    (part / "extra").write_bytes(b"x")
                else:
                    os.symlink(outside, part / "b")
                self.swarm(tr, self.seeder(t))
                final, *_ = self.go(t)
                self.assertEqual(torrentkit.tree(final), dict(files))
                self.assertEqual(outside.read_bytes(), b"precious")
                self.assertEqual(self.notes, ["cannot resume: starting over"])
                shutil.rmtree(final)

    def test_no_resume_discards_the_partial_and_asks_for_every_piece(self):
        tr = self.serve_tracker()
        t = self.single(size=32 * PL, tr=tr, piece_length=PL)
        self.slow_swarm(tr, t)
        self.interrupt(t, None)
        second = self.seeder(t)
        self.swarm(tr, second)
        final, *_ = self.go(t, resume=False)
        self.assertEqual(final.read_bytes(), t.blob)
        self.assertEqual(set(second.piece_requests), set(range(32)))
        self.assertEqual(self.notes, [])
        self.assertEqual(self.listing(), ["a.bin"])

    def test_a_success_leaves_no_dm_items(self):
        tr = self.serve_tracker()
        t = self.torrent([("a", torrentkit.content(3 * PL)), ("b", b"x")], name="set", tr=tr, piece_length=PL)
        self.swarm(tr, self.seeder(t))
        self.go(t)
        self.assertEqual(self.listing(), ["set"])
        self.assertEqual([p for p in self.cwd.rglob(".*")], [])

    def test_a_random_named_partial_from_0_2_0_is_left_alone(self):
        tr = self.serve_tracker()
        t = self.single(size=4 * PL, tr=tr, piece_length=PL)
        old = self.cwd / ".a.bin.0123456789abcdef.dm-part"
        old.write_bytes(t.blob)
        seeder = self.seeder(t)
        self.swarm(tr, seeder)
        self.go(t)
        self.assertEqual(seeder.requests, 4)
        self.assertEqual(old.read_bytes(), t.blob)
        self.assertEqual(self.listing(), [old.name, "a.bin"])

    def test_a_different_dash_o_name_uses_a_different_partial(self):
        tr = self.serve_tracker()
        t = self.single(size=64 * PL, tr=tr, piece_length=PL)
        self.slow_swarm(tr, t)
        self.interrupt(t, None)
        kept = self.part_path(t)
        second = self.seeder(t)
        self.swarm(tr, second)
        self.go(t, fixed_name="other.bin")
        self.assertEqual(set(second.piece_requests), set(range(64)))
        self.assertEqual(self.listing(), [kept.name, "other.bin"])

    def test_ctrl_c_during_the_check_keeps_the_data(self):
        tr = self.serve_tracker()
        t = self.single(size=16 * PL, tr=tr, piece_length=PL)
        self.part_path(t).write_bytes(t.blob)
        handle = TorrentHandle()
        real = session.recheck

        def abort_then_check(storage, meta, report, h):
            h.abort()
            return real(storage, meta, report, h)

        with mock.patch.object(session, "recheck", abort_then_check):
            with self.assertRaises(Cancelled):
                self.go(t, handle=handle)
        self.assertEqual(self.part_path(t).read_bytes(), t.blob)
        self.assertEqual(self.listing(), [self.part_path(t).name])

    def test_a_second_run_of_the_same_download_fails_at_once_and_disturbs_nothing(self):
        from dmcore import resume as r

        tr = self.serve_tracker()
        t = self.single(size=4 * PL, tr=tr, piece_length=PL)
        part = self.part_path(t)
        part.write_bytes(t.blob)
        meta = metainfo.load(t.write_torrent(self.dir / "k.torrent"))
        lock = r.Lock(r.torrent_lock(self.cwd, r.torrent_key(meta.info_hash, Target(self.cwd, None))))
        lock.acquire()
        self.addCleanup(lock.release, True)
        with self.assertRaises(DmError) as cm:
            self.go(t)
        self.assertEqual(str(cm.exception), "already being downloaded in this folder")
        self.assertEqual(part.read_bytes(), t.blob)

    def test_a_retryable_failure_with_nothing_verified_leaves_nothing(self):
        # R3: the flag says "retryable"; with zero verified bytes there is nothing worth keeping
        for label, make_err in (
            ("no peers", None),
            ("enospc", OSError(28, "No space left on device")),
            ("eacces", OSError(13, "Permission denied")),
        ):
            with self.subTest(label):
                tr = self.serve_tracker(mode="empty") if label == "no peers" else self.serve_tracker()
                t = self.single(size=4 * PL, tr=tr, piece_length=PL)
                if make_err is not None:
                    self.swarm(tr, self.seeder(t))
                patch = (mock.patch.object(session.Storage, "write_piece", side_effect=make_err)
                         if make_err else mock.MagicMock())
                with patch:
                    with self.assertRaises(DmError) as cm:
                        self.go(t, opts=session.Options(dht=False, stall_s=1))
                self.assertEqual(cm.exception.keep, label != "eacces")
                self.assertEqual(self.listing(), [])
                shutil.rmtree(self.cwd)
                self.cwd.mkdir()

    def test_a_retryable_failure_after_a_piece_was_verified_in_this_run_keeps_the_partial(self):
        tr = self.serve_tracker()
        t = self.single(size=8 * PL, tr=tr, piece_length=PL)
        self.swarm(tr, self.seeder(t))
        real, calls = session.Storage.write_piece, []

        def flaky(st, index, data):
            calls.append(index)
            if len(calls) > 3:
                time.sleep(0.5)  # let the first pieces be counted as verified
                raise OSError(28, "No space left on device")
            return real(st, index, data)

        with mock.patch.object(session.Storage, "write_piece", flaky):
            with self.assertRaises(DmError) as cm:
                self.go(t)
        self.assertTrue(cm.exception.keep)
        self.assertEqual(self.listing(), [self.part_path(t).name])

    def test_a_retryable_failure_keeps_pieces_found_by_the_recheck(self):
        tr = self.serve_tracker(mode="empty")
        t = self.single(size=4 * PL, tr=tr, piece_length=PL)
        self.part_path(t).write_bytes(t.blob[:2 * PL] + bytes(2 * PL))
        with self.assertRaises(DmError) as cm:
            self.go(t, opts=session.Options(dht=False, stall_s=1))
        self.assertEqual(str(cm.exception), "no peers found")
        self.assertEqual(self.listing(), [self.part_path(t).name])

    def test_cancel_with_nothing_verified_still_keeps_the_partial(self):
        tr = self.serve_tracker(mode="empty")
        t = self.single(size=4 * PL, tr=tr, piece_length=PL)
        handle, result = TorrentHandle(), {}

        def work():
            try:
                self.go(t, handle=handle, opts=session.Options(dht=False, stall_s=60))
            except BaseException as e:
                result["err"] = e

        thread = threading.Thread(target=work)
        thread.start()
        self.assertTrue(self.wait_for(lambda: self.part_path(t).exists(), 10))
        self.assertTrue(self.wait_for(lambda: any("peer" in x for _, x in list(self.progress)), 10))
        handle.abort()
        thread.join(5)
        self.assertIsInstance(result.get("err"), Cancelled, result)
        self.assertEqual(self.listing(), [self.part_path(t).name])

    def test_a_resume_that_cannot_read_the_partial_keeps_it_with_a_user_safe_message(self):
        import errno

        tr = self.serve_tracker()
        t = self.single(size=4 * PL, tr=tr, piece_length=PL)
        part = self.part_path(t)
        part.write_bytes(t.blob)
        for where, patch in (
            ("open", mock.patch.object(session.Storage, "open_existing", side_effect=OSError(errno.EMFILE, "Too many open files"))),
            ("read", mock.patch.object(session.Storage, "read_block", side_effect=OSError(errno.EIO, "Input/output error"))),
        ):
            with self.subTest(where):
                with patch:
                    with self.assertRaises(DmError) as cm:
                        self.go(t)
                self.assertTrue(cm.exception.keep)
                self.assertNotIn(str(self.cwd), str(cm.exception))
                self.assertEqual(self.listing(), [part.name])
                self.assertEqual(part.read_bytes(), t.blob)

    def test_a_failed_cleanup_never_replaces_the_original_error(self):
        tr = self.serve_tracker(mode="empty")
        t = self.single(size=4 * PL, tr=tr, piece_length=PL)
        with mock.patch.object(session.resume_mod.os, "unlink", side_effect=OSError(13, "Permission denied")):
            with self.assertRaises(DmError) as cm:
                self.go(t, opts=session.Options(dht=False, stall_s=1))
        self.assertEqual(str(cm.exception), "no peers found")

    def test_a_full_download_that_cannot_be_placed_keeps_the_partial_for_a_rerun(self):
        tr = self.serve_tracker()
        t = self.single(size=4 * PL, tr=tr, piece_length=PL)
        self.swarm(tr, self.seeder(t))
        with mock.patch.object(session, "place_file", side_effect=DmError("no free name for 'a.bin'")):
            with self.assertRaises(DmError) as cm:
                self.go(t)
        self.assertEqual(str(cm.exception), "no free name for 'a.bin'")
        self.assertEqual(self.listing(), [self.part_path(t).name])
        final, *_ = self.go(t)  # a rerun finds every piece on disk
        self.assertEqual(final.read_bytes(), t.blob)
        self.assertEqual(self.listing(), ["a.bin"])

    def test_the_same_download_twice_at_once_fails_the_second_at_once_and_the_first_completes(self):
        tr = self.serve_tracker()
        t = self.single(size=32 * PL, tr=tr, piece_length=PL)
        self.swarm(tr, self.seeder(t, delay=0.01))
        result = {}

        def first():
            try:
                result["ok"] = self.go(t, reset=False)
            except BaseException as e:
                result["err"] = e

        self.notes, self.progress = [], []
        thread = threading.Thread(target=first)
        thread.start()
        self.assertTrue(self.wait_for(lambda: self.part_path(t).exists(), 10))
        start = time.monotonic()
        with self.assertRaises(DmError) as cm:
            self.go(t, reset=False)
        self.assertEqual(str(cm.exception), "already being downloaded in this folder")
        self.assertLess(time.monotonic() - start, 2)
        thread.join(30)
        self.assertNotIn("err", result)
        self.assertEqual(result["ok"][0].read_bytes(), t.blob)
        self.assertEqual(self.listing(), ["a.bin"])

    def test_stalled_and_tracker_errors_are_keep(self):
        tr = self.serve_tracker()
        t = self.single(size=4 * PL, tr=tr, piece_length=PL)
        self.swarm(tr, self.seeder(t, never_unchoke=True))
        with self.assertRaises(DmError) as cm:
            self.go(t, opts=session.Options(dht=False, stall_s=1, idle_timeout=30))
        self.assertTrue(cm.exception.keep)
        self.assertTrue(str(cm.exception).startswith("stalled"))
        shutil.rmtree(self.cwd)
        self.cwd.mkdir()
        dead = self.single(size=4 * PL, piece_length=PL, tr=tr)
        with mock.patch.object(session.tracker, "announce", side_effect=session.tracker.TrackerError("boom")):
            with self.assertRaises(DmError) as cm:
                self.go(dead, opts=session.Options(dht=False, stall_s=1))
        self.assertTrue(cm.exception.keep)
        self.assertTrue(str(cm.exception).startswith("tracker error"))

    def test_an_invalid_torrent_leaves_nothing(self):
        bad = self.dir / "bad.torrent"
        bad.write_bytes(b"not a torrent")
        with self.assertRaises(DmError) as cm:
            session.run_torrent(bad, Target(self.cwd, None), lambda *a: None)
        self.assertFalse(cm.exception.keep)
        self.assertEqual(self.listing(), [])


def dmfmt(n):
    from dmcore.fmt import human_size

    return human_size(n)


if __name__ == "__main__":
    unittest.main()
