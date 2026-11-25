"""dm torrent support in one process: fixture tracker and peers on 127.0.0.1, no container runtime needed.

The fixtures run on a background loop (torrentkit.Background); the code under test blocks the test thread.
"""

import asyncio
import errno
import hashlib
import os
import shutil
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "plugins" / "dm"))
sys.path.insert(0, str(ROOT / "tests"))

import torrentkit
import dmcore
from dmcore import Cancelled, fetch
from dmcore.target import DmError, Target
from dmcore.torrent import layout, metainfo, session, tracker as tracker_mod
from dmcore.torrent.handle import TorrentHandle

PL = 16384
FAST = session.Options(dht=False, stall_s=3, connect_timeout=2, idle_timeout=10)


class Env(unittest.TestCase):
    """A scratch folder, a background loop, and the fixture modules."""

    @classmethod
    def setUpClass(cls):
        cls.bg = torrentkit.Background()
        cls.addClassCleanup(cls.bg.close)
        cls.trackerfix = torrentkit.load_fixture("tracker")
        cls.peerfix = torrentkit.load_fixture("peer")

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="dm-local-")).resolve()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.cwd = self.dir / "cwd"
        self.cwd.mkdir()
        self.data = self.dir / "data"
        self.data.mkdir()

    def start(self, fixture):
        self.bg.call(fixture.start("127.0.0.1"))
        self.addCleanup(self.bg.call, fixture.stop())
        return fixture

    def seeder(self, *torrents, **kw):
        s = self.start(self.peerfix.Seeder(**kw))
        for t in torrents:
            path = t.write_torrent(self.dir / f"seed-{t.info_hash.hex()[:8]}.torrent")
            s.add(path, torrentkit.write_files(self.data, t).parent)
        return s


class FixtureSmoke(Env):
    """The leecher fixture downloads from the seeder fixture: the two agree on the protocol."""

    def test_leecher_gets_every_piece_from_the_seeder(self):
        t = torrentkit.make_torrent([("a.bin", torrentkit.content(3 * PL + 100))], PL, name="a.bin")
        seeder = self.seeder(t)
        leecher = self.peerfix.Leecher(t.write_torrent(self.dir / "a.torrent"))
        self.bg.call(leecher.connect("127.0.0.1", seeder.port))
        self.assertTrue(self.bg.call(leecher.wait_pieces(4)))
        self.assertEqual(b"".join(leecher.pieces[i] for i in range(4)), t.blob)
        self.bg.call(leecher.stop())


class Harness(Env):
    """Helpers to run a torrent against the fixture tracker and seeders."""

    def serve_tracker(self, **kw):
        return self.start(self.trackerfix.Tracker(**kw))

    def torrent(self, files, name="t", tr=None, scheme="http", query="", multi=None, piece_length=PL):
        announce = None
        if tr is not None:
            port = tr.http_port if scheme == "http" else tr.udp_port
            announce = f"{scheme}://127.0.0.1:{port}/announce{query}"
        return torrentkit.make_torrent(files, piece_length, announce=announce, name=name, multi=multi)

    def run_torrent(self, t, opts=FAST, fixed_name=None, log=None, handle=None):
        path = t.write_torrent(self.dir / "x.torrent")
        self.progress = []
        return session.run_torrent(path, Target(self.cwd, fixed_name),
                                   lambda pct, text: self.progress.append((pct, text)),
                                   handle or TorrentHandle(), opts, log)

    def listing(self):
        return sorted(p.name for p in self.cwd.iterdir())

    def kept(self, t, *others):
        """What a kept partial leaves in the folder (the .dm-part item) next to `others`."""
        from dmcore import resume

        meta = metainfo.load(t.write_torrent(self.dir / "k.torrent"))
        key = resume.torrent_key(meta.info_hash, Target(self.cwd, None))
        return sorted([resume.paths(self.cwd, layout.item_name(meta), key).part.name, *others])

    def single(self, size=4 * 1024 * 1024, piece_length=64 * 1024, **kw):
        return self.torrent([("a.bin", torrentkit.content(size))], name="a.bin", piece_length=piece_length,
                            **kw)

    def swarm(self, tr, *seeders):
        tr.static = [("127.0.0.1", sd.port) for sd in seeders]

    def wait_for(self, cond, seconds=5):
        end = time.monotonic() + seconds
        while not cond():
            if time.monotonic() > end:
                return False
            time.sleep(0.02)
        return True


