"""dm magnet links in one process: fixture tracker and seeders on 127.0.0.1, no container runtime needed.

AC3, AC4, AC6, AC7, AC8, AC11, AC12, AC15 and a Ctrl-C resume that fetches the metadata again.
"""

import errno
import hashlib
import random
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import torrentkit
import dmcore
from dmcore import Cancelled
from dmcore.target import DmError, Target
from dmcore.torrent import bencode, layout, magnet, metadata, metainfo, session
from dmcore.torrent.handle import TorrentHandle
from test_dm_torrent_local import FAST, PL, Harness


class MagnetHarness(Harness):
    def announce_url(self, tr):
        return f"http://127.0.0.1:{tr.http_port}/announce"

    def magnet(self, t, tr=None, peers=(), **kw):
        trackers = [self.announce_url(tr)] if tr is not None else []
        return torrentkit.magnet_for(t, trackers, peers, **kw)

    def in_listed_order(self):
        """Peers are dialed in the order the tracker lists them: no shuffle, so `peer #n` in a log line is the
        n-th seeder of `swarm()`. A log never carries an address, so this is how a test tells the peers apart."""
        return mock.patch.object(session, "random", types.SimpleNamespace(shuffle=lambda items: None,
                                                                         sample=random.sample))

    def run_magnet(self, link, opts=FAST, fixed_name=None, handle=None, resume=True):
        self.progress, self.notes = [], []
        return session.run_magnet(magnet.parse(link), Target(self.cwd, fixed_name),
                                  lambda pct, text: self.progress.append((pct, text)),
                                  handle or TorrentHandle(), opts, None, resume=resume,
                                  note=self.notes.append)

    def start_magnet(self, link, opts=FAST, fixed_name=None, resume=True):
        """Run in a thread; returns (handle, result dict, thread)."""
        handle, result = TorrentHandle(), {}

        def work():
            try:
                result["ok"] = self.run_magnet(link, opts, fixed_name, handle, resume)
            except BaseException as e:
                result["err"] = e

        thread = threading.Thread(target=work)
        thread.start()
        self.addCleanup(thread.join, 10)
        return handle, result, thread

    def dm(self, link, **inputs):
        """dmcore.run for a magnet: (exit code, events)."""
        events = []
        code = dmcore.run({"url": link, "no_dht": True, **inputs}, str(self.cwd),
                          lambda method, **kw: events.append((method, kw)), threading.Event())
        return code, events

    @staticmethod
    def texts(events, kind):
        return [kw.get("text") or kw.get("message") for k, kw in events if k == kind]


class Download(MagnetHarness):
    """AC4, AC6, AC8: the metadata comes from the seeder, then the torrent downloads as usual."""

    def test_single_file_from_one_seeder(self):
        tr = self.serve_tracker()
        t = self.single(size=2 * 1024 * 1024, tr=None)
        seeder = self.seeder(t)
        self.swarm(tr, seeder)
        final, size, nfiles, info = self.run_magnet(self.magnet(t, tr))
        self.assertEqual((final, size, nfiles), (self.cwd / "a.bin", 2 * 1024 * 1024, 1))
        self.assertEqual(final.read_bytes(), t.blob)
        self.assertEqual(hashlib.sha1(info).digest(), t.info_hash)
        self.assertEqual(self.listing(), ["a.bin"])
        self.assertTrue(any(p is None and text.startswith("fetching metadata · ")
                            for p, text in self.progress))
        self.assertTrue(any(text.startswith("2.1 MB of 2.1 MB") for _, text in self.progress))

    def test_multi_file_matches_the_source(self):
        tr = self.serve_tracker()
        files = [("x/one.bin", torrentkit.content(20 * PL + 5, "1")),
                 ("two.bin", torrentkit.content(3 * PL, "2")), ("empty", b"")]
        t = self.torrent(files, name="set")
        self.swarm(tr, self.seeder(t))
        final, size, nfiles, _ = self.run_magnet(self.magnet(t, tr))
        self.assertEqual((final, nfiles), (self.cwd / "set", 3))
        self.assertEqual(torrentkit.tree(final), dict(files))

    def test_peers_from_the_magnet_need_no_tracker(self):
        t = self.single(size=4 * PL)
        seeder = self.seeder(t)
        final, *_ = self.run_magnet(self.magnet(t, peers=[("127.0.0.1", seeder.port)]))
        self.assertEqual(final.read_bytes(), t.blob)

    def test_fixed_name_names_the_download_and_dn_is_never_the_name(self):
        tr = self.serve_tracker()
        t = self.single(size=4 * PL)
        self.swarm(tr, self.seeder(t))
        final, *_ = self.run_magnet(self.magnet(t, tr, name="Evil Name"), fixed_name="mine.bin")
        self.assertEqual(self.listing(), ["mine.bin"])
        self.assertEqual(final, self.cwd / "mine.bin")

    # AC8
    def test_one_connection_serves_both_phases(self):
        tr = self.serve_tracker()
        t = self.single(size=8 * PL)
        seeder = self.seeder(t)
        self.swarm(tr, seeder)
        self.run_magnet(self.magnet(t, tr))
        self.assertEqual(seeder.connections, 1)
        self.assertGreaterEqual(seeder.metadata_requests, 1)

    def test_a_peer_without_ut_metadata_is_kept_for_the_download_and_the_other_gives_the_metadata(self):
        tr = self.serve_tracker()
        t = self.single(size=8 * PL)
        plain, source = self.seeder(t, metadata="none"), self.seeder(t)
        self.swarm(tr, plain, source)
        final, *_ = self.run_magnet(self.magnet(t, tr))
        self.assertEqual(final.read_bytes(), t.blob)
        self.assertEqual(plain.metadata_requests, 0)
        self.assertEqual(plain.connections, 1)

    def test_a_seeder_that_never_answers_metadata_requests_does_not_block_an_honest_one(self):
        tr = self.serve_tracker()
        t = self.single(size=8 * PL)
        self.swarm(tr, self.seeder(t, metadata="silent"), self.seeder(t))
        final, *_ = self.run_magnet(self.magnet(t, tr))
        self.assertEqual(final.read_bytes(), t.blob)

    def test_the_tracker_sees_a_leecher_with_something_left_while_metadata_is_unknown(self):
        tr = self.serve_tracker()
        t = self.single(size=8 * PL)
        self.swarm(tr, self.seeder(t))
        self.run_magnet(self.magnet(t, tr))
        first, last = tr.announces[0], tr.announces[-1]
        self.assertEqual((first["event"], first["left"]), ("started", 1))
        self.assertEqual(last["event"], "stopped")

    # AC6
    def test_metadata_with_an_unsafe_path_fails_and_leaves_nothing(self):
        tr = self.serve_tracker()
        t = self.torrent([("../x", b"data" * 100), ("y", b"z")], name="evil")
        self.swarm(tr, self.seeder(t))
        with self.assertRaises(DmError) as cm:
            self.run_magnet(self.magnet(t, tr))
        self.assertTrue(str(cm.exception).startswith("invalid torrent: "), str(cm.exception))
        self.assertFalse(cm.exception.keep)
        self.assertEqual(self.listing(), [])


