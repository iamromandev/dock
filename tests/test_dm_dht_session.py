"""The DHT inside a download (phase 2): wiring, the private flag, the lookup loop, the cache and cancel.

In one process: the fake DHT network of tests/fixtures/torrent/dht.py, the fixture tracker and seeders of
test_dm_torrent_local, all on 127.0.0.1. No Docker.
"""

import asyncio
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "plugin" / "dm"))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tests" / "fixtures" / "torrent"))

import dht as dhtfix  # noqa: E402
import dmcore  # noqa: E402
import torrentkit  # noqa: E402
from dmcore import Cancelled  # noqa: E402
from dmcore.target import DmError, Target  # noqa: E402
from dmcore.torrent import magnet, metainfo, session  # noqa: E402
from dmcore.torrent.dht import cache  # noqa: E402
from dmcore.torrent.handle import TorrentHandle  # noqa: E402
from test_dm_magnet_local import MagnetHarness  # noqa: E402
from test_dm_torrent_local import PL  # noqa: E402

LINK = "magnet:?xt=urn:btih:c9e15763f722f23e98a29decdfae341b98d53056&tr=udp://203.0.113.9:1"
BARE = "magnet:?xt=urn:btih:c9e15763f722f23e98a29decdfae341b98d53056"
MAIN = ROOT / "plugin" / "dm" / "main.py"


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

    def test_without_a_data_dir_there_is_no_cache(self):
        self.dm()
        self.assertIsNone(self.opts[0].dht_cache)
        self.assertTrue(self.opts[0].dht)

    def test_no_dht_turns_the_dht_and_its_cache_off(self):
        self.dm(data_dir="/data/dm", no_dht=True)
        self.assertEqual((self.opts[0].dht, self.opts[0].dht_cache), (False, None))

    def test_the_address_switches_are_off_whatever_the_inputs_say(self):
        self.dm(data_dir="/data/dm", dht_allow_private=True, dht_allow_loopback=True, allow_private=True)
        self.assertEqual((self.opts[0].dht_allow_private, self.opts[0].dht_allow_loopback), (False, False))

    def test_a_magnet_with_only_the_hash_starts_when_the_dht_is_on(self):  # AC3 shape
        self.dm(BARE)
        self.assertEqual(len(self.opts), 1)

    def test_a_magnet_with_only_the_hash_and_no_dht_fails_before_any_session(self):  # AC5
        events = []
        with mock.patch.object(session, "run_magnet", side_effect=AssertionError("started")):
            code = dmcore.run({"url": BARE, "no_dht": True}, self.cwd,
                              lambda m, **p: events.append((m, p)), threading.Event())
        self.assertEqual(code, 1)
        self.assertEqual([p["message"] for m, p in events if m == "log"],
                         ["dm: magnet c9e15763…: magnet link has no trackers or peers (and the DHT is off)"])

    def test_the_default_options_use_the_three_public_bootstrap_hosts_and_no_loopback(self):
        o = session.Options()
        self.assertEqual([h for h, _ in o.dht_bootstrap],
                         ["router.bittorrent.com", "dht.transmissionbt.com", "router.utorrent.com"])
        self.assertEqual({p for _, p in o.dht_bootstrap}, {6881})
        self.assertEqual((o.dht, o.dht_allow_private, o.dht_allow_loopback), (True, False, False))


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
        base = {"stall_s": 20, "connect_timeout": 2, "idle_timeout": 10, "dht": True,
                "dht_bootstrap": tuple(net.addrs), "dht_allow_loopback": True}
        base.update(kw)
        return session.Options(**base)

    def known_by_dht(self, t, **seed_kw):
        """A seeder for t, and a network whose nodes near t's info-hash name it."""
        seeder = self.seeder(t, **seed_kw)
        net = self.network(n=50, info_hash=t.info_hash, peers=[("127.0.0.1", seeder.port)])
        return seeder, net