class Download(Harness):
    """AC3, AC4, AC9, AC10, AC11, AC12: run_torrent against the fixture tracker and seeders."""

    # AC4
    def test_single_file_4mib_from_one_seeder(self):
        tr = self.serve_tracker()
        t = self.single(tr=tr)
        self.swarm(tr, self.seeder(t))
        final, size, nfiles = self.run_torrent(t)
        self.assertEqual((final, size, nfiles), (self.cwd / "a.bin", 4 * 1024 * 1024, 1))
        self.assertEqual(final.read_bytes(), t.blob)
        self.assertEqual(self.listing(), ["a.bin"])

    def test_a_failing_live_view_does_not_stop_the_download(self):
        tr = self.serve_tracker()
        t = self.torrent([("a", torrentkit.content(60000, "a")), ("b", torrentkit.content(60000, "b"))],
                         name="pack", tr=tr, piece_length=PL)
        self.swarm(tr, self.seeder(t, delay=0.01))
        path = t.write_torrent(self.dir / "x.torrent")
        logs = []

        def broken(*args, **kw):
            raise RuntimeError("secret detail")

        final, size, nfiles = session.run_torrent(path, Target(self.cwd, None), broken, TorrentHandle(), FAST,
                                                  logs.append, on_stats=broken)
        self.assertEqual((size, nfiles), (120000, 2))
        self.assertEqual(torrentkit.tree(final), {"a": torrentkit.content(60000, "a"),
                                                  "b": torrentkit.content(60000, "b")})
        failed = [m for m in logs if "RuntimeError" in m]
        self.assertEqual(len(failed), 1, logs)  # the class of the first failure, once, never again
        self.assertFalse([m for m in logs if "secret" in m], logs)

    def test_multi_file_with_nested_and_empty_files(self):
        tr = self.serve_tracker()
        files = [("one.txt", torrentkit.content(70000, "1")), ("sub/two.bin", torrentkit.content(40000, "2")),
                 ("sub/deep/empty", b""), ("three", torrentkit.content(5, "3"))]
        t = self.torrent(files, name="album", tr=tr, piece_length=32768)
        self.swarm(tr, self.seeder(t))
        final, size, nfiles = self.run_torrent(t)
        self.assertEqual((final, nfiles, size), (self.cwd / "album", 4, 70000 + 40000 + 5))
        self.assertEqual(torrentkit.tree(final), {k: v for k, v in files})
        self.assertEqual(self.listing(), ["album"])

    def test_folder_gets_the_normal_mode_not_0700(self):
        tr = self.serve_tracker()
        t = self.torrent([("a", b"x" * 100), ("b", b"y" * 100)], name="d", tr=tr)
        self.swarm(tr, self.seeder(t))
        old = os.umask(0o022)
        try:
            final, _, _ = self.run_torrent(t)
        finally:
            os.umask(old)
        self.assertEqual(stat.S_IMODE(final.stat().st_mode), 0o755)

    def test_existing_folder_is_left_alone_and_the_download_gets_a_numbered_name(self):
        tr = self.serve_tracker()
        t = self.torrent([("a", b"x" * 100), ("b", b"y" * 100)], name="dataset", tr=tr)
        self.swarm(tr, self.seeder(t))
        (self.cwd / "dataset").mkdir()
        (self.cwd / "dataset" / "mine").write_text("keep")
        final, _, _ = self.run_torrent(t)
        self.assertEqual(final.name, "dataset (1)")
        self.assertEqual(torrentkit.tree(self.cwd / "dataset"), {"mine": b"keep"})
        self.assertEqual(torrentkit.tree(final), {"a": b"x" * 100, "b": b"y" * 100})

    def test_fixed_name_names_the_top_level_item(self):
        tr = self.serve_tracker()
        t = self.single(size=50000, tr=tr)
        self.swarm(tr, self.seeder(t))
        final, _, _ = self.run_torrent(t, fixed_name="renamed.dat")
        self.assertEqual(final, self.cwd / "renamed.dat")
        self.assertEqual(self.listing(), ["renamed.dat"])

    def test_a_very_long_name_keeps_the_temp_name_within_the_limit(self):
        tr = self.serve_tracker()
        name = "n" * 250
        t = self.torrent([(name, b"z" * 40000)], name=name, tr=tr)
        self.swarm(tr, self.seeder(t))
        seen = []
        real = session.Storage.prepare

        def spy(storage):
            seen.append(os.path.basename(storage.root))
            return real(storage)

        with mock.patch.object(session.Storage, "prepare", spy):
            final, _, _ = self.run_torrent(t)
        self.assertLessEqual(len(seen[0].encode()), 255)
        self.assertEqual(final.name, name)

    # AC3
    def test_http_compact_dict_and_udp_trackers_all_work(self):
        for mode, scheme in (("compact", "http"), ("dict", "http"), ("compact", "udp")):
            with self.subTest(mode=mode, scheme=scheme):
                cwd = self.cwd / f"{mode}-{scheme}"
                cwd.mkdir()
                tr = self.serve_tracker(mode=mode)
                t = self.single(size=100000, tr=tr, scheme=scheme)
                self.swarm(tr, self.seeder(t))
                path = t.write_torrent(self.dir / "m.torrent")
                final, _, _ = session.run_torrent(path, Target(cwd, None), lambda *a: None,
                                                  TorrentHandle(), FAST)
                self.assertEqual(final.read_bytes(), t.blob)
                self.assertEqual(tr.announces[0]["event"], "started")

    def test_udp_tracker_that_drops_the_first_two_packets(self):
        tr = self.serve_tracker(drop=2)
        t = self.single(size=100000, tr=tr, scheme="udp")
        self.swarm(tr, self.seeder(t))
        with mock.patch.object(tracker_mod, "UDP_TIMEOUT", 0.15):
            final, _, _ = self.run_torrent(t, opts=session.Options(dht=False, stall_s=10, connect_timeout=2))
        self.assertEqual(final.read_bytes(), t.blob)

    def test_a_dead_udp_tracker_does_not_hold_up_a_live_one_in_its_tier(self):
        dead = self.serve_tracker(drop=10 ** 6)  # reads datagrams, never answers
        good = self.serve_tracker()
        t = torrentkit.make_torrent([("a.bin", torrentkit.content(50000))], PL, name="a.bin", announce_list=[
            [f"udp://127.0.0.1:{dead.udp_port}/announce", f"http://127.0.0.1:{good.http_port}/announce"]])
        self.swarm(good, self.seeder(t))
        start = time.monotonic()
        final, _, _ = self.run_torrent(t, opts=session.Options(dht=False, stall_s=30, connect_timeout=2))
        self.assertEqual(final.read_bytes(), t.blob)
        self.assertLess(time.monotonic() - start, 10)

    def test_a_tier_of_only_dead_trackers_fails_within_the_budget(self):
        dead = self.serve_tracker(drop=10 ** 6)
        t = self.single(size=50000, tr=dead, scheme="udp")
        start = time.monotonic()
        with self.assertRaises(DmError) as cm:
            self.run_torrent(t, opts=session.Options(dht=False, stall_s=30, tracker_budget=0.5))
        self.assertEqual(str(cm.exception), "tracker error: timed out")
        self.assertLess(time.monotonic() - start, 10)

    def test_a_worker_that_crashes_fails_the_download_at_once(self):
        tr = self.serve_tracker()
        t = self.single(size=50000, tr=tr)
        self.swarm(tr, self.seeder(t))

        async def boom(self):
            raise RuntimeError("secret detail")

        start = time.monotonic()
        with mock.patch.object(session.Session, "connector", boom):
            with self.assertRaises(DmError) as cm:
                self.run_torrent(t, opts=session.Options(dht=False, stall_s=60))
        self.assertNotIn("secret", str(cm.exception))
        self.assertLess(time.monotonic() - start, 10)
        self.assertEqual(self.listing(), [])

    def test_every_tracker_failing_is_a_tracker_error_with_the_reason(self):
        tr = self.serve_tracker(mode="fail")
        t = self.single(size=100000, tr=tr, query="?passkey=SECRET")
        with self.assertRaises(DmError) as cm:
            self.run_torrent(t)
        self.assertEqual(str(cm.exception), "tracker error: tracker says no")
        self.assertTrue(cm.exception.keep)
        self.assertEqual(self.listing(), [])  # retryable, but nothing verified: nothing is kept

    def test_next_tracker_is_used_when_the_first_fails(self):
        bad = self.serve_tracker(mode="fail")
        good = self.serve_tracker()
        files = [("a.bin", torrentkit.content(50000))]
        t = torrentkit.make_torrent(files, PL, name="a.bin", announce_list=[
            [f"http://127.0.0.1:{bad.http_port}/announce"], [f"http://127.0.0.1:{good.http_port}/announce"]])
        self.swarm(good, self.seeder(t))
        final, _, _ = self.run_torrent(t)
        self.assertEqual(final.read_bytes(), t.blob)

    def test_completed_and_stopped_are_announced(self):
        tr = self.serve_tracker()
        t = self.single(size=50000, tr=tr)
        self.swarm(tr, self.seeder(t))
        self.run_torrent(t)
        self.assertEqual([a["event"] for a in tr.announces], ["started", "completed", "stopped"])

    # AC4: hostile peers
    def test_corrupt_piece_disconnects_the_sender_and_comes_from_the_other_seeder(self):
        tr = self.serve_tracker()
        t = self.single(size=64 * PL, tr=tr, piece_length=PL)
        bad = self.seeder(t, corrupt=set(range(64)))
        good = self.seeder(t, delay=0.01)
        self.swarm(tr, bad, good)
        final, _, _ = self.run_torrent(t)
        self.assertEqual(final.read_bytes(), t.blob)
        self.assertTrue(self.wait_for(lambda: bad.open_connections == 0))
        self.assertEqual(bad.connections, 1)
        # nothing more was asked of it after its first bad piece
        self.assertLessEqual(bad.requests, FAST.pipeline)

    def test_unrequested_block_disconnects_the_peer(self):
        tr = self.serve_tracker()
        t = self.single(size=16 * PL, tr=tr, piece_length=PL)
        bad = self.seeder(t, unrequested=True)
        good = self.seeder(t, delay=0.02)
        self.swarm(tr, bad, good)
        final, _, _ = self.run_torrent(t)
        self.assertEqual(final.read_bytes(), t.blob)
        self.assertTrue(self.wait_for(lambda: bad.open_connections == 0))
        self.assertEqual(bad.connections, 1)

    # AC9
    def test_progress_reaches_100_and_names_peers_without_their_addresses(self):
        tr = self.serve_tracker()
        t = self.single(size=32 * PL, tr=tr, piece_length=PL)
        self.swarm(tr, self.seeder(t))
        self.run_torrent(t)
        pcts = [p for p, _ in self.progress]
        self.assertEqual(pcts[-1], 100)
        self.assertEqual(pcts, sorted(pcts))
        for _, text in self.progress:
            self.assertRegex(text, r"^[\d.]+ (B|kB|MB) of 524\.3 kB · \d+ peers?$")
            self.assertNotIn("127.0.0.1", text)

    # AC11
    def test_passkey_and_peer_addresses_stay_out_of_user_visible_text(self):
        tr = self.serve_tracker()
        t = self.single(size=50000, tr=tr, query="?passkey=SECRET")
        self.swarm(tr, self.seeder(t))
        logs = []
        self.run_torrent(t, log=logs.append)
        self.assertIn("passkey=SECRET", tr.announces[0]["query"])  # it was sent to the tracker
        shown = " ".join(text for _, text in self.progress)
        self.assertNotIn("SECRET", shown)
        self.assertNotIn("127.0.0.1", shown)
        self.assertFalse([m for m in logs if "SECRET" in m])

    # AC12
    def test_no_peers_found_after_the_stall_time(self):
        tr = self.serve_tracker(mode="empty")
        t = self.single(size=50000, tr=tr)
        start = time.monotonic()
        with self.assertRaises(DmError) as cm:
            self.run_torrent(t, opts=session.Options(dht=False, stall_s=1))
        self.assertEqual(str(cm.exception), "no peers found")
        self.assertLess(time.monotonic() - start, 4)
        self.assertEqual(self.listing(), [])

    def test_a_torrent_without_usable_trackers_has_no_peers(self):
        t = self.single(size=50000)
        with self.assertRaises(DmError) as cm:
            self.run_torrent(t, opts=session.Options(dht=False, stall_s=1))
        self.assertEqual(str(cm.exception), "no peers found")

    def test_peers_that_never_unchoke_stall_the_download(self):
        tr = self.serve_tracker()
        t = self.single(size=50000, tr=tr)
        self.swarm(tr, self.seeder(t, never_unchoke=True))
        start = time.monotonic()
        with self.assertRaises(DmError) as cm:
            self.run_torrent(t, opts=session.Options(dht=False, stall_s=1, idle_timeout=30))
        self.assertEqual(str(cm.exception), "stalled: no progress for 2.5 seconds")
        self.assertLess(time.monotonic() - start, 6)
        self.assertEqual(self.listing(), [])

    # AC10
    def test_cancel_stops_within_two_seconds_and_keeps_the_partial(self):
        tr = self.serve_tracker()
        t = self.single(size=64 * PL, tr=tr, piece_length=PL)
        seeder = self.seeder(t, delay=0.05)
        self.swarm(tr, seeder)
        handle, result = TorrentHandle(), {}

        def work():
            try:
                result["ok"] = self.run_torrent(t, handle=handle)
            except BaseException as e:
                result["err"] = e
            result["end"] = time.monotonic()

        thread = threading.Thread(target=work)
        thread.start()
        self.assertTrue(self.wait_for(lambda: seeder.requests >= 3, 10))
        self.assertTrue(list(self.cwd.glob(".*.dm-part")))
        start = time.monotonic()
        self.assertTrue(handle.abort())
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertLess(result["end"] - start, 2)
        self.assertIsInstance(result["err"], Cancelled)
        self.assertEqual(self.listing(), self.kept(t))
        self.assertTrue(self.wait_for(lambda: seeder.open_connections == 0))
        self.assertTrue(self.wait_for(lambda: tr.announces[-1]["event"] == "stopped"))

    def test_the_file_is_in_place_before_the_tracker_farewell_and_a_cancel_then_keeps_it(self):
        tr = self.serve_tracker(delay={"completed": 3})
        t = self.single(size=8 * PL, tr=tr, piece_length=PL)
        self.swarm(tr, self.seeder(t))
        handle, result = TorrentHandle(), {}

        def work():
            try:
                result["ok"] = self.run_torrent(t, handle=handle)
            except BaseException as e:
                result["err"] = e

        thread = threading.Thread(target=work)
        thread.start()
        self.assertTrue(self.wait_for(lambda: any(a["event"] == "completed" for a in tr.announces), 10))
        self.assertTrue(thread.is_alive())  # the farewell is still waiting for the tracker
        self.assertEqual([n for n in self.listing() if not n.endswith(".dm-lock")], ["a.bin"])
        self.assertEqual((self.cwd / "a.bin").read_bytes(), t.blob)
        self.assertFalse(handle.abort())  # too late to cancel: it is saved
        thread.join(10)
        self.assertNotIn("err", result)
        self.assertEqual(result["ok"][0], self.cwd / "a.bin")
        self.assertEqual(self.listing(), ["a.bin"])

    def test_a_tracker_failure_reason_never_shows_the_passkey_or_c1_controls(self):
        key = "k3yPASSKEY0123456789"
        tr = self.serve_tracker(mode="fail", reason=f"bad passkey {key} \u0085here")
        t = self.single(size=50000, tr=tr, query=f"?passkey={key}")
        with self.assertRaises(DmError) as cm:
            self.run_torrent(t)
        text = str(cm.exception)
        self.assertNotIn(key, text)
        self.assertNotIn("\u0085", text)
        self.assertIn("bad passkey", text)

    def test_abort_after_the_move_into_place_is_refused(self):
        h = TorrentHandle()
        h.placed = True
        self.assertFalse(h.abort())
        self.assertFalse(h.aborted)

    # NFR3: a full disk
    def test_a_disk_full_error_becomes_a_user_message_and_deletes_the_empty_partial(self):
        tr = self.serve_tracker()
        t = self.single(size=50000, tr=tr)
        self.swarm(tr, self.seeder(t))
        err = OSError(errno.ENOSPC, "No space left on device")
        with mock.patch.object(session.Storage, "write_piece", side_effect=err):
            with self.assertRaises(DmError) as cm:
                self.run_torrent(t)
        self.assertEqual(str(cm.exception), f"cannot write to {self.cwd}: No space left on device")
        self.assertTrue(cm.exception.keep)
        self.assertEqual(self.listing(), [])  # nothing was verified, so nothing is kept

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores folder permissions")
    def test_a_read_only_folder_gives_the_same_message_for_single_and_multi_file(self):
        self.cwd.chmod(0o555)
        self.addCleanup(self.cwd.chmod, 0o755)
        single = self.single(size=50000)
        multi = self.torrent([("x", b"1" * 20000), ("y/z", b"2" * 20000)], name="pack")
        for t in (single, multi):
            with self.assertRaises(DmError) as cm:
                self.run_torrent(t)
            self.assertEqual(str(cm.exception), f"cannot write to {self.cwd}: Permission denied")