def many_files(n=700):
    """A torrent whose info dictionary needs two ut_metadata blocks."""
    return [(f"d/f{i:04d}.bin", b"x") for i in range(n)]


class Hostile(MagnetHarness):
    """S3: a corrupt metadata seeder never gets an honest peer banned."""

    def test_a_corrupt_seeder_and_an_honest_one_in_either_order_still_succeed(self):
        for order in (0, 1, 0, 1, 0, 1):
            with self.subTest(round=order):
                self.setUp()
                t = self.torrent(many_files(), name="set", multi=True)
                self.assertGreater(len(bencode.encode(bencode.decode(t.data)[0][b"info"])), metadata.BLOCK)
                tr = self.serve_tracker()
                good, evil = self.seeder(t), self.seeder(t, metadata="corrupt")
                self.swarm(tr, *((good, evil) if order == 0 else (evil, good)))
                logs = []
                self.progress, self.notes = [], []
                with self.in_listed_order():
                    final, _, nfiles, info = session.run_magnet(
                        magnet.parse(self.magnet(t, tr)), Target(self.cwd, None), lambda p, x: None,
                        TorrentHandle(), FAST, logs.append, note=self.notes.append)
                self.assertEqual(nfiles, 700)
                self.assertEqual(hashlib.sha1(info).digest(), t.info_hash)
                dropped = [m for m in logs if "dropped" in m]
                self.assertFalse([m for m in dropped if m.startswith(f"peer #{order + 1} ")], dropped)


class HostileMetadata(MagnetHarness):
    """AC5, NFR1: peers that lie about or abuse the metadata are dropped, an honest peer supplies it."""

    def setUp(self):
        super().setUp()
        files = many_files() + [("big.bin", torrentkit.content(30 * PL, "b"))]
        self.files = files
        self.t = self.torrent(files, name="set", multi=True)
        self.info = bencode.encode(bencode.decode(self.t.data)[0][b"info"])
        self.assertGreater(len(self.info), metadata.BLOCK)

    def run_with(self, *evil, honest=None):
        tr = self.serve_tracker()
        good = self.seeder(self.t, delay=0.02, **{"metadata_delay": 0.3, **(honest or {})})  # hostile peers go first
        self.swarm(tr, *evil, good)
        logs = []
        self.progress, self.notes = [], []
        with self.in_listed_order():
            final, _, nfiles, info = session.run_magnet(
                magnet.parse(self.magnet(self.t, tr)), Target(self.cwd, None), lambda p, x: None,
                TorrentHandle(), FAST, logs.append, note=self.notes.append)
        self.assertEqual(torrentkit.tree(final), dict(self.files))
        self.assertEqual(hashlib.sha1(info).digest(), self.t.info_hash)
        dropped = [m for m in logs if "dropped" in m]
        # never the honest one, listed last; and what is logged is the peer's number and the kind of error only
        self.assertFalse([m for m in dropped if m.startswith(f"peer #{len(evil) + 1} ")], dropped)
        for m in dropped:
            self.assertRegex(m, r"^peer #\d+ dropped: \w+$")
        return dropped

    def dropped_peer(self, dropped, number):
        return any(m.startswith(f"peer #{number} dropped") for m in dropped)

    def hostile(self, mode):
        evil = self.seeder(self.t, metadata=mode)
        dropped = self.run_with(evil)
        self.assertTrue(self.dropped_peer(dropped, 1), (mode, dropped))
        self.assertLessEqual(evil.metadata_requests, 3)

    def test_metadata_size_zero_drops_the_peer(self):
        self.hostile("size0")

    def test_a_huge_metadata_size_drops_the_peer(self):
        self.hostile("huge")

    def test_blocks_of_the_wrong_length_drop_the_peer(self):
        self.hostile("short")

    def test_unsolicited_and_extra_blocks_drop_the_peer(self):
        self.hostile("extra")

    def test_a_peer_that_never_answers_is_ignored_and_not_needed(self):
        silent = self.seeder(self.t, metadata="silent")
        self.run_with(silent)

    def test_a_corrupt_peer_does_not_get_the_honest_one_banned(self):
        self.run_with(self.seeder(self.t, metadata="corrupt"))

    def test_every_kind_of_hostile_peer_at_once(self):
        # a silent peer is tested on its own: next to a corrupt one it can cost a 10 s timeout per attempt
        evil = [self.seeder(self.t, metadata=m) for m in ("size0", "huge", "short", "extra", "corrupt")]
        self.run_with(*evil)

    def test_sybil_peers_holding_a_block_for_each_of_three_sizes_do_not_stall_the_run(self):
        sizes = [metadata.BLOCK * 2 + 1 + i for i in range(metadata.MAX_SIZES)]
        sybils = [self.seeder(self.t, metadata="sybil", sybil_size=size) for size in sizes]
        # the honest seeder says hello only after every slot holds a block
        self.run_with(*sybils, honest={"ext_delay": 1.0, "metadata_delay": 0})
        for sybil in sybils:
            self.assertGreaterEqual(sybil.metadata_requests, 1)

    def test_a_full_size_junk_set_is_refused_within_the_memory_bound(self):
        import tracemalloc

        junk = self.seeder(self.t, metadata="junk")
        tracemalloc.start()
        try:
            tracemalloc.reset_peak()
            base = tracemalloc.get_traced_memory()[0]
            dropped = self.run_with(junk, honest={"metadata_delay": 1.0})
            peak = tracemalloc.get_traced_memory()[1] - base
        finally:
            tracemalloc.stop()
        self.assertTrue(self.dropped_peer(dropped, 1), dropped)  # its whole set was assembled, and failed
        self.assertGreaterEqual(junk.metadata_requests, 600)
        peers = 2
        self.assertLess(peak, metadata.MAX_METADATA + peers * metadata.BLOCK + 2 * 1024 * 1024,
                        f"peak {peak} bytes")
        print(f"\n[tracemalloc] in-process magnet run with a 10 MiB junk set: peak {peak} bytes")


