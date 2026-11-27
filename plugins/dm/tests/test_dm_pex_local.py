"""Receive-only PEX (BEP 11) inside a download (phase 3): AC10 and the PEX part of AC4 and R3.

One process, the fixture tracker and seeders of test_dm_torrent_local on 127.0.0.1. No Docker. The seeder
fixture sends the ut_pex messages and records whether dm ever sent one.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tests" / "fixtures" / "torrent"))

import torrentkit
from dmcore.torrent import bencode, metadata, session
from dmcore.torrent.handle import TorrentHandle
from dmcore.torrent.peer import PeerConn
from test_dm_magnet_local import MagnetHarness
from test_dm_torrent_local import PL

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
        t = torrentkit.make_torrent(
            [("a.bin", torrentkit.content(8 * PL))],
            PL,
            name="a.bin",
            announce=f"http://127.0.0.1:{tr.http_port}/announce",
            **kw,
        )
        return tr, t


class Receiving(PexHarness):
    """AC10."""

    def test_pex_works_with_the_dht_off_and_is_off_when_the_option_says_so(
        self,
    ):  # R4: independent of --no-dht
        for pex, expect in ((True, 1), (False, 0)):
            with self.subTest(pex=pex):
                tr, t = self.tracker_torrent()
                hub = self.seeder(t, pex_peers=[("127.0.0.1", 1)])
                self.swarm(tr, hub)
                calls = self.pex_adds()
                self.run_torrent(
                    t,
                    session.Options(
                        dht=False,
                        pex=pex,
                        stall_s=20,
                        connect_timeout=2,
                        idle_timeout=10,
                        dht_allow_loopback=True,
                    ),
                )
                self.assertEqual(len(calls), expect)
                self.assertEqual(b"ut_pex" in hub.dm_ids[0], pex)


class Gate(unittest.IsolatedAsyncioTestCase):
    """The per-peer rate limit and the private gate, with a clock the test owns."""

    def make(self, **kw):
        s = session.Session(
            None, None, lambda *a: None, TorrentHandle(), session.Options(**kw), info_hash=b"\x22" * 20
        )
        self.addCleanup(s.pool.shutdown, wait=False)
        self.addCleanup(s.reader_pool.shutdown, wait=False)
        conn = PeerConn(s, ("127.0.0.1", 1), 1)
        conn.on_extended(b"\x00" + bencode.encode({"m": {"ut_pex": 1}}), 0)
        return s, conn

    @staticmethod
    def msg(*ports):
        added = b"".join(bytes([8, 8, 8, 8]) + p.to_bytes(2, "big") for p in ports)
        return bytes([metadata.OUR_UT_PEX]) + bencode.encode({"added": added})

    async def test_a_malformed_message_uses_up_the_slot(self):
        s, conn = self.make()
        conn.on_extended(bytes([metadata.OUR_UT_PEX]) + b"\xffnot bencode", 100)
        conn.on_extended(self.msg(1), 130)
        self.assertEqual(list(s.pending), [])
        conn.on_extended(self.msg(2), 160)
        self.assertEqual(list(s.pending), [("8.8.8.8", 2)])


if __name__ == "__main__":
    unittest.main()