class EventLoopDelay(Harness):
    """AC14 (NFR4): checking and writing pieces never holds up the loop that talks to the peers."""

    def lag_while(self, run):
        """Run `run()` while a task in the session's loop records how late its 10 ms sleeps wake up."""
        worst = []
        real = session.run_session

        async def watched(*args, **kw):
            async def watch():
                loop = asyncio.get_running_loop()
                while True:
                    before = loop.time()
                    await asyncio.sleep(0.01)
                    worst.append(loop.time() - before - 0.01)

            task = asyncio.ensure_future(watch())
            try:
                await real(*args, **kw)
            finally:
                task.cancel()

        with mock.patch.object(session, "run_session", watched):
            run()
        self.assertGreater(len(worst), 10)
        return max(worst)

    def test_downloading_32_mib_in_1_mib_pieces_keeps_the_loop_responsive(self):
        tr = self.serve_tracker()
        t = self.single(size=32 * 1024 * 1024, piece_length=1024 * 1024, tr=tr)
        self.swarm(tr, self.seeder(t))
        lag = self.lag_while(lambda: self.run_torrent(t, opts=session.Options(dht=False, stall_s=20, idle_timeout=20)))
        print(f"\n[perf] worst event-loop delay: {lag * 1000:.1f} ms", file=sys.stderr)
        self.assertLess(lag, 0.1)

    def test_a_slow_hash_check_runs_off_the_loop(self):
        real = hashlib.sha1

        def slow(data=b""):
            time.sleep(0.2)  # stands for a huge piece; sleeping in a worker thread leaves the loop alone
            return real(data)

        tr = self.serve_tracker()
        t = self.single(size=8 * PL, piece_length=PL, tr=tr)
        self.swarm(tr, self.seeder(t))
        with mock.patch.object(session.hashlib, "sha1", slow):
            lag = self.lag_while(lambda: self.run_torrent(t))
        self.assertLess(lag, 0.1)