class OddExtensions(MagnetHarness):
    """S4: a plain .torrent download never drops a peer over its BEP 10 extras."""

    def download_from(self, **seeder_args):
        tr = self.serve_tracker()
        t = self.single(size=4 * PL, tr=tr)
        seeder = self.seeder(t, **seeder_args)
        self.swarm(tr, seeder)
        final, *_ = self.run_torrent(t)
        self.assertEqual(final.read_bytes(), t.blob)
        self.assertEqual(seeder.connections, 1)

    def test_an_extension_handshake_with_unsorted_keys(self):
        self.download_from(ext_body=b"d1:vi1e1:mdee")

    def test_an_extension_handshake_with_a_huge_ut_metadata_id(self):
        self.download_from(ext_body=bencode.encode({"m": {"ut_metadata": 300}, "metadata_size": 5}))

    def test_an_extension_handshake_that_is_not_a_dictionary(self):
        self.download_from(ext_body=b"i5e")

    def test_an_oversized_extension_message_is_ignored(self):
        self.download_from(ext_noise=100_000)


class Stalls(MagnetHarness):
    """AC7: the stall rules apply while fetching metadata."""

    def test_peers_that_never_send_metadata_stall_the_run(self):
        tr = self.serve_tracker()
        t = self.single(size=4 * PL)
        self.swarm(tr, self.seeder(t, metadata="silent"))
        opts = session.Options(dht=False, stall_s=1, connect_timeout=2, idle_timeout=10)
        start = time.monotonic()
        with self.assertRaises(DmError) as cm:
            self.run_magnet(self.magnet(t, tr), opts)
        self.assertEqual(str(cm.exception), "stalled: no progress for 2.5 seconds")
        self.assertLess(time.monotonic() - start, 8)
        self.assertEqual(self.listing(), [])

    def test_no_peers_found(self):
        tr = self.serve_tracker(mode="empty")
        t = self.single(size=4 * PL)
        opts = session.Options(dht=False, stall_s=1, connect_timeout=2, idle_timeout=10)
        with self.assertRaises(DmError) as cm:
            self.run_magnet(self.magnet(t, tr), opts)
        self.assertEqual(str(cm.exception), "no peers found")
        self.assertEqual(self.listing(), [])

    def test_a_stall_leaves_an_earlier_partial_alone(self):
        tr = self.serve_tracker()
        t = self.single(size=64 * PL, piece_length=PL)
        seeder = self.seeder(t, delay=0.01)
        self.swarm(tr, seeder)
        link = self.magnet(t, tr)
        handle, result, thread = self.start_magnet(link)
        self.assertTrue(self.wait_for(lambda: any(p and p >= 20 for p, _ in list(self.progress)), 20))
        handle.abort()
        thread.join(5)
        before = self.listing()
        self.assertEqual(len(before), 2)  # the partial and its stored metadata
        tr.mode = "empty"
        self.swarm(tr)  # nobody to ask
        tr.static = []
        with self.assertRaises(DmError):
            self.run_magnet(link, session.Options(dht=False, stall_s=1, connect_timeout=2, idle_timeout=10))
        self.assertEqual(self.listing(), before)


