"""Receive-only PEX (BEP 11) inside a download (phase 3): AC10 and the PEX part of AC4 and R3.

One process, the fixture tracker and seeders of test_dm_torrent_local on 127.0.0.1. No Docker. The seeder
fixture sends the ut_pex messages and records whether dm ever sent one.
"""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "plugin" / "dm"))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tests" / "fixtures" / "torrent"))

import torrentkit  # noqa: E402
from dmcore.target import Target  # noqa: E402
from dmcore.torrent import bencode, magnet, metadata, session, wire  # noqa: E402
from dmcore.torrent.handle import TorrentHandle  # noqa: E402
from dmcore.torrent.peer import PeerConn  # noqa: E402
from test_dm_magnet_local import MagnetHarness  # noqa: E402
from test_dm_torrent_local import PL  # noqa: E402

OPTS = session.Options(dht=False, stall_s=20, connect_timeout=2, idle_timeout=10, dht_allow_loopback=True)


class PexHarness(MagnetHarness):
    def pex_adds(self):
        """Patch Session.add_peers; the list collects the sizes of the PEX-sourced calls."""
        calls = []
        real = session.Session.add_peers

        def watch(s, peers, from_dht=False, from_pex=False):
            if from_pex:
                calls.append(list(peers))
            return real(s, peers, from_dht, from_pex)

        patch = mock.patch.object(session.Session, "add_peers", watch)
        patch.start()
        self.addCleanup(patch.stop)
        return calls

    def tracker_torrent(self, **kw):
        tr = self.serve_tracker()
        t = torrentkit.make_torrent([("a.bin", torrentkit.content(8 * PL))], PL, name="a.bin",
                                    announce=f"http://127.0.0.1:{tr.http_port}/announce", **kw)
        return tr, t