class Run(Harness):
    """AC1, AC8, AC10: dmcore.run with a fake notify, the way the plugin calls it."""

    def dm(self, arg, cancelled=None, **inputs):
        events = []
        code = dmcore.run({"url": arg, "no_dht": True, **inputs}, str(self.cwd),
                          lambda method, **p: events.append((method, p)), cancelled or threading.Event())
        self.events = events
        return code

    def errors(self):
        return [p["message"] for m, p in self.events if m == "log" and p["level"] == "error"]

    def output(self):
        return "".join(p["text"] for m, p in self.events if m == "output")

    def prepared(self, files, name, **kw):
        tr = self.serve_tracker(**kw)
        t = self.torrent(files, name=name, tr=tr)
        self.swarm(tr, self.seeder(t))
        t.write_torrent(self.cwd / "data.torrent")
        return t

    # AC1
    def test_a_torrent_file_in_the_folder_is_downloaded(self):
        t = self.prepared([("a.bin", torrentkit.content(50000))], "a.bin")
        self.assertEqual(self.dm("data.torrent"), 0, self.errors())
        self.assertEqual(self.output(), "saved a.bin (50.0 kB, 1 file)\n")
        self.assertEqual((self.cwd / "a.bin").read_bytes(), t.blob)
        self.assertEqual(self.listing(), ["a.bin", "data.torrent"])
        self.assertTrue([m for m, _ in self.events if m == "bar"])
        self.assertTrue([p for m, p in self.events if m == "log" and "peak_rss_kb=" in p["message"]])

    def timed(self, arg, **inputs):
        """dmcore.run with the time of every event: (exit code, [(seconds, method, params)])."""
        events = []
        start = time.monotonic()
        code = dmcore.run({"url": arg, "no_dht": True, **inputs}, str(self.cwd),
                          lambda method, **p: events.append((time.monotonic() - start, method, p)), threading.Event())
        self.events = [(m, p) for _, m, p in events]
        return code, events

    # AC15 (R21, R22)
    def test_a_three_file_torrent_shows_file_bars_a_total_a_panel_and_a_peers_table(self):
        tr = self.serve_tracker()
        t = self.torrent([("x", torrentkit.content(90000, "x")), ("y/z", torrentkit.content(60000, "z")),
                          ("w", torrentkit.content(40000, "w"))], name="pack", tr=tr, piece_length=PL)
        self.swarm(tr, self.seeder(t, delay=0.03))
        t.write_torrent(self.cwd / "data.torrent")
        code, events = self.timed("data.torrent")
        self.assertEqual(code, 0, self.errors())
        self.assertEqual(self.output(), "saved pack (190.0 kB, 3 files)\n")
        bars = {}
        for _, m, p in events:
            if m == "bar":
                bars.setdefault(p["id"], []).append(p)
        self.assertEqual(sorted(bars), ["f0", "f1", "f2", "total"])
        self.assertEqual({bars[f"f{i}"][0]["label"] for i in range(3)}, {"x", "y/z", "w"})
        for id, items in bars.items():
            self.assertEqual([p.get("done", False) for p in items].count(True), 1, id)
            self.assertTrue(items[-1]["done"], id)
        panels = [p for _, m, p in events if m == "panel"]
        self.assertTrue(panels and {k for k, _ in panels[-1]["items"]} == {"down", "up", "peers", "eta", "ratio"})
        tables = [p for _, m, p in events if m == "table"]
        self.assertTrue(tables)
        self.assertTrue(any(row[0].startswith("127.0.0.1:") for t_ in tables for row in t_["rows"]))
        # success clears nothing: the host freezes the panel and the table, as last updated, into scrollback
        self.assertEqual([p for _, m, p in events if m == "clear"], [])
        saved = next(i for i, (_, m, _) in enumerate(events) if m == "output")
        last_panel = max(i for i, (_, m, _) in enumerate(events) if m == "panel")
        self.assertLess(last_panel, saved, "the final update comes before the saved line")

    def test_updates_are_at_most_five_a_second_for_each_area(self):
        tr = self.serve_tracker()
        t = self.torrent([("a", torrentkit.content(120000, "a")), ("b", torrentkit.content(120000, "b"))],
                         name="pack", tr=tr, piece_length=PL)
        self.swarm(tr, self.seeder(t, delay=0.01))
        t.write_torrent(self.cwd / "data.torrent")
        code, events = self.timed("data.torrent")
        self.assertEqual(code, 0, self.errors())
        per_area = {}
        for at, m, p in events:
            if m in ("bar", "panel", "table"):
                per_area.setdefault((m, p["id"]), []).append((at, p.get("done", False)))
        for key, items in per_area.items():
            live = [at for at, done in items if not done]  # the closing update is exempt
            for i, at in enumerate(live):
                in_second = [x for x in live[i:] if x < at + 1]
                self.assertLessEqual(len(in_second), 5, (key, in_second))

    def test_no_peer_address_reaches_a_log_or_the_output(self):
        tr = self.serve_tracker()
        t = self.torrent([("a", torrentkit.content(60000, "a")), ("b", torrentkit.content(60000, "b"))],
                         name="pack", tr=tr, piece_length=PL)
        self.swarm(tr, self.seeder(t, delay=0.01))
        t.write_torrent(self.cwd / "data.torrent")
        self.timed("data.torrent")
        # every level, debug included (`dock -v` prints it), and every message but the peers table, the one
        # place an address may be: the host draws it
        shown = " ".join(str(v) for m, p in self.events if m != "table" for v in p.values())
        self.assertNotIn("127.0.0.1", shown)
        self.assertTrue([1 for m, p in self.events if m == "log" and p["level"] == "debug"
                         and p["message"].startswith("peer #")], "peers are still logged, by number")

    def test_a_failed_torrent_leaves_no_area_behind(self):
        tr = self.serve_tracker()  # nobody announces it
        t = self.torrent([("a", torrentkit.content(60000, "a")), ("b", torrentkit.content(60000, "b"))],
                         name="pack", tr=tr, piece_length=PL)
        t.write_torrent(self.cwd / "data.torrent")
        code, events = self.timed("data.torrent", stall=1)
        self.assertEqual(code, 1)
        shown = {(m, p["id"]) for _, m, p in events if m in ("bar", "panel", "table")}
        cleared = {(p["kind"], p["id"]) for _, m, p in events if m == "clear"}
        self.assertTrue(shown)
        self.assertEqual(shown, cleared)
        self.assertFalse([1 for _, m, p in events if m == "bar" and p.get("done")])

    def test_a_multi_file_torrent_reports_the_file_count(self):
        self.prepared([("x", b"1" * 40000), ("y/z", b"2" * 40000), ("e", b"")], "pack")
        self.assertEqual(self.dm("data.torrent"), 0, self.errors())
        self.assertEqual(self.output(), "saved pack (80.0 kB, 3 files)\n")
        self.assertEqual(sorted(torrentkit.tree(self.cwd / "pack")), ["e", "x", "y/z"])

    def test_the_extension_is_matched_in_any_case(self):
        self.prepared([("a.bin", b"q" * 20000)], "a.bin")
        (self.cwd / "data.torrent").rename(self.cwd / "DATA.TORRENT")
        self.assertEqual(self.dm("DATA.TORRENT"), 0, self.errors())

    def test_a_url_ending_in_torrent_is_still_an_http_download(self):
        with mock.patch.object(fetch, "download", return_value=(self.cwd / "x.torrent", 5)) as dl:
            self.assertEqual(self.dm("https://example.invalid/x.torrent"), 0)
        dl.assert_called_once()

    def test_a_torrent_outside_the_folder_is_refused_without_a_prefix(self):
        (self.dir / "x.torrent").write_bytes(b"d4:infod4:name1:aee")
        self.assertEqual(self.dm("../x.torrent"), 1)
        self.assertEqual(self.errors(), ["dm: ../x.torrent is outside the current folder"])

    def test_a_symlink_to_a_torrent_outside_is_refused(self):
        (self.dir / "x.torrent").write_bytes(b"de")
        os.symlink(self.dir / "x.torrent", self.cwd / "link.torrent")
        self.assertEqual(self.dm("link.torrent"), 1)
        self.assertEqual(self.errors(), ["dm: link.torrent is outside the current folder"])

    def test_a_missing_torrent_file_is_an_invalid_url(self):
        self.assertEqual(self.dm("missing.torrent"), 1)
        self.assertEqual(self.errors(), ["dm: invalid URL"])

    def test_errors_carry_the_torrent_name(self):
        (self.cwd / "bad.torrent").write_bytes(b"not bencode")
        self.assertEqual(self.dm("bad.torrent"), 1)
        self.assertRegex(self.errors()[0], r"^dm: bad\.torrent: invalid torrent: ")
        self.prepared([("a.bin", b"q" * 20000)], "a.bin", mode="fail")
        self.assertEqual(self.dm("data.torrent"), 1)
        self.assertEqual(self.errors(), ["dm: data.torrent: tracker error: tracker says no"])

    def test_the_torrent_name_is_shown_as_given(self):
        (self.cwd / "my file \u00e9.torrent").write_bytes(b"x")
        self.assertEqual(self.dm("my file \u00e9.torrent"), 1)
        self.assertTrue(self.errors()[0].startswith("dm: my file \u00e9.torrent: invalid torrent"))

    # AC8
    def test_dir_places_the_result_in_that_folder(self):
        self.prepared([("a.bin", b"q" * 20000)], "a.bin")
        (self.cwd / "sub").mkdir()
        self.assertEqual(self.dm("data.torrent", dir="sub"), 0, self.errors())
        self.assertEqual(self.output(), "saved sub/a.bin (20.0 kB, 1 file)\n")

    def test_output_names_the_top_level_item(self):
        self.prepared([("a", b"1" * 20000), ("b", b"2" * 20000)], "pack")
        self.assertEqual(self.dm("data.torrent", output="renamed"), 0, self.errors())
        self.assertEqual(sorted(p.name for p in self.cwd.iterdir()), ["data.torrent", "renamed"])

    def test_output_and_dir_together_is_a_usage_error(self):
        self.prepared([("a.bin", b"q" * 20000)], "a.bin")
        self.assertEqual(self.dm("data.torrent", output="a", dir="b"), 2)

    # AC10
    def test_cancel_raises_cancelled_and_keeps_the_partial(self):
        tr = self.serve_tracker()
        t = self.torrent([("a.bin", torrentkit.content(64 * PL))], name="a.bin", tr=tr)
        seeder = self.seeder(t, delay=0.05)
        self.swarm(tr, seeder)
        t.write_torrent(self.cwd / "data.torrent")
        cancelled, result = threading.Event(), {}

        def work():
            try:
                self.dm("data.torrent", cancelled=cancelled)
            except BaseException as e:
                result["err"] = e
            result["end"] = time.monotonic()

        thread = threading.Thread(target=work)
        thread.start()
        self.assertTrue(self.wait_for(lambda: seeder.requests >= 3, 10))
        self.assertTrue(list(self.cwd.glob(".*.dm-part")))
        start = time.monotonic()
        cancelled.set()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertLess(result["end"] - start, 2)
        self.assertIsInstance(result["err"], Cancelled)
        self.assertEqual(self.listing(), self.kept(t, "data.torrent"))

    # AC12
    def test_stall_seconds_sets_the_no_peers_time(self):
        t = self.prepared([("a.bin", b"x" * 5000)], "a.bin", mode="empty")
        start = time.monotonic()
        self.assertEqual(self.dm("data.torrent", stall=1), 1)
        self.assertEqual(self.errors(), ["dm: data.torrent: no peers found"])
        self.assertLess(time.monotonic() - start, 5)
        self.assertEqual(self.listing(), ["data.torrent"])

    def test_stall_seconds_sets_the_stalled_time(self):
        tr = self.serve_tracker()
        t = self.torrent([("a.bin", b"x" * 5000)], name="a.bin", tr=tr)
        self.swarm(tr, self.seeder(t, never_unchoke=True))
        t.write_torrent(self.cwd / "data.torrent")
        self.assertEqual(self.dm("data.torrent", stall=1), 1)
        self.assertEqual(self.errors(), ["dm: data.torrent: stalled: no progress for 2.5 seconds"])

    def test_a_stall_time_below_one_second_is_a_usage_error(self):
        self.prepared([("a.bin", b"x" * 5000)], "a.bin")
        for bad in (0, -5):
            with self.subTest(bad):
                self.assertEqual(self.dm("data.torrent", stall=bad), 2)
                self.assertEqual(self.errors(), ["dm: --stall-seconds must be at least 1"])


if __name__ == "__main__":
    unittest.main()