class Cancel(MagnetHarness):
    """AC15 and resume: Ctrl-C in the metadata phase leaves nothing; later it keeps the partial."""

    def test_cancel_during_the_metadata_phase_answers_fast_and_leaves_nothing(self):
        tr = self.serve_tracker()
        t = self.single(size=4 * PL)
        self.swarm(tr, self.seeder(t, metadata="silent"))
        handle, result, thread = self.start_magnet(self.magnet(t, tr))
        self.assertTrue(self.wait_for(
            lambda: any("fetching metadata" in x for _, x in list(self.progress)), 10))
        start = time.monotonic()
        self.assertTrue(handle.abort())
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertLess(time.monotonic() - start, 2)
        self.assertIsInstance(result.get("err"), Cancelled)
        self.assertEqual(self.listing(), [])

    def part_name(self, t):
        from dmcore import resume

        meta = metainfo.load(t.write_torrent(self.dir / "k.torrent"))
        key = resume.torrent_key(meta.info_hash, Target(self.cwd, None))
        return resume.paths(self.cwd, layout.item_name(meta), key).part

    def test_a_cancelled_download_resumes_by_fetching_the_metadata_again_but_not_the_verified_pieces(self):
        tr = self.serve_tracker()
        t = self.single(size=64 * PL, piece_length=PL)
        first = self.seeder(t, delay=0.01)
        self.swarm(tr, first)
        link = self.magnet(t, tr)
        handle, result, thread = self.start_magnet(link)
        self.assertTrue(self.wait_for(lambda: any(p and p >= 30 for p, _ in list(self.progress)), 20))
        self.assertTrue(handle.abort())
        thread.join(5)
        self.assertIsInstance(result.get("err"), Cancelled)
        part = self.part_name(t)
        meta = next(self.cwd.glob(".dm.*.dm-meta"))
        self.assertEqual(self.listing(), sorted([part.name, meta.name]))
        meta.unlink()  # without the stored metadata it has to be fetched again (StoredMetadata covers the reuse)
        data = part.read_bytes()
        have = {i for i in range(64) if any(data[i * PL:(i + 1) * PL]) and data[i * PL:(i + 1) * PL]
                == t.blob[i * PL:(i + 1) * PL]}
        self.assertTrue(0 < len(have) < 64)
        second = self.seeder(t)
        self.swarm(tr, second)
        final, *_ = self.run_magnet(link)
        self.assertEqual(final.read_bytes(), t.blob)
        self.assertGreaterEqual(second.metadata_requests, 1)  # nothing is stored yet: fetched again
        self.assertEqual(set(second.piece_requests) & have, set())
        self.assertEqual(set(second.piece_requests), set(range(64)) - have)
        self.assertEqual(self.notes, [f"resuming from {dmcore.fmt.human_size(len(have) * PL)} of "
                                      f"{dmcore.fmt.human_size(64 * PL)}"])
        self.assertEqual(self.listing(), ["a.bin"])

    def test_a_partial_started_from_a_torrent_file_is_resumed_by_its_magnet(self):
        tr = self.serve_tracker()
        t = self.single(size=64 * PL, tr=tr, piece_length=PL)
        self.swarm(tr, self.seeder(t, delay=0.01))
        path = t.write_torrent(self.dir / "x.torrent")
        handle, result, progress = TorrentHandle(), {}, []

        def work():
            try:
                session.run_torrent(path, Target(self.cwd, None), lambda p, x: progress.append((p, x)),
                                    handle, FAST)
            except BaseException as e:
                result["err"] = e

        thread = threading.Thread(target=work)
        thread.start()
        self.assertTrue(self.wait_for(lambda: any(p is not None and p >= 30 and "peer" in x
                                                  for p, x in list(progress)), 20))
        handle.abort()
        thread.join(5)
        self.assertEqual(self.listing(), [self.part_name(t).name])
        second = self.seeder(t)
        self.swarm(tr, second)
        final, *_ = self.run_magnet(self.magnet(t, tr))
        self.assertEqual(final.read_bytes(), t.blob)
        self.assertTrue(self.notes and self.notes[0].startswith("resuming from"))