class Receiving(PexHarness):
    """AC10."""

    def test_three_added_addresses_make_dm_connect_to_them(self):
        tr, t = self.tracker_torrent()
        others = [self.seeder(t) for _ in range(3)]
        hub = self.seeder(t, never_unchoke=True, pex_peers=[("127.0.0.1", o.port) for o in others])
        self.swarm(tr, hub)
        self.run_torrent(t, OPTS)
        self.assertEqual((self.cwd / "a.bin").read_bytes(), t.blob)
        self.assertTrue(self.wait_for(lambda: all(o.connections >= 1 for o in others)),
                        [o.connections for o in others])

    def test_dm_advertises_ut_pex_and_never_sends_it(self):
        tr, t = self.tracker_torrent()
        other = self.seeder(t)
        hub = self.seeder(t, pex_peers=[("127.0.0.1", other.port)])
        self.swarm(tr, hub)
        self.run_torrent(t, OPTS)
        self.assertTrue(hub.dm_ids)
        self.assertEqual(hub.dm_ids[0][b"ut_pex"], metadata.OUR_UT_PEX)
        self.assertEqual(hub.pex_received, 0)
        self.assertEqual(other.pex_received, 0)

    def test_1000_added_addresses_add_at_most_50(self):
        tr, t = self.tracker_torrent()
        hub = self.seeder(t, pex_peers=[("127.0.0.1", 1)], pex_mode="flood")
        self.swarm(tr, hub)
        calls = self.pex_adds()
        self.run_torrent(t, OPTS)
        self.assertEqual([len(c) for c in calls], [50])

    def test_a_second_message_within_60_seconds_is_ignored(self):
        tr, t = self.tracker_torrent()
        hub = self.seeder(t, pex_peers=[("127.0.0.1", 1)], pex_mode="twice")
        self.swarm(tr, hub)
        calls = self.pex_adds()
        self.run_torrent(t, OPTS)
        self.assertEqual(len(calls), 1)

    def test_a_garbage_message_adds_nothing_and_keeps_the_peer(self):
        tr, t = self.tracker_torrent()
        hub = self.seeder(t, pex_peers=[("127.0.0.1", 1)], pex_mode="garbage")
        self.swarm(tr, hub)
        calls = self.pex_adds()
        self.run_torrent(t, OPTS)
        self.assertEqual((self.cwd / "a.bin").read_bytes(), t.blob)
        self.assertEqual((calls, hub.connections), ([], 1))
        self.assertEqual(hub.pex_sent, 1)  # it was sent, and rejected

    def test_a_message_before_the_extension_handshake_is_ignored(self):
        tr, t = self.tracker_torrent()
        hub = self.seeder(t, pex_peers=[("127.0.0.1", 1)], pex_mode="early")
        self.swarm(tr, hub)
        calls = self.pex_adds()
        self.run_torrent(t, OPTS)
        self.assertEqual(hub.pex_sent, 2)  # the early one and the valid one after the handshake
        self.assertEqual(calls, [[("127.0.0.1", 1)]])  # only the later one counted

    def test_a_peer_that_did_not_list_ut_pex_is_ignored(self):
        tr, t = self.tracker_torrent()
        hub = self.seeder(t, pex_peers=[("127.0.0.1", 1)], pex_advertise=False)
        self.swarm(tr, hub)
        calls = self.pex_adds()
        self.run_torrent(t, OPTS)
        self.assertEqual((hub.pex_sent, calls), (1, []))

    def test_a_peer_whose_ut_pex_id_is_ours_still_works_and_gets_nothing_back(self):
        tr, t = self.tracker_torrent()
        hub = self.seeder(t, pex_peers=[("127.0.0.1", 1)], pex_id=metadata.OUR_UT_PEX)
        self.swarm(tr, hub)
        calls = self.pex_adds()
        self.run_torrent(t, OPTS)
        self.assertEqual(hub.dm_ids[0][b"ut_pex"], metadata.OUR_UT_PEX)
        self.assertEqual((len(calls), hub.pex_received), (1, 0))

    def test_added6_and_dropped_are_ignored(self):
        tr, t = self.tracker_torrent()
        hub = self.seeder(t, pex_peers=[("127.0.0.1", 1)], pex_mode="extra")
        self.swarm(tr, hub)
        calls = self.pex_adds()
        self.run_torrent(t, OPTS)
        self.assertEqual(calls, [[("127.0.0.1", 1)]])

    def test_pex_works_with_the_dht_off_and_is_off_when_the_option_says_so(self):  # R4: independent of --no-dht
        for pex, expect in ((True, 1), (False, 0)):
            with self.subTest(pex=pex):
                tr, t = self.tracker_torrent()
                hub = self.seeder(t, pex_peers=[("127.0.0.1", 1)])
                self.swarm(tr, hub)
                calls = self.pex_adds()
                self.run_torrent(t, session.Options(dht=False, pex=pex, stall_s=20, connect_timeout=2,
                                                    idle_timeout=10, dht_allow_loopback=True))
                self.assertEqual(len(calls), expect)
                self.assertEqual(b"ut_pex" in hub.dm_ids[0], pex)


class Private(PexHarness):
    """AC4, R3: a private torrent neither advertises ut_pex nor learns from it."""

    def test_a_private_torrent_does_not_advertise_ut_pex_and_adds_nothing(self):
        tr, t = self.tracker_torrent(extra_info={"private": 1})
        other = self.seeder(t)
        hub = self.seeder(t, pex_peers=[("127.0.0.1", other.port)])
        self.swarm(tr, hub)
        calls = self.pex_adds()
        self.run_torrent(t, OPTS)
        self.assertEqual((self.cwd / "a.bin").read_bytes(), t.blob)
        self.assertNotIn(b"ut_pex", hub.dm_ids[0])
        self.assertEqual((calls, other.connections), ([], 0))

    def test_a_magnet_drops_ut_pex_from_its_handshake_once_the_metadata_says_private(self):
        t = torrentkit.make_torrent([("a.bin", torrentkit.content(8 * PL))], PL, name="a.bin",
                                    extra_info={"private": 1})
        hub = self.seeder(t, pex_peers=[])
        self.progress, self.notes = [], []
        link = torrentkit.magnet_for(t, [], [("127.0.0.1", hub.port)])
        session.run_magnet(magnet.parse(link), Target(self.cwd, None), lambda p, x: None, TorrentHandle(), OPTS,
                           None, note=self.notes.append)
        self.assertIn(b"ut_pex", hub.dm_ids[0])
        self.assertNotIn(b"ut_pex", hub.dm_ids[-1])
        self.assertGreater(len(hub.dm_ids), 1)


