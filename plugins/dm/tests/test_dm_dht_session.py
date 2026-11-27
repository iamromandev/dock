"""The DHT inside a download (phase 2): wiring, the private flag, the lookup loop, the cache and cancel.

In one process: the fake DHT network of tests/fixtures/torrent/dht.py, the fixture tracker and seeders of
test_dm_torrent_local, all on 127.0.0.1. No Docker.
"""

import os
import re
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
sys.path.insert(0, str(ROOT / "tests" / "fixtures" / "torrent"))

import dht as dhtfix
import dmcore
import torrentkit
from dmcore.target import DmError
from dmcore.torrent import session
from dmcore.torrent.dht import cache
from dmcore.torrent.handle import TorrentHandle
from test_dm_magnet_local import MagnetHarness
from test_dm_torrent_local import PL

LINK = "magnet:?xt=urn:btih:c9e15763f722f23e98a29decdfae341b98d53056&tr=udp://203.0.113.9:1"
BARE = "magnet:?xt=urn:btih:c9e15763f722f23e98a29decdfae341b98d53056"
MAIN = ROOT / "main.py"


class Wiring(unittest.TestCase):
    """R4, R10, R13, R18: what dmcore.run hands the session."""

    def setUp(self):
        self.cwd = tempfile.mkdtemp(prefix="dm-dht-wire-")
        self.addCleanup(__import__("shutil").rmtree, self.cwd, ignore_errors=True)
        self.opts = []

    def dm(self, arg=LINK, data_dir=None, **inputs):
        def fake(magnet, target, progress, handle, opts, *a, **kw):
            self.opts.append(opts)
            raise DmError("stop here")

        kw = {} if data_dir is None else {"data_dir": data_dir}
        with mock.patch.object(session, "run_magnet", fake):
            return dmcore.run({"url": arg, **inputs}, self.cwd, lambda *a, **k: None, threading.Event(), **kw)

    def test_the_cache_is_named_in_the_data_dir_and_the_dht_is_on(self):
        self.dm(data_dir="/data/dm")
        (opts,) = self.opts
        self.assertTrue(opts.dht)
        self.assertEqual(opts.dht_cache, Path("/data/dm") / "dht-nodes.dat")


async def _stop(net):
    net.stop()


ADDRESS = re.compile(r"\d+\.\d+\.\d+\.\d+")


class DhtHarness(MagnetHarness):
    """A fake DHT network next to the fixture tracker and seeders, all on 127.0.0.1."""

    def network(self, **kw):
        net = dhtfix.DhtNetwork(**kw)
        self.bg.call(net.start("127.0.0.1"))
        self.addCleanup(self.bg.call, _stop(net))
        return net

    def opts(self, net, **kw):
        base = {
            "stall_s": 20,
            "connect_timeout": 2,
            "idle_timeout": 10,
            "dht": True,
            "dht_bootstrap": tuple(net.addrs),
            "dht_allow_loopback": True,
        }
        base.update(kw)
        return session.Options(**base)

    def known_by_dht(self, t, **seed_kw):
        """A seeder for t, and a network whose nodes near t's info-hash name it."""
        seeder = self.seeder(t, **seed_kw)
        net = self.network(n=50, info_hash=t.info_hash, peers=[("127.0.0.1", seeder.port)])
        return seeder, net


class PrivateDrop(unittest.IsolatedAsyncioTestCase):
    """R3: peers only the DHT named are dropped when the torrent turns out private."""

    def session(self, **kw):
        s = session.Session(
            None, None, lambda *a: None, TorrentHandle(), session.Options(**kw), info_hash=b"\x11" * 20
        )
        self.addCleanup(s.pool.shutdown, wait=False)
        self.addCleanup(s.reader_pool.shutdown, wait=False)
        return s

    async def test_dht_peers_not_yet_connected_are_dropped_and_others_stay(self):
        s = self.session()
        s.add_peers([("1.1.1.1", 1), ("2.2.2.2", 2)], from_dht=True)
        s.add_peers([("3.3.3.3", 3)])
        s.add_peers([("2.2.2.2", 2)])  # a tracker names it too: it stays
        s._go_private()
        self.assertEqual(list(s.pending), [("2.2.2.2", 2), ("3.3.3.3", 3)])
        self.assertEqual(s.queued, {("2.2.2.2", 2), ("3.3.3.3", 3)})
        self.assertEqual((s.private, s.dht_on, s.from_dht), (True, False, set()))