class StoredMetadata(MagnetHarness):
    """AC9, AC10 (R14, R15): the metadata is kept beside the partial and reused by the next run."""

    def paths(self, t):
        from dmcore import resume

        meta = metainfo.load(t.write_torrent(self.dir / "k.torrent"))
        key = resume.torrent_key(meta.info_hash, Target(self.cwd, None))
        return resume.paths(self.cwd, layout.item_name(meta), key).part, resume.meta_path(self.cwd, key)

    def cancelled_run(self, t, tr):
        """Start the magnet against a slow seeder and cancel it once some pieces are in."""
        first = self.seeder(t, delay=0.01)
        self.swarm(tr, first)
        link = self.magnet(t, tr)
        handle, result, thread = self.start_magnet(link)
        self.assertTrue(self.wait_for(lambda: any(p and p >= 30 for p, _ in list(self.progress)), 20))
        self.assertTrue(handle.abort())
        thread.join(5)
        self.assertIsInstance(result.get("err"), Cancelled)
        return link

    def test_ctrl_c_keeps_the_partial_and_the_metadata_and_the_rerun_asks_for_no_metadata(self):
        tr = self.serve_tracker()
        t = self.single(size=64 * PL, piece_length=PL)
        link = self.cancelled_run(t, tr)
        part, meta = self.paths(t)
        self.assertEqual(self.listing(), sorted([part.name, meta.name]))
        self.assertEqual(hashlib.sha1(meta.read_bytes()).digest(), t.info_hash)
        second = self.seeder(t)
        self.swarm(tr, second)
        final, *_, info = self.run_magnet(link)
        self.assertEqual(final.read_bytes(), t.blob)
        self.assertEqual(second.metadata_requests, 0)
        self.assertEqual(hashlib.sha1(info).digest(), t.info_hash)
        self.assertTrue(self.notes and self.notes[0].startswith("resuming from"))
        self.assertEqual(self.listing(), ["a.bin"])  # success leaves neither

    def torrent_run(self, t, resume=True):
        path = t.write_torrent(self.dir / "x.torrent")
        return session.run_torrent(path, Target(self.cwd, None), lambda p, x: None, TorrentHandle(), FAST,
                                   resume=resume)

    def test_a_torrent_file_run_that_resumes_a_magnets_partial_removes_the_stored_metadata(self):
        tr = self.serve_tracker()
        t = self.single(size=64 * PL, tr=tr, piece_length=PL)
        self.cancelled_run(t, tr)
        part, meta = self.paths(t)
        self.assertEqual(self.listing(), sorted([part.name, meta.name]))
        second = self.seeder(t)
        self.swarm(tr, second)
        final, *_ = self.torrent_run(t)
        self.assertEqual(final.read_bytes(), t.blob)
        self.assertEqual(self.listing(), ["a.bin"])  # no .dm-meta left behind

    def test_metadata_arriving_for_a_torrent_already_complete_on_disk_finishes_without_pieces(self):
        tr = self.serve_tracker()
        t = self.single(size=8 * PL, piece_length=PL)
        seeder = self.seeder(t)
        self.swarm(tr, seeder)
        part, _ = self.paths(t)
        part.write_bytes(t.blob)  # every piece is already there, as a run killed just before the move leaves it
        final, size, nfiles, info = self.run_magnet(self.magnet(t, tr))
        self.assertEqual(final.read_bytes(), t.blob)
        self.assertEqual((size, nfiles), (8 * PL, 1))
        self.assertEqual(hashlib.sha1(info).digest(), t.info_hash)
        self.assertGreaterEqual(seeder.metadata_requests, 1)
        self.assertEqual(seeder.requests, 0)  # not one block of a piece was asked for
        self.assertEqual(self.progress[-1][0], 100)
        self.assertEqual(self.listing(), ["a.bin"])

    def test_a_temp_file_of_a_killed_metadata_write_is_cleaned_up_by_the_next_run(self):
        tr = self.serve_tracker()
        t = self.single(size=4 * PL, piece_length=PL)
        self.swarm(tr, self.seeder(t))
        _, meta = self.paths(t)
        stale = meta.with_name(meta.name + ".0123456789abcdef.tmp")
        stale.write_bytes(b"half")
        final, *_ = self.run_magnet(self.magnet(t, tr))
        self.assertEqual(final.read_bytes(), t.blob)
        self.assertEqual(self.listing(), ["a.bin"])

    def test_a_torrent_file_run_with_no_resume_removes_the_stored_metadata_up_front(self):
        tr = self.serve_tracker()
        t = self.single(size=64 * PL, tr=tr, piece_length=PL)
        self.cancelled_run(t, tr)
        tr.mode = "empty"  # nobody to ask: the run fails, and the old files are gone anyway
        self.swarm(tr)
        with self.assertRaises(DmError):
            session.run_torrent(t.write_torrent(self.dir / "x.torrent"), Target(self.cwd, None),
                                lambda p, x: None, TorrentHandle(),
                                session.Options(dht=False, stall_s=1, connect_timeout=2, idle_timeout=10), resume=False)
        self.assertEqual(self.listing(), [])

    def test_a_torrent_file_run_never_reads_the_stored_metadata(self):
        tr = self.serve_tracker()
        t = self.single(size=64 * PL, tr=tr, piece_length=PL)
        self.cancelled_run(t, tr)
        _, meta = self.paths(t)
        meta.write_bytes(b"not an info dictionary")
        second = self.seeder(t)
        self.swarm(tr, second)
        final, *_ = self.torrent_run(t)
        self.assertEqual(final.read_bytes(), t.blob)
        self.assertEqual(self.listing(), ["a.bin"])

    def test_a_tampered_metadata_file_is_deleted_and_fetched_again(self):
        tr = self.serve_tracker()
        t = self.single(size=64 * PL, piece_length=PL)
        link = self.cancelled_run(t, tr)
        _, meta = self.paths(t)
        data = bytearray(meta.read_bytes())
        data[10] ^= 0xFF
        meta.write_bytes(bytes(data))
        second = self.seeder(t)
        self.swarm(tr, second)
        final, *_ = self.run_magnet(link)
        self.assertEqual(final.read_bytes(), t.blob)
        self.assertGreaterEqual(second.metadata_requests, 1)
        self.assertEqual(self.listing(), ["a.bin"])

    def test_no_resume_removes_the_partial_and_the_metadata_up_front(self):
        tr = self.serve_tracker()
        t = self.single(size=64 * PL, piece_length=PL)
        link = self.cancelled_run(t, tr)
        tr.mode = "empty"  # nobody to ask: the run fails, and the old files are gone anyway
        self.swarm(tr)
        with self.assertRaises(DmError):
            self.run_magnet(link, session.Options(dht=False, stall_s=1, connect_timeout=2, idle_timeout=10), resume=False)
        self.assertEqual(self.listing(), [])

    def test_no_resume_downloads_everything_again(self):
        tr = self.serve_tracker()
        t = self.single(size=64 * PL, piece_length=PL)
        link = self.cancelled_run(t, tr)
        second = self.seeder(t)
        self.swarm(tr, second)
        final, *_ = self.run_magnet(link, resume=False)
        self.assertEqual(final.read_bytes(), t.blob)
        self.assertGreaterEqual(second.metadata_requests, 1)
        self.assertEqual(set(second.piece_requests), set(range(64)))
        self.assertEqual(self.listing(), ["a.bin"])

    def test_a_failure_with_no_verified_piece_deletes_the_partial_and_the_metadata(self):
        tr = self.serve_tracker()
        t = self.single(size=4 * PL, piece_length=PL)
        self.swarm(tr, self.seeder(t, never_unchoke=True))
        opts = session.Options(dht=False, stall_s=1, connect_timeout=2, idle_timeout=10)
        with self.assertRaises(DmError) as cm:
            self.run_magnet(self.magnet(t, tr), opts)
        self.assertTrue(str(cm.exception).startswith("stalled"), str(cm.exception))
        self.assertEqual(self.listing(), [])

    def test_a_failure_after_a_verified_piece_keeps_both(self):
        tr = self.serve_tracker()
        t = self.single(size=64 * PL, piece_length=PL)
        self.cancelled_run(t, tr)
        part, meta = self.paths(t)
        tr.mode = "empty"
        self.swarm(tr)
        with self.assertRaises(DmError):
            self.run_magnet(self.magnet(t, tr), session.Options(dht=False, stall_s=1, connect_timeout=2, idle_timeout=10))
        self.assertEqual(self.listing(), sorted([part.name, meta.name]))

    def test_a_file_in_the_way_of_a_symlinked_metadata_name_is_not_followed(self):
        tr = self.serve_tracker()
        t = self.single(size=64 * PL, piece_length=PL)
        link = self.cancelled_run(t, tr)
        _, meta = self.paths(t)
        secret = self.dir / "secret"
        secret.write_bytes(b"x")
        meta.unlink()
        meta.symlink_to(secret)
        second = self.seeder(t)
        self.swarm(tr, second)
        final, *_ = self.run_magnet(link)
        self.assertEqual(final.read_bytes(), t.blob)
        self.assertEqual(secret.read_bytes(), b"x")
        self.assertGreaterEqual(second.metadata_requests, 1)