class Finding(DhtHarness):
    """AC1, AC2, AC3, R15, R16: the DHT is the only way to peers."""

    def dead_tracker_torrent(self, **kw):
        files = [("a.bin", torrentkit.content(16 * PL))]
        return torrentkit.make_torrent(files, PL, announce="http://127.0.0.1:9/announce", name="a.bin", **kw)

    def run_t(self, t, opts, log=None):
        path = t.write_torrent(self.dir / "x.torrent")
        self.progress = []
        return session.run_torrent(path, Target(self.cwd, None), lambda p, x: self.progress.append((p, x)),
                                   TorrentHandle(), opts, log)

    def run_m(self, link, opts, log=None):
        self.progress, self.notes = [], []
        return session.run_magnet(magnet.parse(link), Target(self.cwd, None),
                                  lambda p, x: self.progress.append((p, x)), TorrentHandle(), opts, log,
                                  note=self.notes.append)

    def test_a_torrent_with_a_dead_tracker_reaches_its_seeder_through_the_dht_within_5_seconds(self):  # AC1
        t = self.dead_tracker_torrent()
        seeder, net = self.known_by_dht(t)
        first, logs = [], []
        real = session.Session.add_peers

        def watch(s, peers, from_dht=False):
            if from_dht and not first:
                first.append(time.monotonic())
            return real(s, peers, from_dht)

        start = time.monotonic()
        with mock.patch.object(session.Session, "add_peers", watch):
            final, size, nfiles = self.run_t(t, self.opts(net), logs.append)
        self.assertEqual((final, nfiles), (self.cwd / "a.bin", 1))
        self.assertEqual((self.cwd / "a.bin").read_bytes(), t.blob)
        self.assertTrue(first)
        self.assertLess(first[0] - start, 5)
        self.assertGreaterEqual(seeder.connections, 1)
        self.__class__.first_peer_seconds = first[0] - start

    def test_the_log_has_one_dht_line_per_lookup_and_no_address(self):  # R16
        t = self.dead_tracker_torrent()
        _, net = self.known_by_dht(t, delay=0.05)  # slow enough for the lookup to end before the download
        logs = []
        self.run_t(t, self.opts(net), logs.append)
        lines = [m for m in logs if m.startswith("dht:")]
        self.assertTrue(lines)
        self.assertTrue(all(re.fullmatch(r"dht: lookup \d+, \d+ nodes, \d+ peers", m) for m in lines), lines)
        self.assertFalse([m for m in logs if ADDRESS.search(m)], logs)
        self.assertFalse([x for _, x in self.progress if ADDRESS.search(x)])

    def test_a_magnet_with_only_the_hash_shows_finding_peers_then_fetching_metadata_and_downloads(self):  # AC2
        t = self.single(size=16 * PL, piece_length=PL)
        _, net = self.known_by_dht(t, delay=0.01, metadata_delay=0.6)
        final, size, nfiles, info = self.run_m(magnet_link := torrentkit.magnet_for(t), self.opts(net))
        self.assertEqual((self.cwd / "a.bin").read_bytes(), t.blob)
        texts = [x for _, x in self.progress]
        self.assertEqual(texts[0], "finding peers (DHT)")
        self.assertTrue(any(x.startswith("fetching metadata") for x in texts), texts)
        self.assertLess(texts.index("finding peers (DHT)"),
                        min(i for i, x in enumerate(texts) if x.startswith("fetching metadata")))
        self.assertEqual(magnet.parse(magnet_link).trackers, ())

    def test_a_magnet_with_only_the_hash_and_a_network_that_knows_nobody_fails_at_the_stall_time(self):  # AC3
        t = self.single(size=4 * PL, piece_length=PL)
        net = self.network(n=30)
        start = time.monotonic()
        with self.assertRaises(DmError) as cm:
            self.run_m(torrentkit.magnet_for(t), self.opts(net, stall_s=2))
        elapsed = time.monotonic() - start
        self.assertEqual(str(cm.exception), "no peers found")
        self.assertGreaterEqual(elapsed, 2)
        self.assertLess(elapsed, 8)
        self.assertEqual(self.listing(), [])

    def test_with_the_dht_on_a_dead_tracker_alone_no_longer_fails_the_run_at_once(self):
        t = self.dead_tracker_torrent()
        net = self.network(n=20)
        start = time.monotonic()
        with self.assertRaises(DmError) as cm:
            self.run_t(t, self.opts(net, stall_s=3))
        self.assertEqual(str(cm.exception), "no peers found")
        self.assertGreaterEqual(time.monotonic() - start, 3)

    def test_with_the_dht_on_a_dead_tracker_failure_is_logged_without_an_address(self):
        t = self.dead_tracker_torrent()
        net = self.network(n=20)
        logs = []
        with self.assertRaises(DmError):
            self.run_t(t, self.opts(net, stall_s=2), logs.append)
        lines = [m for m in logs if m.startswith("tracker:")]
        self.assertTrue(lines, logs)
        self.assertFalse([m for m in logs if ADDRESS.search(m)], logs)

    def test_with_the_dht_off_a_dead_tracker_still_fails_at_once(self):  # as in dm 0.5.0
        t = self.dead_tracker_torrent()
        with self.assertRaises(DmError) as cm:
            self.run_t(t, session.Options(dht=False, stall_s=30))
        self.assertTrue(str(cm.exception).startswith("tracker error"))