class Gate(unittest.IsolatedAsyncioTestCase):
    """The per-peer rate limit and the private gate, with a clock the test owns."""

    def make(self, **kw):
        s = session.Session(None, None, lambda *a: None, TorrentHandle(), session.Options(**kw),
                            info_hash=b"\x22" * 20)
        self.addCleanup(s.pool.shutdown, wait=False)
        self.addCleanup(s.reader_pool.shutdown, wait=False)
        conn = PeerConn(s, ("127.0.0.1", 1), 1)
        conn.on_extended(b"\x00" + bencode.encode({"m": {"ut_pex": 1}}), 0)
        return s, conn

    @staticmethod
    def msg(*ports):
        added = b"".join(bytes([8, 8, 8, 8]) + p.to_bytes(2, "big") for p in ports)
        return bytes([metadata.OUR_UT_PEX]) + bencode.encode({"added": added})

    async def test_one_message_per_peer_per_60_seconds(self):
        s, conn = self.make()
        conn.on_extended(self.msg(1), 100)
        conn.on_extended(self.msg(2), 159)
        self.assertEqual(list(s.pending), [("8.8.8.8", 1)])
        conn.on_extended(self.msg(3), 160)
        self.assertEqual(sorted(s.pending), [("8.8.8.8", 1), ("8.8.8.8", 3)])
        self.assertEqual(s.from_pex, set(s.pending))

    async def test_a_malformed_message_uses_up_the_slot(self):
        s, conn = self.make()
        conn.on_extended(bytes([metadata.OUR_UT_PEX]) + b"\xffnot bencode", 100)
        conn.on_extended(self.msg(1), 130)
        self.assertEqual(list(s.pending), [])
        conn.on_extended(self.msg(2), 160)
        self.assertEqual(list(s.pending), [("8.8.8.8", 2)])

    async def test_a_peer_that_did_not_list_ut_pex_adds_nothing(self):
        s, conn = self.make()
        conn.on_extended(b"\x00" + bencode.encode({"m": {"ut_metadata": 2}}), 0)
        conn.on_extended(self.msg(1), 100)
        self.assertEqual(list(s.pending), [])

    async def test_private_adds_nothing_even_for_an_already_connected_peer(self):
        s, conn = self.make()
        s._go_private()
        conn.on_extended(self.msg(1), 100)
        self.assertEqual(list(s.pending), [])

    async def test_go_private_drops_pending_pex_peers_but_keeps_tracker_named_ones(self):
        s, _ = self.make()
        s.add_peers([("1.1.1.1", 1), ("2.2.2.2", 2)], from_pex=True)
        s.add_peers([("2.2.2.2", 2)])
        s._go_private()
        self.assertEqual(list(s.pending), [("2.2.2.2", 2)])
        self.assertEqual((s.queued, s.from_pex), ({("2.2.2.2", 2)}, set()))

    async def test_a_dialed_pex_peer_is_no_longer_pex_only(self):
        s, _ = self.make()
        a = ("1.1.1.1", 1)
        s.add_peers([a], from_pex=True)
        with mock.patch.object(session, "PeerConn") as conn:
            conn.return_value.run = mock.AsyncMock()
            task = asyncio.ensure_future(s.connector())
            await asyncio.sleep(0.05)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertNotIn(a, s.from_pex)

    async def test_pex_never_fills_more_than_its_share_of_the_pending_peers(self):
        s, _ = self.make()
        s.add_peers([("10.0.%d.%d" % divmod(i, 250), 1) for i in range(session.MAX_PENDING)], from_pex=True)
        self.assertEqual(len(s.pending), session.MAX_PEX_PENDING)
        s.add_peers([("20.0.0.1", 1)])
        self.assertIn(("20.0.0.1", 1), s.queued)

    async def test_the_handshake_a_session_sends_has_no_ut_pex_when_pex_is_off(self):
        s, conn = self.make(pex=False)
        sent = []
        conn.writer = mock.Mock(write=sent.append)
        conn.send(wire.extended(0, metadata.ext_handshake(pex=s.opts.pex and not s.private)))
        self.assertNotIn(b"ut_pex", sent[0])


if __name__ == "__main__":
    unittest.main()