class ServeMetadata(MagnetHarness):
    """AC13 (R18): peers that ask dm for the metadata are refused before it is verified and served after."""

    def probe(self, t, mode="all"):
        probe = self.start(self.peerfix.MetadataProbe(t.write_torrent(self.dir / "p.torrent"), mode))
        return probe

    def run_with(self, t, tr, probe):
        """Download t as a magnet from a slow seeder whose metadata comes late, with the probe alongside."""
        seeder = self.seeder(t, delay=0.02, metadata_delay=0.5)
        self.swarm(tr, probe, seeder)
        info = bencode.encode(bencode.decode(t.data)[0][b"info"])
        final, *_ = self.run_magnet(self.magnet(t, tr))
        self.assertEqual(final.read_bytes(), t.blob)
        return info

    def test_a_request_before_the_metadata_is_rejected_and_after_it_the_blocks_are_served(self):
        tr = self.serve_tracker()
        t = self.single(size=64 * PL, piece_length=PL)
        probe = self.probe(t)
        info = self.run_with(t, tr, probe)
        self.assertEqual(probe.early, [metadata.REJECT])
        self.assertEqual(probe.advertised, len(info))
        self.assertEqual(b"".join(probe.blocks[i] for i in sorted(probe.blocks)), info)
        self.assertEqual(probe.connections, 1)

    def test_a_block_past_the_end_drops_the_peer_and_the_download_still_finishes(self):
        tr = self.serve_tracker()
        t = self.single(size=64 * PL, piece_length=PL)
        probe = self.probe(t, "bad_index")
        self.run_with(t, tr, probe)
        self.assertEqual(probe.early, [metadata.REJECT])
        self.assertEqual(probe.blocks, {})
        self.assertTrue(probe.eof)

    def test_a_few_queued_requests_are_all_answered(self):
        tr = self.serve_tracker()
        t = self.single(size=64 * PL, piece_length=PL)
        probe = self.probe(t, "twice")
        self.run_with(t, tr, probe)
        self.assertEqual([k for k, p, _ in probe.later], [metadata.DATA, metadata.DATA])

    def test_a_download_that_started_from_stored_metadata_advertises_the_size_at_once(self):
        tr = self.serve_tracker()
        t = self.single(size=64 * PL, piece_length=PL)
        link = StoredMetadata.cancelled_run(self, t, tr)
        probe = self.probe(t)
        self.swarm(tr, probe, self.seeder(t, delay=0.02))
        final, *_ = self.run_magnet(link)
        self.assertEqual(final.read_bytes(), t.blob)
        self.assertEqual(probe.early, [])
        self.assertEqual(probe.advertised, len(bencode.encode(bencode.decode(t.data)[0][b"info"])))


class SetupCancel(MagnetHarness):
    """N6: Ctrl-C while the setup thread re-checks a partial, and a second run of the same torrent."""

    def test_cancel_during_the_recheck_keeps_the_partial_and_a_same_key_torrent_run_is_refused(self):
        from dmcore import resume

        tr = self.serve_tracker()
        t = self.single(size=64 * PL, piece_length=PL)
        self.swarm(tr, self.seeder(t))
        meta = metainfo.load(t.write_torrent(self.dir / "k.torrent"))
        key = resume.torrent_key(meta.info_hash, Target(self.cwd, None))
        part = resume.paths(self.cwd, layout.item_name(meta), key).part
        part.write_bytes(t.blob)  # a full partial: the re-check has work to do
        entered = threading.Event()

        def slow(storage, meta, report, handle):
            entered.set()
            for _ in range(600):
                if handle.aborted:
                    raise Cancelled()
                time.sleep(0.05)

        with mock.patch.object(session, "recheck", slow):
            handle, result, thread = self.start_magnet(self.magnet(t, tr))
            self.assertTrue(entered.wait(10))
            with self.assertRaises(DmError) as cm:  # the same key, as a .torrent
                session.run_torrent(t.write_torrent(self.dir / "x.torrent"), Target(self.cwd, None),
                                    lambda *a: None, TorrentHandle(), FAST)
            self.assertEqual(str(cm.exception), "already being downloaded in this folder")
            start = time.monotonic()
            handle.abort()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertLess(time.monotonic() - start, 2)
        self.assertIsInstance(result.get("err"), Cancelled)
        self.assertEqual(self.listing(), [part.name])  # the partial stays, the lock is gone
        self.assertEqual(part.read_bytes(), t.blob)