class Switches(DhtHarness):
    """AC4, AC5: no DHT traffic when it is off or the torrent is private."""

    def tracker_download(self, **kw):
        tr = self.serve_tracker()
        t = torrentkit.make_torrent([("a.bin", torrentkit.content(8 * PL))], PL, name="a.bin",
                                    announce=f"http://127.0.0.1:{tr.http_port}/announce", **kw)
        seeder = self.seeder(t)
        self.swarm(tr, seeder)
        return t, seeder

    def test_with_the_dht_off_no_datagram_is_sent_and_the_cache_is_neither_read_nor_written(self):  # AC5
        t, seeder = self.tracker_download()
        net = self.network(n=20, info_hash=t.info_hash, peers=[("127.0.0.1", seeder.port)])
        cache_path = self.dir / "dht-nodes.dat"
        self.run_torrent(t, self.opts(net, dht=False, dht_cache=cache_path))
        self.assertEqual(net.datagrams, 0)
        self.assertFalse(cache_path.exists())
        cache_path.write_bytes(cache.MAGIC + bytes(26))  # a cache that must stay as it is
        before = (cache_path.read_bytes(), cache_path.stat().st_mtime_ns)
        self.run_torrent(t, self.opts(net, dht=False, dht_cache=cache_path))
        self.assertEqual((cache_path.read_bytes(), cache_path.stat().st_mtime_ns), before)
        self.assertEqual(net.datagrams, 0)

    def test_a_private_torrent_sends_no_datagram(self):  # AC4
        t, seeder = self.tracker_download(extra_info={"private": 1})
        net = self.network(n=20, info_hash=t.info_hash, peers=[("127.0.0.1", seeder.port)])
        self.run_torrent(t, self.opts(net))
        self.assertEqual(net.datagrams, 0)
        self.assertEqual((self.cwd / "a.bin").read_bytes(), t.blob)

    def magnet_run(self, private):
        """A trackerless magnet found through the DHT; the number of datagrams the network had received when
        the metadata turned out to be (or not to be) private, and at the end."""
        t = torrentkit.make_torrent([("a.bin", torrentkit.content(40 * PL))], PL, name="a.bin",
                                    extra_info={"private": 1} if private else None)
        _, net = self.known_by_dht(t, delay=0.02)
        mark = []
        real = session.Session._setup

        async def setup(s, info_bytes):
            await real(s, info_bytes)
            mark.append(net.datagrams)

        with mock.patch.object(session, "DHT_TICK", 0.02), mock.patch.object(session, "DHT_RETRY", 0.05), \
                mock.patch.object(session.Session, "_setup", setup):
            self.run_magnet(torrentkit.magnet_for(t), self.opts(net, stall_s=30))
        self.assertEqual((self.cwd / "a.bin").read_bytes(), t.blob)
        return mark[0], net.datagrams

    def test_a_magnet_whose_metadata_is_private_stops_the_dht_when_it_is_known(self):  # AC4
        at_setup, at_end = self.magnet_run(private=True)
        self.assertEqual(at_end, at_setup)

    def test_control_a_public_magnet_keeps_looking_while_it_downloads(self):
        at_setup, at_end = self.magnet_run(private=False)
        self.assertGreater(at_end, at_setup)


