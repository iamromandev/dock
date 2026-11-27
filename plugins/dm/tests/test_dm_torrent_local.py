"""dm torrent support in one process: fixture tracker and peers on 127.0.0.1, no container runtime needed.

The fixtures run on a background loop (torrentkit.Background); the code under test blocks the test thread.
"""

import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import torrentkit
import dmcore
from dmcore import Cancelled
from dmcore.target import Target
from dmcore.torrent import layout, metainfo, session
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
        return session.run_torrent(
            path,
            Target(self.cwd, fixed_name),
            lambda pct, text: self.progress.append((pct, text)),
            handle or TorrentHandle(),
            opts,
            log,
        )

    def listing(self):
        return sorted(p.name for p in self.cwd.iterdir())

    def kept(self, t, *others):
        """What a kept partial leaves in the folder (the .dm-part item) next to `others`."""
        from dmcore import resume

        meta = metainfo.load(t.write_torrent(self.dir / "k.torrent"))
        key = resume.torrent_key(meta.info_hash, Target(self.cwd, None))
        return sorted([resume.paths(self.cwd, layout.item_name(meta), key).part.name, *others])

    def single(self, size=4 * 1024 * 1024, piece_length=64 * 1024, **kw):
        return self.torrent(
            [("a.bin", torrentkit.content(size))], name="a.bin", piece_length=piece_length, **kw
        )

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

    def test_a_failing_live_view_does_not_stop_the_download(self):
        tr = self.serve_tracker()
        t = self.torrent(
            [("a", torrentkit.content(60000, "a")), ("b", torrentkit.content(60000, "b"))],
            name="pack",
            tr=tr,
            piece_length=PL,
        )
        self.swarm(tr, self.seeder(t, delay=0.01))
        path = t.write_torrent(self.dir / "x.torrent")
        logs = []

        def broken(*args, **kw):
            raise RuntimeError("secret detail")

        final, size, nfiles = session.run_torrent(
            path, Target(self.cwd, None), broken, TorrentHandle(), FAST, logs.append, on_stats=broken
        )
        self.assertEqual((size, nfiles), (120000, 2))
        self.assertEqual(
            torrentkit.tree(final), {"a": torrentkit.content(60000, "a"), "b": torrentkit.content(60000, "b")}
        )
        failed = [m for m in logs if "RuntimeError" in m]
        self.assertEqual(len(failed), 1, logs)  # the class of the first failure, once, never again
        self.assertFalse([m for m in logs if "secret" in m], logs)

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
                final, _, _ = session.run_torrent(
                    path, Target(cwd, None), lambda *a: None, TorrentHandle(), FAST
                )
                self.assertEqual(final.read_bytes(), t.blob)
                self.assertEqual(tr.announces[0]["event"], "started")

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

    # AC9

    # AC11

    # AC12

    # AC10

    # NFR3: a full disk


class Run(Harness):
    """AC1, AC8, AC10: dmcore.run with a fake notify, the way the plugin calls it."""

    def dm(self, arg, cancelled=None, **inputs):
        events = []
        code = dmcore.run(
            {"url": arg, "no_dht": True, **inputs},
            str(self.cwd),
            lambda method, **p: events.append((method, p)),
            cancelled or threading.Event(),
        )
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

    def timed(self, arg, **inputs):
        """dmcore.run with the time of every event: (exit code, [(seconds, method, params)])."""
        events = []
        start = time.monotonic()
        code = dmcore.run(
            {"url": arg, "no_dht": True, **inputs},
            str(self.cwd),
            lambda method, **p: events.append((time.monotonic() - start, method, p)),
            threading.Event(),
        )
        self.events = [(m, p) for _, m, p in events]
        return code, events

    # AC15 (R21, R22)

    def test_errors_carry_the_torrent_name(self):
        (self.cwd / "bad.torrent").write_bytes(b"not bencode")
        self.assertEqual(self.dm("bad.torrent"), 1)
        self.assertRegex(self.errors()[0], r"^dm: bad\.torrent: invalid torrent: ")
        self.prepared([("a.bin", b"q" * 20000)], "a.bin", mode="fail")
        self.assertEqual(self.dm("data.torrent"), 1)
        self.assertEqual(self.errors(), ["dm: data.torrent: tracker error: tracker says no"])

    # AC8

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

    def test_stall_seconds_sets_the_stalled_time(self):
        tr = self.serve_tracker()
        t = self.torrent([("a.bin", b"x" * 5000)], name="a.bin", tr=tr)
        self.swarm(tr, self.seeder(t, never_unchoke=True))
        t.write_torrent(self.cwd / "data.torrent")
        self.assertEqual(self.dm("data.torrent", stall=1), 1)
        self.assertEqual(self.errors(), ["dm: data.torrent: stalled: no progress for 2.5 seconds"])


if __name__ == "__main__":
    unittest.main()