class SetupRace(MagnetHarness):
    """N4: a failure that races the thread setting up the download never keeps a fresh empty partial."""

    def test_a_failure_while_the_setup_creates_the_partial_discards_it(self):
        t = self.single(size=4 * PL)
        info = bencode.encode(bencode.decode(t.data)[0][b"info"])
        entered, threads = threading.Event(), []
        real_prepare = session._prepare

        def prepare(*a, **kw):  # runs inside the setup thread, holding the run's lock
            entered.set()
            return real_prepare(*a, **kw)

        def drive(meta, storage, progress, handle, opts, log, place, have, out, **kw):
            thread = threading.Thread(target=lambda: kw["on_metadata"](info))
            threads.append(thread)
            thread.start()
            self.assertTrue(entered.wait(10))
            raise DmError("stalled: no progress for 2.5 seconds", keep=True)

        link = torrentkit.magnet_for(t, [], [("127.0.0.1", 9)])
        with mock.patch.object(session, "_prepare", prepare), mock.patch.object(session, "_drive", drive):
            with self.assertRaises(DmError):
                self.run_magnet(link)
        threads[0].join(10)
        self.assertEqual(self.listing(), [])


class Timing(MagnetHarness):
    """AC15: a 1 MiB info dictionary from a local seeder."""

    def test_one_mib_of_metadata_arrives_in_under_five_seconds(self):
        tr = self.serve_tracker()
        files = [(f"d/{'n' * 230}{i:05d}", b"") for i in range(4200)]
        files[0] = (files[0][0], b"x")
        t = self.torrent(files, name="many")
        info = bencode.encode(bencode.decode(t.data)[0][b"info"])
        self.assertGreater(len(info), 1024 * 1024)
        self.swarm(tr, self.seeder(t))
        started = {}
        real = metainfo.from_info

        def timed(*a, **kw):
            started.setdefault("at", time.monotonic())
            return real(*a, **kw)

        begin = time.monotonic()
        with mock.patch.object(metainfo, "from_info", timed):
            final, size, nfiles, got = self.run_magnet(self.magnet(t, tr))
        self.assertEqual(got, info)
        self.assertEqual(nfiles, 4200)
        self.assertLess(started["at"] - begin, 5)