class PrivateDrop(unittest.IsolatedAsyncioTestCase):
    """R3: peers only the DHT named are dropped when the torrent turns out private."""

    def session(self, **kw):
        s = session.Session(None, None, lambda *a: None, TorrentHandle(), session.Options(**kw),
                            info_hash=b"\x11" * 20)
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

    async def test_finding_peers_is_shown_only_while_no_tracker_has_answered_and_nobody_is_connected(self):
        s = self.session()
        self.assertTrue(s._finding_peers())
        s.tracker_url = "http://t/announce"
        self.assertFalse(s._finding_peers())
        s.tracker_url = None
        s.conns = {1: types.SimpleNamespace(handshaken=True)}
        self.assertFalse(s._finding_peers())
        self.assertFalse(self.session(dht=False)._finding_peers())

    async def test_a_peer_the_dht_named_then_dialed_then_the_tracker_named_again_survives_go_private(self):
        s = self.session()
        a = ("1.1.1.1", 1)
        s.add_peers([a], from_dht=True)
        with mock.patch.object(session, "PeerConn") as conn:  # the real connector dials it
            conn.return_value.run = mock.AsyncMock()
            task = asyncio.ensure_future(s.connector())
            await asyncio.sleep(0.05)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertNotIn(a, s.from_dht)
        s.conns.clear()
        s.add_peers([a])  # the tracker names it
        s._go_private()
        self.assertEqual(list(s.pending), [a])
        self.assertIn(a, s.queued)

    async def test_dht_replies_after_go_private_add_nothing_and_the_socket_is_closed(self):
        FakeClient.made = []
        s = self.session()
        with mock.patch.object(session, "DhtClient", FakeClient), mock.patch.object(session, "DHT_TICK", 0.001):
            s.dht_task = asyncio.ensure_future(s.dht_loop())
            await asyncio.sleep(0.02)
            client = FakeClient.made[0]
            self.assertIsNotNone(client.on_peers)
            s._go_private()
            self.assertIsNone(client.on_peers)  # synchronously: a late reply has nobody to go to
            s._dht_peers([("9.9.9.9", 9)])  # and a late call is ignored
            await asyncio.sleep(0.02)
        self.assertEqual((list(s.pending), s.from_dht, client.closed), ([], set(), 1))

    async def test_the_dht_never_fills_more_than_its_share_of_the_pending_peers(self):
        s = self.session()
        s.add_peers([("10.0.%d.%d" % divmod(i, 250), 1) for i in range(session.MAX_PENDING)], from_dht=True)
        self.assertEqual(len(s.pending), session.MAX_DHT_PENDING)
        s.add_peers([("20.0.0.1", 1)])
        self.assertIn(("20.0.0.1", 1), s.queued)

    async def test_a_fully_resumed_torrent_does_not_say_finding_peers(self):
        meta = metainfo.parse(torrentkit.make_torrent([("a.bin", torrentkit.content(40000))], 16384,
                                                      name="a.bin").data)
        s = session.Session(meta, None, lambda *a: None, TorrentHandle(), session.Options(),
                            have=range(len(meta.hashes)))
        self.addCleanup(s.pool.shutdown, wait=False)
        self.addCleanup(s.reader_pool.shutdown, wait=False)
        self.assertFalse(s._finding_peers())

    async def test_a_private_torrent_never_starts_the_dht(self):
        meta = metainfo.parse(torrentkit.make_torrent(
            [("a.bin", torrentkit.content(40000))], 16384, name="a.bin", extra_info={"private": 1}).data)
        s = session.Session(meta, None, lambda *a: None, TorrentHandle(), session.Options())
        self.addCleanup(s.pool.shutdown, wait=False)
        self.addCleanup(s.reader_pool.shutdown, wait=False)
        self.assertFalse(s.dht_on)
        self.assertFalse(self.session(dht=False).dht_on)
        self.assertTrue(self.session().dht_on)


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


