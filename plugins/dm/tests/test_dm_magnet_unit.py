"""Unit tests for magnet links in dm: parsing (AC1-AC3), the metadata assembler (AC5), metainfo helpers
(AC6, AC11)."""

import asyncio
import base64
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import torrentkit
from dmcore.torrent import bencode, magnet, metadata, metainfo, wire
from dmcore.torrent.peer import PeerConn

HEX = "c9e15763f722f23e98a29decdfae341b98d53056"
IH = bytes.fromhex(HEX)
B32 = base64.b32encode(IH).decode()


def link(*params):
    return "magnet:?" + "&".join(params)


class Parse(unittest.TestCase):
    # AC1

    def test_bad_peers_are_ignored(self):
        bad = [
            "x.pe=1.2.3.4",
            "x.pe=1.2.3.4:0",
            "x.pe=1.2.3.4:65536",
            "x.pe=1.2.3.4:abc",
            "x.pe=:80",
            "x.pe=[::1:80",
            "x.pe=[zz]:80",
            "x.pe=bad_host!:80",
            "x.pe=a%00b:80",
            "x.pe=",
            "x.pe=%5Bnot-ip%5D:80",
            "x.pe=1.2.3.4:-5",
        ]
        m = magnet.parse(link(f"xt=urn:btih:{HEX}", *bad, "x.pe=9.9.9.9:9"))
        self.assertEqual(m.peers, (("9.9.9.9", 9),))

    def test_the_same_hash_twice_is_fine_and_a_hybrid_uses_the_v1_hash(self):
        same = link(f"xt=urn:btih:{HEX}", f"xt=urn:btih:{B32}", "tr=udp://a:1")
        self.assertEqual(magnet.parse(same).info_hash, IH)
        hybrid = link(f"xt=urn:btih:{HEX}", "xt=urn:btmh:1220" + "ab" * 32, "tr=udp://a:1")
        self.assertEqual(magnet.parse(hybrid).info_hash, IH)