class Saved(MagnetHarness):
    """AC3, AC4, AC11, AC12 through dmcore.run: output lines, the saved .torrent, its failure."""

    def seeded(self, **kw):
        tr = self.serve_tracker()
        t = self.single(size=8 * PL, **kw)
        self.swarm(tr, self.seeder(t))
        return tr, t

    # AC3
    def test_a_magnet_without_trackers_or_peers_and_no_dht_fails_at_once_naming_only_the_hash(self):
        t = self.single(size=4 * PL)
        link = f"magnet:?xt=urn:btih:{t.info_hash.hex()}&dn=Secret+Name"
        with mock.patch.object(session, "run_magnet", side_effect=AssertionError("network used")):
            code, events = self.dm(link, no_dht=True)
        self.assertEqual(code, 1)
        errors = self.texts(events, "log")
        self.assertEqual(errors, [f"dm: magnet {t.info_hash.hex()[:8]}…: magnet link has no trackers or "
                                  "peers (and the DHT is off)"])
        self.assertEqual(self.listing(), [])

    def test_invalid_and_v2_links_fail_without_a_label_or_the_link(self):
        for link, message in (("magnet:?dn=Secret", "dm: invalid magnet link"),
                              ("magnet:?xt=urn:btmh:1220" + "ab" * 32 + "&tr=udp://secret.example:1",
                               "dm: v2-only magnet links are not supported")):
            with self.subTest(link=link[:30]):
                code, events = self.dm(link)
                self.assertEqual((code, self.texts(events, "log")), (1, [message]))

    # AC4
    def test_output_is_the_saved_download_then_the_saved_torrent(self):
        tr, t = self.seeded()
        code, events = self.dm(self.magnet(t, tr, name="Secret Name"))
        self.assertEqual(code, 0)
        out = "".join(self.texts(events, "output"))
        self.assertEqual(out, "saved a.bin (131.1 kB, 1 file)\nsaved a.bin.torrent\n")
        self.assertEqual(self.listing(), ["a.bin", "a.bin.torrent"])
        self.assertEqual((self.cwd / "a.bin").read_bytes(), t.blob)
        # the peers table is the one place an address may be: the host draws it, nothing else shows it
        shown = " ".join(str(v) for k, kw in events if k != "table" for v in kw.values())  # every log level
        self.assertNotIn("Secret", shown)
        self.assertNotIn("127.0.0.1", shown)
        self.assertTrue(any(str(kw.get("message")).startswith("fetching metadata · ")
                            and kw.get("percent") is None for k, kw in events if k == "bar"))

    # AC11
    def test_the_saved_torrent_has_the_same_hash_and_the_magnets_trackers(self):
        tr, t = self.seeded()
        second = "udp://127.0.0.1:9/announce"
        link = torrentkit.magnet_for(t, [self.announce_url(tr), second])
        code, _ = self.dm(link)
        self.assertEqual(code, 0)
        saved = metainfo.load(self.cwd / "a.bin.torrent")
        self.assertEqual(saved.info_hash, t.info_hash)
        self.assertEqual(saved.trackers, ((self.announce_url(tr), second),))
        top = bencode.decode((self.cwd / "a.bin.torrent").read_bytes())[0]
        self.assertEqual(top[b"announce"], self.announce_url(tr).encode())

    def test_the_saved_torrent_without_trackers_has_none(self):
        t = self.single(size=4 * PL)
        seeder = self.seeder(t)
        code, _ = self.dm(torrentkit.magnet_for(t, [], [("127.0.0.1", seeder.port)]))
        self.assertEqual(code, 0)
        top = bencode.decode((self.cwd / "a.bin.torrent").read_bytes())[0]
        self.assertEqual(sorted(top), [b"info"])

    def test_an_existing_torrent_file_is_not_overwritten(self):
        tr, t = self.seeded()
        (self.cwd / "a.bin.torrent").write_bytes(b"mine")
        code, events = self.dm(self.magnet(t, tr))
        self.assertEqual(code, 0)
        self.assertEqual((self.cwd / "a.bin.torrent").read_bytes(), b"mine")
        self.assertEqual(metainfo.load(self.cwd / "a.bin (1).torrent").info_hash, t.info_hash)
        self.assertIn("saved a.bin (1).torrent\n", self.texts(events, "output"))

    def test_with_output_name_the_torrent_is_named_after_it(self):
        tr, t = self.seeded()
        code, _ = self.dm(self.magnet(t, tr), output="mine.bin")
        self.assertEqual(code, 0)
        self.assertEqual(self.listing(), ["mine.bin", "mine.bin.torrent"])

    def test_a_multi_file_download_saves_its_torrent_beside_the_folder(self):
        tr = self.serve_tracker()
        t = self.torrent([("a.bin", torrentkit.content(PL)), ("b.bin", b"hi")], name="set")
        self.swarm(tr, self.seeder(t))
        code, events = self.dm(self.magnet(t, tr))
        self.assertEqual(code, 0)
        self.assertEqual(self.listing(), ["set", "set.torrent"])
        self.assertIn("saved set.torrent\n", self.texts(events, "output"))

    # AC12
    def test_a_torrent_that_cannot_be_written_warns_and_still_succeeds(self):
        tr, t = self.seeded()
        from dmcore.torrent import place

        with mock.patch.object(place, "place_file", side_effect=OSError(errno.EACCES, "Permission denied")):
            code, events = self.dm(self.magnet(t, tr))
        self.assertEqual(code, 0)
        self.assertEqual(self.listing(), ["a.bin"])
        self.assertEqual((self.cwd / "a.bin").read_bytes(), t.blob)
        out = "".join(self.texts(events, "output"))
        self.assertEqual(out, "saved a.bin (131.1 kB, 1 file)\n")
        warns = [kw["message"] for k, kw in events if k == "log" and kw.get("level") == "warn"]
        self.assertEqual(warns, ["could not save a.bin.torrent: Permission denied"])

    def test_any_error_while_saving_the_torrent_only_warns(self):
        tr, t = self.seeded()
        with mock.patch.object(dmcore, "save_torrent", side_effect=ValueError("boom")):
            code, events = self.dm(self.magnet(t, tr))
        self.assertEqual(code, 0)
        self.assertEqual((self.cwd / "a.bin").read_bytes(), t.blob)
        warns = [kw["message"] for k, kw in events if k == "log" and kw.get("level") == "warn"]
        self.assertEqual(warns, ["could not save a.bin.torrent: boom"])

    def test_a_torrent_named_after_a_255_byte_output_name_fits_and_a_second_one_gets_a_number(self):
        t = self.single(size=4 * PL)
        info = bencode.encode(bencode.decode(t.data)[0][b"info"])
        target = Target(self.cwd, "n" * 255)
        paths = [dmcore.save_torrent(self.cwd / target.fixed_name, target, info, ())[0] for _ in range(2)]
        names = [p.name for p in paths]
        self.assertEqual(len(set(names)), 2)
        self.assertTrue(all(0 < len(n.encode()) <= 255 and n.endswith(".torrent") for n in names), names)
        self.assertIn(" (1).torrent", names[1])
        self.assertEqual(self.listing(), sorted(names))

    def test_a_failed_download_saves_no_torrent(self):
        tr = self.serve_tracker(mode="empty")
        t = self.single(size=4 * PL)
        code, _ = self.dm(self.magnet(t, tr), stall=1)
        self.assertEqual(code, 1)
        self.assertEqual(self.listing(), [])

    def test_a_torrent_file_download_is_unchanged_and_saves_nothing_extra(self):
        tr = self.serve_tracker()
        t = self.single(size=4 * PL, tr=tr)
        self.swarm(tr, self.seeder(t))
        t.write_torrent(self.cwd / "x.torrent")
        code, events = self.dm("x.torrent")
        self.assertEqual(code, 0)
        self.assertEqual(self.listing(), ["a.bin", "x.torrent"])
        self.assertEqual("".join(self.texts(events, "output")), "saved a.bin (65.5 kB, 1 file)\n")


if __name__ == "__main__":
    unittest.main()