class Schedule(unittest.IsolatedAsyncioTestCase):
    """AC6, R5: when the next lookup is due, with a fake clock; and the exit rules."""

    async def asyncSetUp(self):
        FakeClient.made = []
        self.clock = Clock()
        for patch in (mock.patch.object(session, "DhtClient", FakeClient),
                      mock.patch.object(session, "DHT_TICK", 0.001)):
            patch.start()
            self.addCleanup(patch.stop)
        self.s = session.Session(None, None, lambda *a: None, TorrentHandle(), session.Options(),
                                 info_hash=b"\x22" * 20, clock=self.clock)
        self.addCleanup(self.s.pool.shutdown, wait=False)
        self.addCleanup(self.s.reader_pool.shutdown, wait=False)
        self.task = asyncio.ensure_future(self.s.dht_loop())
        self.addAsyncCleanup(self.stop)
        await self.settle()
        self.client = FakeClient.made[0]

    async def stop(self):
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)

    async def settle(self):
        await asyncio.sleep(0.03)

    async def advance(self, seconds):
        self.clock.now += seconds
        await self.settle()

    def connections(self, n):
        self.s.conns = {i: types.SimpleNamespace(handshaken=True) for i in range(n)}

    async def test_the_first_lookup_starts_at_once_with_the_options(self):
        self.assertEqual(self.client.lookups, 1)
        self.assertEqual(self.client.args[0], b"\x22" * 20)
        self.assertEqual(self.client.args[1], session.Options().dht_bootstrap)
        self.assertEqual((self.client.kw["allow_loopback"], self.client.kw["allow_private"]), (False, False))
        self.assertEqual(self.client.on_peers, self.s._dht_peers)

    async def test_with_few_connections_it_repeats_after_60_seconds_not_before(self):
        await self.advance(59)
        self.assertEqual(self.client.lookups, 1)
        await self.advance(2)
        self.assertEqual(self.client.lookups, 2)
        await self.advance(60)
        self.assertEqual(self.client.lookups, 3)

    async def test_with_five_connections_it_repeats_after_5_minutes(self):
        self.connections(5)
        await self.advance(61)
        self.assertEqual(self.client.lookups, 1)
        await self.advance(238)  # 299 s since the first
        self.assertEqual(self.client.lookups, 1)
        await self.advance(2)
        self.assertEqual(self.client.lookups, 2)

    async def test_four_connections_still_count_as_few(self):
        self.connections(4)
        await self.advance(61)
        self.assertEqual(self.client.lookups, 2)

    async def test_after_the_end_nothing_more_is_looked_up_and_the_socket_is_closed(self):
        await self.stop()
        self.assertEqual(self.client.closed, 1)
        await self.advance(10_000)
        self.assertEqual(self.client.lookups, 1)

    async def test_the_log_line_names_the_lookup_and_the_counts_only(self):
        lines = []
        self.s._log = lines.append
        await self.advance(61)
        self.assertEqual(lines, ["dht: lookup 2, 3 nodes, 0 peers"])


class Skips(unittest.IsolatedAsyncioTestCase):
    async def test_nothing_is_looked_up_when_every_piece_is_already_there(self):
        FakeClient.made = []
        meta = metainfo.parse(torrentkit.make_torrent([("a.bin", torrentkit.content(40000))], 16384,
                                                      name="a.bin").data)
        s = session.Session(meta, None, lambda *a: None, TorrentHandle(), session.Options(),
                            have=range(len(meta.hashes)))
        self.addCleanup(s.pool.shutdown, wait=False)
        self.addCleanup(s.reader_pool.shutdown, wait=False)
        self.assertTrue(s.picker.done())
        with mock.patch.object(session, "DhtClient", FakeClient), mock.patch.object(session, "DHT_TICK", 0.001):
            task = asyncio.ensure_future(s.dht_loop())
            await asyncio.sleep(0.05)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(FakeClient.made[0].lookups, 0)


class Closing(unittest.IsolatedAsyncioTestCase):
    """The way out: the cache is kept after a success or a find, and nothing here can fail or hang."""

    def session(self, client):
        s = session.Session(None, None, lambda *a: None, TorrentHandle(), session.Options(),
                            info_hash=b"\x33" * 20)
        self.addCleanup(s.pool.shutdown, wait=False)
        self.addCleanup(s.reader_pool.shutdown, wait=False)
        s.dht = client
        return s

    async def test_the_cache_is_saved_only_when_the_dht_found_peers(self):
        for completed, peers, saved in ((True, 0, 0), (True, 5, 1), (False, 5, 1), (False, 0, 0)):
            with self.subTest(completed=completed, peers=peers):
                client = FakeClient()
                client.stats["peers"] = peers
                await self.session(client)._close_dht()
                self.assertEqual((client.saved, client.closed), (saved, 1))

    async def test_a_socket_that_will_not_close_costs_at_most_the_cap(self):
        client = FakeClient()

        async def hang():
            await asyncio.Event().wait()

        client.close = hang
        with mock.patch.object(session, "DHT_CLOSE", 0.1):
            start = time.monotonic()
            await self.session(client)._close_dht()
        self.assertLess(time.monotonic() - start, 0.5)

    async def test_a_failing_save_does_not_stop_the_close(self):
        client = FakeClient()

        def boom():
            raise OSError("disk full")

        client.save_cache = boom
        await self.session(client)._close_dht()  # must not raise
        self.assertEqual(client.closed, 1)

    async def test_without_a_client_there_is_nothing_to_do(self):
        await self.session(None)._close_dht()