class FakeClient:
    """Stands for DhtClient: counts what the loop asks of it."""

    made = []

    def __init__(self, *a, **kw):
        self.lookups, self.closed, self.saved = 0, 0, 0
        self.stats = {"nodes": 3, "peers": 0}
        self.on_peers = None
        self.args, self.kw = a, kw
        FakeClient.made.append(self)

    async def lookup(self):
        self.lookups += 1
        return []

    def save_cache(self):
        self.saved += 1  # counts the asks: the real DhtClient writes nothing without peers
        return bool(self.stats["peers"])

    async def close(self):
        self.closed += 1


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Cache(DhtHarness):
    """AC9, R12, R13: the node cache in the data folder."""

    def setUp(self):
        super().setUp()
        self.data_dir = self.dir / "dm-data"
        self.data_dir.mkdir()
        self.path = self.data_dir / cache.CACHE_NAME

    def download(self, bootstrap=None, **kw):
        t = self.dead = getattr(self, "dead", None) or torrentkit.make_torrent(
            [("a.bin", torrentkit.content(16 * PL))], PL, announce="http://127.0.0.1:9/announce", name="a.bin"
        )
        if not hasattr(self, "net"):
            self.seeder_, self.net = self.known_by_dht(t)
        extra = {} if bootstrap is None else {"dht_bootstrap": bootstrap}
        for item in self.cwd.iterdir():
            item.unlink()
        self.run_torrent(t, self.opts(self.net, dht_cache=self.path, **extra, **kw))
        self.assertEqual((self.cwd / "a.bin").read_bytes(), t.blob)

    def saved(self):
        return cache.load(self.path, allow_loopback=True)

    def test_garbage_a_big_file_and_a_symlink_are_ignored_and_replaced_and_the_download_works(self):
        outside = self.dir / "outside"
        outside.write_bytes(b"precious")
        cases = {
            "garbage": lambda: self.path.write_bytes(os.urandom(500)),
            "1 MiB": lambda: self.path.write_bytes(b"x" * (1 << 20)),
            "symlink": lambda: self.path.symlink_to(outside),
        }
        for label, make in cases.items():
            with self.subTest(label):
                if self.path.is_symlink() or self.path.exists():
                    self.path.unlink()
                make()
                self.download()
                self.assertTrue(self.path.is_file() and not self.path.is_symlink())
                self.assertTrue(self.saved())
                self.assertEqual(outside.read_bytes(), b"precious")

    def test_a_missing_folder_does_not_fail_the_download(self):
        self.data_dir.rmdir()
        self.download()

    def test_a_private_magnet_leaves_no_cache_behind(self):
        t = torrentkit.make_torrent(
            [("a.bin", torrentkit.content(20 * PL))], PL, name="a.bin", extra_info={"private": 1}
        )
        _, net = self.known_by_dht(t, delay=0.01)
        self.run_magnet(torrentkit.magnet_for(t), self.opts(net, dht_cache=self.path))
        self.assertFalse(self.path.exists())


class Hostile(DhtHarness):
    """AC8 at the download level: bad nodes cost nothing, the honest ones still deliver."""

    def test_the_download_completes_with_nodes_that_each_misbehave(self):
        t = torrentkit.make_torrent(
            [("a.bin", torrentkit.content(16 * PL))], PL, announce="http://127.0.0.1:9/announce", name="a.bin"
        )
        seeder = self.seeder(t)
        net = self.network(n=60, info_hash=t.info_hash, peers=[("127.0.0.1", seeder.port)])
        for mode in (
            "oversize",
            "garbage",
            "deep",
            "flood",
            "self",
            "bad-addrs",
            "many-values",
            "dup-replies",
            "spoof",
            "wrong-tid",
            "private",
            "silent",
        ):
            net.hostile(mode, 2)
        self.run_torrent(t, self.opts(net))
        self.assertEqual((self.cwd / "a.bin").read_bytes(), t.blob)
        self.assertEqual(net.announces, [])
        self.assertEqual(net.dm_replies, [])
        self.assertTrue(all(q["ro"] == 1 and q["method"] == "get_peers" for q in net.queries))


class Ending(DhtHarness):
    """AC6: after success, failure or cancel, the network counts no more datagrams."""

    def settled(self, net):
        sent = net.datagrams
        time.sleep(0.4)
        self.assertEqual(net.datagrams, sent)

    def test_nothing_is_sent_after_a_failure(self):
        t = self.single(size=4 * PL, piece_length=PL)
        net = self.network(n=30)
        with mock.patch.object(session, "DHT_TICK", 0.02), mock.patch.object(session, "DHT_RETRY", 0.05):
            with self.assertRaises(DmError):
                self.run_magnet(torrentkit.magnet_for(t), self.opts(net, stall_s=1))
        self.settled(net)


def bencode_info(t):
    from dmcore.torrent import bencode

    top, span = bencode.decode(t.data)
    return t.data[span[0] : span[1]]


if __name__ == "__main__":
    unittest.main()