class ParseErrors(unittest.TestCase):
    def fails(self, arg, message):
        with self.assertRaises(metainfo.TorrentError) as cm:
            magnet.parse(arg)
        self.assertEqual(str(cm.exception), message)
        return cm.exception

    # AC2
    def test_invalid_links(self):
        cases = [
            "magnet:?",
            "magnet:?dn=x&tr=udp://a:1",
            "magnet:?xt=urn:btih:",
            f"magnet:?xt=urn:btih:{HEX[:-1]}&tr=udp://a:1",
            f"magnet:?xt=urn:btih:{HEX}0&tr=udp://a:1",
            f"magnet:?xt=urn:btih:{'z' * 40}&tr=udp://a:1",
            f"magnet:?xt=urn:sha1:{HEX}&tr=udp://a:1",
            f"magnet:?xt={HEX}&tr=udp://a:1",
            f"magnet:?xt=urn:btih:{HEX}&xt=urn:btih:{'1' * 40}&tr=udp://a:1",
            f"magnet:?xt=urn:btih:{HEX}&xt=urn:btih:garbage&tr=udp://a:1",
            "magnet:?" + "x" * 70_000,
        ]
        for arg in cases:
            with self.subTest(arg=arg[:60]):
                self.fails(arg, "invalid magnet link")


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def info_for(size_hint):
    """(info dict bytes, info-hash) of a torrent whose info is at least size_hint bytes."""
    n = max(1, size_hint // 38)  # about 38 bytes of info per file
    files = [(f"d/f{i:06d}.bin", b"x") for i in range(n)]
    t = torrentkit.make_torrent(files, name="m", multi=True)
    return bencode.encode(bencode.decode(t.data)[0][b"info"]), t.info_hash


def blocks(blob):
    return [blob[i : i + metadata.BLOCK] for i in range(0, len(blob), metadata.BLOCK)]


class Messages(unittest.TestCase):
    def test_parse_ext_handshake_rejects_garbage(self):
        for body in (
            b"",
            b"xx",
            b"i1e",
            b"le",
            bencode.encode({"m": 3}),
            bencode.encode({"m": {"ut_metadata": b"a"}}),
            bencode.encode({"m": {"ut_metadata": 300}}),
            bencode.encode({"metadata_size": b"1"}),
        ):
            with self.subTest(body=body[:12]):
                with self.assertRaises(ValueError):
                    metadata.parse_ext_handshake(body)

    def test_parse_metadata_msg_rejects_garbage(self):
        for body in (
            b"",
            b"zz",
            b"i1e",
            bencode.encode({"piece": 1}),
            bencode.encode({"msg_type": 1}),
            bencode.encode({"msg_type": 1, "piece": -1}),
            bencode.encode({"msg_type": 1, "piece": 0, "total_size": b"x"}),
        ):
            with self.subTest(body=body[:12]):
                with self.assertRaises(ValueError):
                    metadata.parse_metadata_msg(body)


class Assembler(unittest.TestCase):
    """AC5 unit part: what the assembler accepts, drops and bans."""

    def setUp(self):
        self.info, self.ih = info_for(40_000)  # three blocks, the last one short
        self.parts = blocks(self.info)
        self.assertEqual(len(self.parts), 3)
        self.clock = Clock()
        self.a = metadata.MetadataAssembler(self.ih, self.clock)

    def feed(self, peer, piece, data=None, total=None):
        return self.a.received(peer, piece, self.parts[piece] if data is None else data, total)

    def fetch_all(self, peer):
        got = None
        while (piece := self.a.next_request(peer, self.clock())) is not None:
            got = self.feed(peer, piece) or got
        return got

    def fill_with_block_holders(self):
        for i in range(3):
            size = 100_000 + i * 20_000
            self.a.offer(f"p{i}", size)
            piece = self.a.next_request(f"p{i}", self.clock())
            self.a.received(f"p{i}", piece, b"z" * metadata.BLOCK)

    def test_slots_holding_stale_blocks_are_freed_while_their_peers_stay(self):
        self.fill_with_block_holders()
        self.assertTrue(self.a.offer("late", len(self.info)))
        self.assertIsNone(self.a.next_request("late", self.clock()))
        self.clock.now += metadata.STALE
        self.assertEqual(self.fetch_all("late"), self.info)
        self.assertEqual(self.a._stored, 0)
        self.assertLessEqual(len(self.a._attempts), metadata.MAX_SIZES)

    def corrupt_set(self):
        return [bytes([self.parts[0][0] ^ 1]) + self.parts[0][1:], *self.parts[1:]]

    def mismatch_from_two_peers(self):
        """evil sends blocks 0 and 2 (0 is wrong), good sends 1: the set fails the hash."""
        corrupt = self.corrupt_set()
        self.a.offer("evil", len(self.info))
        self.a.offer("good", len(self.info))
        e0 = self.a.next_request("evil", self.clock())
        g1 = self.a.next_request("good", self.clock())
        self.a.received("evil", e0, corrupt[e0])
        self.a.received("good", g1, corrupt[g1])
        e2 = self.a.next_request("evil", self.clock())
        self.assertIsNone(self.a.received("evil", e2, corrupt[e2]))

    def test_a_block_that_would_pass_the_memory_cap_is_dropped(self):
        a = metadata.MetadataAssembler(self.ih, self.clock)
        a.offer("s", len(self.info))
        self.assertEqual(a.next_request("s", self.clock()), 0)
        self.clock.now += metadata.REQUEST_TIMEOUT + 1
        self.assertEqual(a.next_request("s", self.clock()), 1)  # 0 is owed late, 1 is reserved
        a.offer("h", metadata.MAX_METADATA)
        while (piece := a.next_request("h", self.clock())) is not None:
            a.received("h", piece, b"z" * metadata.BLOCK)  # until the budget is full
        self.assertIsNone(a.received("s", 0, self.parts[0]))
        self.assertEqual(set(a._attempts[len(self.info)].blocks), {0})
        self.assertIsNone(a.received("s", 1, self.parts[1]))  # no room left: dropped, not stored
        self.assertEqual(set(a._attempts[len(self.info)].blocks), {0})
        self.assertLessEqual(a._stored, metadata.MAX_METADATA)

    def test_a_rejecting_peer_is_not_asked_again(self):
        self.a.offer("a", len(self.info))
        piece = self.a.next_request("a", self.clock())
        self.a.rejected("a", piece)
        self.assertIsNone(self.a.next_request("a", self.clock()))

    def test_an_attempt_that_stopped_receiving_is_evicted_for_another(self):
        a = metadata.MetadataAssembler(self.ih, self.clock)
        big = metadata.MAX_METADATA
        a.offer("hostile", big)
        a_blocks = -(-big // metadata.BLOCK) - 1
        for _ in range(a_blocks):  # everything but the last block, then silence
            piece = a.next_request("hostile", self.clock())
            a.received("hostile", piece, b"z" * metadata.BLOCK)
        a.offer("honest", len(self.info))
        first = a.next_request("honest", self.clock())
        a.received("honest", first, self.parts[first])
        # the budget is held by a fresh attempt: wait
        self.assertIsNone(a.next_request("honest", self.clock()))
        self.clock.now += metadata.STALE + 1
        self.assertIsNotNone(a.next_request("honest", self.clock()))  # it went stale and was evicted
        self.assertEqual(a_blocks, 639)


class PeerBitfield(unittest.TestCase):
    """N5: a bitfield arriving around the switch to the download is checked against the real count."""

    def conn(self, meta=True):
        picker = mock.Mock()
        picker.verified_bytes.return_value = 0
        hashes = [b"\x00" * 20] * 20  # three bytes of bitfield
        sess = SimpleNamespace(
            meta=SimpleNamespace(hashes=hashes) if meta else None,
            picker=picker,
            banned=set(),
            log=lambda m: None,
            interest_changed=lambda c: None,
            clock=time.monotonic,
        )
        peer = PeerConn(sess, ("127.0.0.1", 1))
        peer.handshaken = True
        return peer, sess

    def test_a_bitfield_buffered_before_the_switch_is_checked_when_the_download_starts(self):
        for payload in (b"\xff\xff\xf0\x00", b"\xff\xff"):
            with self.subTest(n=len(payload)):

                async def go():
                    peer, sess = self.conn(meta=False)
                    peer.on_message(wire.BITFIELD, payload, 0)
                    sess.meta = SimpleNamespace(hashes=[b"\x00" * 20] * 20)
                    peer.enter_download()
                    self.assertIn(peer.addr, sess.banned)
                    self.assertTrue(peer.closed)
                    sess.picker.peer_bitfield.assert_not_called()

                asyncio.run(go())


if __name__ == "__main__":
    unittest.main()