class Cache(DhtHarness):
    """AC9, R12, R13: the node cache in the data folder."""

    def setUp(self):
        super().setUp()
        self.data_dir = self.dir / "dm-data"
        self.data_dir.mkdir()
        self.path = self.data_dir / cache.CACHE_NAME

    def download(self, bootstrap=None, **kw):
        t = self.dead = getattr(self, "dead", None) or torrentkit.make_torrent(
            [("a.bin", torrentkit.content(16 * PL))], PL, announce="http://127.0.0.1:9/announce", name="a.bin")
        if not hasattr(self, "net"):
            self.seeder_, self.net = self.known_by_dht(t)
        extra = {} if bootstrap is None else {"dht_bootstrap": bootstrap}
        for item in self.cwd.iterdir():
            item.unlink()
        self.run_torrent(t, self.opts(self.net, dht_cache=self.path, **extra, **kw))
        self.assertEqual((self.cwd / "a.bin").read_bytes(), t.blob)

    def saved(self):
        return cache.load(self.path, allow_loopback=True)

    def test_a_successful_run_saves_at_most_200_valid_nodes_and_nothing_else_in_the_folder(self):
        self.download()
        nodes = self.saved()
        self.assertTrue(0 < len(nodes) <= cache.MAX_CACHE_NODES)
        self.assertTrue(all(n.addr in set(self.net.addrs) for n in nodes))
        self.assertEqual([p.name for p in self.data_dir.iterdir()], [cache.CACHE_NAME])

    def test_the_next_run_starts_from_the_cached_nodes_without_any_bootstrap_host(self):
        self.download()
        self.net.queries.clear()
        self.download(bootstrap=())  # only the cache can lead anywhere
        self.assertTrue(self.net.queries)

    def test_garbage_a_big_file_and_a_symlink_are_ignored_and_replaced_and_the_download_works(self):
        outside = self.dir / "outside"
        outside.write_bytes(b"precious")
        cases = {"garbage": lambda: self.path.write_bytes(os.urandom(500)),
                 "1 MiB": lambda: self.path.write_bytes(b"x" * (1 << 20)),
                 "symlink": lambda: self.path.symlink_to(outside)}
        for label, make in cases.items():
            with self.subTest(label):
                if self.path.is_symlink() or self.path.exists():
                    self.path.unlink()
                make()
                self.download()
                self.assertTrue(self.path.is_file() and not self.path.is_symlink())
                self.assertTrue(self.saved())
                self.assertEqual(outside.read_bytes(), b"precious")

    def test_unusable_entries_are_ignored(self):
        import socket
        import struct

        def entry(ip, port):
            return os.urandom(20) + socket.inet_aton(ip) + struct.pack(">H", port)

        self.path.write_bytes(cache.MAGIC + entry("0.0.0.0", 6881) + entry("1.2.3.4", 0)
                              + entry("255.255.255.255", 1) + entry("10.0.0.1", 6881))
        self.download()
        self.assertTrue(self.saved())

    def test_a_folder_that_cannot_be_written_does_not_fail_the_download(self):
        if os.geteuid() == 0:
            self.skipTest("root writes anywhere")
        self.data_dir.chmod(stat.S_IRUSR | stat.S_IXUSR)
        self.addCleanup(self.data_dir.chmod, stat.S_IRWXU)
        self.download()
        self.assertFalse(self.path.exists())

    def test_a_missing_folder_does_not_fail_the_download(self):
        self.data_dir.rmdir()
        self.download()

    def test_a_run_that_found_no_peer_through_the_dht_leaves_the_cache_alone(self):
        tr = self.serve_tracker()
        t = torrentkit.make_torrent([("a.bin", torrentkit.content(8 * PL))], PL, name="a.bin",
                                    announce=f"http://127.0.0.1:{tr.http_port}/announce")
        self.swarm(tr, self.seeder(t))
        net = self.network(n=20)  # knows nobody
        self.path.write_bytes(cache.MAGIC + bytes(26))
        before = self.path.read_bytes()
        self.run_torrent(t, self.opts(net, dht_cache=self.path))
        self.assertEqual(self.path.read_bytes(), before)

    def test_a_private_magnet_leaves_no_cache_behind(self):
        t = torrentkit.make_torrent([("a.bin", torrentkit.content(20 * PL))], PL, name="a.bin",
                                    extra_info={"private": 1})
        _, net = self.known_by_dht(t, delay=0.01)
        self.run_magnet(torrentkit.magnet_for(t), self.opts(net, dht_cache=self.path))
        self.assertFalse(self.path.exists())


class Hostile(DhtHarness):
    """AC8 at the download level: bad nodes cost nothing, the honest ones still deliver."""

    def test_the_download_completes_with_nodes_that_each_misbehave(self):
        t = torrentkit.make_torrent([("a.bin", torrentkit.content(16 * PL))], PL,
                                    announce="http://127.0.0.1:9/announce", name="a.bin")
        seeder = self.seeder(t)
        net = self.network(n=60, info_hash=t.info_hash, peers=[("127.0.0.1", seeder.port)])
        for mode in ("oversize", "garbage", "deep", "flood", "self", "bad-addrs", "many-values", "dup-replies",
                     "spoof", "wrong-tid", "private", "silent"):
            net.hostile(mode, 2)
        self.run_torrent(t, self.opts(net))
        self.assertEqual((self.cwd / "a.bin").read_bytes(), t.blob)
        self.assertEqual(net.announces, [])
        self.assertEqual(net.dm_replies, [])
        self.assertTrue(all(q["ro"] == 1 and q["method"] == "get_peers" for q in net.queries))


class Failing(DhtHarness):
    """AC12, NFR4: a broken DHT socket costs nothing, and a cancel is quick during a lookup."""

    def test_every_send_failing_leaves_a_tracker_download_unharmed(self):
        tr = self.serve_tracker()
        t = self.single(size=16 * PL, piece_length=PL, tr=tr)
        self.swarm(tr, self.seeder(t))
        net = self.network(n=20)

        def broken(client, data, addr):
            raise OSError("network unreachable")

        with mock.patch.object(session.DhtClient, "_sendto", broken):
            self.run_torrent(t, self.opts(net))
        self.assertEqual((self.cwd / "a.bin").read_bytes(), t.blob)
        self.assertEqual(net.datagrams, 0)

    def test_cancel_during_a_lookup_ends_within_two_seconds(self):
        t = self.single(size=4 * PL, piece_length=PL)
        net = self.network(n=40, modes={i: "silent" for i in range(40)})  # every query waits out its timeout
        logs = []
        handle, result = TorrentHandle(), {}

        def work():
            try:
                result["ok"] = session.run_magnet(magnet.parse(torrentkit.magnet_for(t)),
                                                  Target(self.cwd, None), lambda p, x: None, handle,
                                                  self.opts(net, stall_s=60), logs.append)
            except BaseException as e:
                result["err"] = e

        thread = threading.Thread(target=work)
        thread.start()
        self.addCleanup(thread.join, 10)
        self.assertTrue(self.wait_for(lambda: net.datagrams > 5, 10))
        start = time.monotonic()
        self.assertTrue(handle.abort())
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.__class__.last_cancel_seconds = time.monotonic() - start
        self.assertLess(self.__class__.last_cancel_seconds, 2)
        self.assertIsInstance(result.get("err"), Cancelled)
        sent = net.datagrams
        time.sleep(0.3)
        self.assertEqual(net.datagrams, sent)  # the socket is gone: nothing more is sent


class Ending(DhtHarness):
    """AC6: after success, failure or cancel, the network counts no more datagrams."""

    def settled(self, net):
        sent = net.datagrams
        time.sleep(0.4)
        self.assertEqual(net.datagrams, sent)

    def test_nothing_is_sent_after_a_success(self):
        t = torrentkit.make_torrent([("a.bin", torrentkit.content(16 * PL))], PL,
                                    announce="http://127.0.0.1:9/announce", name="a.bin")
        _, net = self.known_by_dht(t)
        with mock.patch.object(session, "DHT_TICK", 0.02), mock.patch.object(session, "DHT_RETRY", 0.05):
            self.run_torrent(t, self.opts(net))
        self.settled(net)

    def test_nothing_is_sent_after_a_failure(self):
        t = self.single(size=4 * PL, piece_length=PL)
        net = self.network(n=30)
        with mock.patch.object(session, "DHT_TICK", 0.02), mock.patch.object(session, "DHT_RETRY", 0.05):
            with self.assertRaises(DmError):
                self.run_magnet(torrentkit.magnet_for(t), self.opts(net, stall_s=1))
        self.settled(net)


class Idle(DhtHarness):
    """AC14, NFR3: the DHT adds at most 5% of one core to a download that is waiting."""

    def test_over_30_seconds_with_the_dht_running_and_no_traffic_it_uses_at_most_5_percent_of_a_core(self):
        t = self.single(size=4 * PL, piece_length=PL)
        net = self.network(n=30)  # no peer anywhere: the run waits
        logs, result = [], {}

        def work():
            try:
                session.run_magnet(magnet.parse(torrentkit.magnet_for(t)), Target(self.cwd, None),
                                   lambda p, x: None, handle, self.opts(net, stall_s=300), logs.append)
            except BaseException as e:
                result["err"] = e

        handle = TorrentHandle()
        thread = threading.Thread(target=work)
        thread.start()
        self.addCleanup(thread.join, 10)
        self.assertTrue(self.wait_for(lambda: any(m.startswith("dht: lookup 1,") for m in logs), 20))
        time.sleep(0.5)
        cpu, wall = time.process_time(), time.monotonic()
        time.sleep(30)
        used = (time.process_time() - cpu) / (time.monotonic() - wall)
        handle.abort()
        thread.join(5)
        self.__class__.idle_cpu = used
        self.assertLessEqual(used, 0.05, f"{used:.1%} of a core")
        self.assertEqual(len([m for m in logs if m.startswith("dht: lookup")]), 1)  # none in the window


class PrivateFlag(unittest.TestCase):
    """R3: info.private is read from the info dictionary, 1 and nothing else."""

    def meta(self, **extra):
        t = torrentkit.make_torrent([("a.bin", torrentkit.content(40000))], 16384, name="a.bin",
                                    extra_info=extra)
        return metainfo.parse(t.data), t

    def test_private_equal_to_1_is_private(self):
        meta, t = self.meta(private=1)
        self.assertTrue(meta.private)
        self.assertTrue(metainfo.from_info(bencode_info(t)).private)

    def test_absent_or_anything_else_is_not_private(self):
        for extra in ({}, {"private": 0}, {"private": 2}, {"private": -1}, {"private": "1"}, {"private": [1]}):
            with self.subTest(extra=extra):
                self.assertFalse(self.meta(**extra)[0].private)


def bencode_info(t):
    from dmcore.torrent import bencode

    top, span = bencode.decode(t.data)
    return t.data[span[0]:span[1]]


class MainData(unittest.TestCase):
    """The plugin keeps data_dir from `initialize` and passes it to dmcore.run."""

    def run_main(self, messages):
        out = tempfile.mkdtemp(prefix="dm-main-")
        self.addCleanup(__import__("shutil").rmtree, out, ignore_errors=True)
        seen = Path(out) / "seen.json"
        code = (
            "import json, runpy, sys\n"
            f"sys.path.insert(0, {str(ROOT / 'plugin' / 'dm')!r})\n"
            "import dmcore\n"
            "def fake(inputs, cwd, notify, cancelled, **kw):\n"
            f"    open({str(seen)!r}, 'w').write(json.dumps(kw, default=str))\n"
            "    return 0\n"
            "dmcore.run = fake\n"
            f"runpy.run_path({str(MAIN)!r}, run_name='__main__')\n")
        stdin = "".join(json.dumps({"jsonrpc": "2.0", **m}) + "\n" for m in messages)
        r = subprocess.run([sys.executable, "-c", code], input=stdin, capture_output=True, text=True,
                           timeout=60, check=False)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(seen.read_text())

    def invoke(self, init_params):
        return self.run_main([
            {"id": 1, "method": "initialize", "params": init_params},
            {"id": 2, "method": "invoke", "params": {"command": "dm", "inputs": {"url": "x"}, "cwd": "/"}},
            {"id": 3, "method": "shutdown"}])

    def test_data_dir_reaches_run(self):
        self.assertEqual(self.invoke({"data_dir": "/the/data"}), {"data_dir": "/the/data"})

    def test_a_missing_or_odd_data_dir_is_none(self):
        self.assertEqual(self.invoke({}), {"data_dir": None})
        self.assertEqual(self.invoke({"data_dir": 5}), {"data_dir": None})


if __name__ == "__main__":
    unittest.main()
