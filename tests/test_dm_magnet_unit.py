"""Unit tests for magnet links in dm: parsing (AC1-AC3), the metadata assembler (AC5), metainfo helpers
(AC6, AC11)."""

import asyncio
import base64
import hashlib
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "plugin" / "dm"))
sys.path.insert(0, str(ROOT / "tests"))

import torrentkit  # noqa: E402
from dmcore.torrent import bencode, magnet, metadata, metainfo, wire  # noqa: E402
from dmcore.torrent.peer import PeerConn  # noqa: E402

HEX = "c9e15763f722f23e98a29decdfae341b98d53056"
IH = bytes.fromhex(HEX)
B32 = base64.b32encode(IH).decode()


def link(*params):
    return "magnet:?" + "&".join(params)


class Detect(unittest.TestCase):
    def test_scheme_is_case_insensitive_and_needs_the_question_mark(self):
        for arg in ("magnet:?xt=a", "MAGNET:?xt=a", "Magnet:?"):
            self.assertTrue(magnet.is_magnet(arg), arg)
        for arg in ("magnet:xt=a", "magnet", "http://x/magnet:?", "a.torrent", ""):
            self.assertFalse(magnet.is_magnet(arg), arg)

    def test_label_shows_eight_hex_digits_only(self):
        self.assertEqual(magnet.label(IH), "magnet c9e15763…")


class Parse(unittest.TestCase):
    # AC1
    def test_hex_hash_any_case(self):
        for text in (HEX, HEX.upper(), HEX[:20] + HEX[20:].upper()):
            with self.subTest(text=text):
                m = magnet.parse(link(f"xt=urn:btih:{text}", "tr=http://t.example/a"))
                self.assertEqual(m.info_hash, IH)

    def test_base32_hash_any_case(self):
        for text in (B32, B32.lower()):
            with self.subTest(text=text):
                m = magnet.parse(link(f"xt=urn:btih:{text}", "tr=http://t.example/a"))
                self.assertEqual(m.info_hash, IH)

    def test_scheme_urn_and_percent_encoding(self):
        m = magnet.parse(f"MAGNET:?xt=URN:BTIH:{HEX}&tr=http%3A%2F%2Ft.example%3A80%2Fannounce%3Fk%3Dv")
        self.assertEqual(m.info_hash, IH)
        self.assertEqual(m.trackers, ("http://t.example:80/announce?k=v",))

    def test_numbered_parameters(self):
        m = magnet.parse(link(f"xt.1=urn:btih:{HEX}", "tr.1=udp://a.example:1", "tr.2=udp://b.example:2",
                              "x.pe.1=1.2.3.4:5"))
        self.assertEqual(m.trackers, ("udp://a.example:1", "udp://b.example:2"))
        self.assertEqual(m.peers, (("1.2.3.4", 5),))

    def test_trackers_keep_their_order_drop_duplicates_and_unsupported_schemes(self):
        m = magnet.parse(link(
            f"xt=urn:btih:{HEX}", "tr=udp://a.example:1", "tr=http://b.example/x", "tr=ftp://c.example/x",
            "tr=wss://d.example", "tr=udp://a.example:1", "tr=https://e.example/", "tr=not a url", "tr=http://"))
        self.assertEqual(m.trackers, ("udp://a.example:1", "http://b.example/x", "https://e.example/"))

    def test_peers_in_ipv4_ipv6_and_hostname_form(self):
        m = magnet.parse(link(f"xt=urn:btih:{HEX}", "x.pe=10.0.0.1:6881", "x.pe=%5B2001%3Adb8%3A%3A1%5D:7000",
                              "x.pe=seed.example:80", "x.pe=10.0.0.1:6881"))
        self.assertEqual(m.peers, (("10.0.0.1", 6881), ("2001:db8::1", 7000), ("seed.example", 80)))

    def test_bad_peers_are_ignored(self):
        bad = ["x.pe=1.2.3.4", "x.pe=1.2.3.4:0", "x.pe=1.2.3.4:65536", "x.pe=1.2.3.4:abc", "x.pe=:80",
               "x.pe=[::1:80", "x.pe=[zz]:80", "x.pe=bad_host!:80", "x.pe=a%00b:80", "x.pe=",
               "x.pe=%5Bnot-ip%5D:80", "x.pe=1.2.3.4:-5"]
        m = magnet.parse(link(f"xt=urn:btih:{HEX}", *bad, "x.pe=9.9.9.9:9"))
        self.assertEqual(m.peers, (("9.9.9.9", 9),))

    def test_dn_is_decoded_and_sanitised(self):
        m = magnet.parse(link(f"xt=urn:btih:{HEX}", "dn=Linux+ISO%20%E2%9C%93", "tr=udp://a.example:1"))
        self.assertEqual(m.name, "Linux ISO ✓")
        m = magnet.parse(link(f"xt=urn:btih:{HEX}", "dn=a%2Fb%00c%1b%0ad" + "x" * 500, "tr=udp://a.example:1"))
        self.assertNotIn("/", m.name)
        self.assertTrue(all(c >= " " and c != "\x7f" for c in m.name))
        self.assertLessEqual(len(m.name), 200)
        self.assertIsNone(magnet.parse(link(f"xt=urn:btih:{HEX}", "tr=udp://a.example:1")).name)

    def test_unknown_parameters_are_ignored(self):
        m = magnet.parse(link(f"xt=urn:btih:{HEX}", "ws=http://w.example/f", "xl=5", "kt=a+b", "tr=udp://a:1"))
        self.assertEqual((m.trackers, m.peers), (("udp://a:1",), ()))

    def test_the_same_hash_twice_is_fine_and_a_hybrid_uses_the_v1_hash(self):
        same = link(f"xt=urn:btih:{HEX}", f"xt=urn:btih:{B32}", "tr=udp://a:1")
        self.assertEqual(magnet.parse(same).info_hash, IH)
        hybrid = link(f"xt=urn:btih:{HEX}", "xt=urn:btmh:1220" + "ab" * 32, "tr=udp://a:1")
        self.assertEqual(magnet.parse(hybrid).info_hash, IH)

    def test_unknown_topic_urns_are_ignored_next_to_one_btih(self):
        m = magnet.parse(link(f"xt=urn:sha1:{HEX}", f"xt=urn:btih:{HEX}", "xt=urn:ed2k:abcdef", "xt=plain",
                              "tr=udp://a:1"))
        self.assertEqual(m.info_hash, IH)
        two = link(f"xt=urn:btih:{HEX}", f"xt=urn:sha1:{'1' * 40}", f"xt=urn:btih:{'2' * 40}", "tr=udp://a:1")
        with self.assertRaises(metainfo.TorrentError):
            magnet.parse(two)  # two different btih stay invalid

    def test_caps_on_trackers_and_peers(self):
        trs = [f"tr=udp://t{i}.example:1" for i in range(150)]
        pes = [f"x.pe=10.0.{i // 250}.{i % 250 + 1}:5" for i in range(300)]
        m = magnet.parse(link(f"xt=urn:btih:{HEX}", *trs, *pes))
        self.assertEqual(len(m.trackers), 100)
        self.assertEqual(len(m.peers), 200)


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

    def test_v2_only_links(self):
        self.fails("magnet:?xt=urn:btmh:1220" + "ab" * 32 + "&tr=udp://a:1",
                   "v2-only magnet links are not supported")

    def test_no_trackers_and_no_peers(self):
        message = "magnet link has no trackers or peers (DHT is not supported)"
        exc = self.fails(f"magnet:?xt=urn:btih:{HEX}&dn=Secret+Name", message)
        self.assertEqual(exc.info_hash, IH)
        self.fails(f"magnet:?xt=urn:btih:{HEX}&tr=ftp://a.example/&x.pe=nonsense", message)

    def test_errors_never_show_the_link(self):
        for arg in (f"magnet:?xt=urn:btih:{HEX}&dn=Secret+Name&tr=wss://secret.example/pass",
                    "magnet:?xt=urn:btih:zz"):
            with self.assertRaises(metainfo.TorrentError) as cm:
                magnet.parse(arg)
            for part in ("Secret", "secret.example", HEX, "zz"):
                self.assertNotIn(part, str(cm.exception))

    def test_only_the_readable_hash_is_attached_to_an_error(self):
        self.assertIsNone(self.fails("magnet:?xt=urn:btih:zz", "invalid magnet link").info_hash)
        self.assertIsNone(self.fails("magnet:?xt=urn:btmh:1220" + "ab" * 32 + "&tr=udp://a:1",
                                     "v2-only magnet links are not supported").info_hash)


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
    return [blob[i:i + metadata.BLOCK] for i in range(0, len(blob), metadata.BLOCK)]


class Messages(unittest.TestCase):
    def test_ext_handshake_advertises_ut_metadata_and_size_only_when_known(self):
        plain = bencode.decode(metadata.ext_handshake())[0]
        self.assertEqual(plain[b"m"], {b"ut_metadata": metadata.OUR_UT_METADATA})
        self.assertNotIn(b"metadata_size", plain)
        self.assertEqual(bencode.decode(metadata.ext_handshake(1234))[0][b"metadata_size"], 1234)

    def test_parse_ext_handshake(self):
        body = bencode.encode({"m": {"ut_metadata": 7, "ut_pex": 1}, "metadata_size": 999, "v": "x"})
        self.assertEqual(metadata.parse_ext_handshake(body), metadata.ExtInfo(7, 999))
        self.assertEqual(metadata.parse_ext_handshake(bencode.encode({"m": {}})),
                         metadata.ExtInfo(None, None))
        self.assertEqual(metadata.parse_ext_handshake(bencode.encode({"m": {"ut_metadata": 0}})),
                         metadata.ExtInfo(None, None))

    def test_parse_ext_handshake_rejects_garbage(self):
        for body in (b"", b"xx", b"i1e", b"le", bencode.encode({"m": 3}),
                     bencode.encode({"m": {"ut_metadata": b"a"}}),
                     bencode.encode({"m": {"ut_metadata": 300}}), bencode.encode({"metadata_size": b"1"})):
            with self.subTest(body=body[:12]):
                with self.assertRaises(ValueError):
                    metadata.parse_ext_handshake(body)

    def test_request_reject_and_data_round_trip(self):
        def body(frame):  # strip the length, message id and extension id
            return frame[6:]
        self.assertEqual(metadata.parse_metadata_msg(body(metadata.request_msg(3, 2))), (0, 2, None, b""))
        self.assertEqual(metadata.parse_metadata_msg(body(metadata.reject_msg(3, 5))), (2, 5, None, b""))
        self.assertEqual(metadata.parse_metadata_msg(body(metadata.data_msg(3, 1, 20000, b"RAW\x00bytes"))),
                         (1, 1, 20000, b"RAW\x00bytes"))
        self.assertEqual(metadata.request_msg(9, 0)[4], wire.EXTENDED)
        self.assertEqual(metadata.request_msg(9, 0)[5], 9)

    def test_parse_metadata_msg_rejects_garbage(self):
        for body in (b"", b"zz", b"i1e", bencode.encode({"piece": 1}), bencode.encode({"msg_type": 1}),
                     bencode.encode({"msg_type": 1, "piece": -1}),
                     bencode.encode({"msg_type": 1, "piece": 0, "total_size": b"x"})):
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

    def test_one_peer_supplies_everything(self):
        self.assertTrue(self.a.offer("a", len(self.info)))
        self.assertEqual(self.fetch_all("a"), self.info)
        self.assertEqual(self.a.metadata, self.info)

    def test_blocks_come_from_several_peers(self):
        for p in "ab":
            self.assertTrue(self.a.offer(p, len(self.info)))
        first = self.a.next_request("a", self.clock())
        second = self.a.next_request("b", self.clock())
        self.assertNotEqual(first, second)  # no block is asked of two peers at once
        self.assertIsNone(self.a.next_request("a", self.clock()))  # one request in flight per peer
        self.assertIsNone(self.feed("a", first))
        self.assertIsNone(self.feed("b", second))
        third = self.a.next_request("a", self.clock())
        self.assertEqual(sorted([first, second, third]), [0, 1, 2])
        self.assertEqual(self.feed("a", third), self.info)

    def test_hostile_sizes_drop_the_peer(self):
        for size in (0, -1, metadata.MAX_METADATA + 1, 2 ** 40, None, "x"):
            with self.subTest(size=size):
                self.assertFalse(self.a.offer("h", size))
        self.assertTrue(self.a.offer("h", metadata.MAX_METADATA))

    def test_at_most_three_sizes_are_tracked_and_an_idle_one_makes_room(self):
        for i, size in enumerate((100, 200, 300)):
            self.assertTrue(self.a.offer(f"p{i}", size))
        self.assertTrue(self.a.offer("p3", 400))  # valid: not banned, an idle slot is given up for it
        self.assertEqual(len(self.a._attempts), metadata.MAX_SIZES)
        self.assertIn(400, self.a._attempts)
        self.assertTrue(self.a.offer("p4", 400))  # a known size is fine
        self.a.peer_gone("p3")
        self.a.peer_gone("p4")  # nobody offers 400 any more, and it holds nothing
        self.assertNotIn(400, self.a._attempts)

    def test_three_idle_sybil_sizes_do_not_keep_an_honest_peer_out(self):
        for i, size in enumerate((100_000, 200_000, 300_000)):
            self.assertTrue(self.a.offer(f"sybil{i}", size))
            self.assertIsNotNone(self.a.next_request(f"sybil{i}", self.clock()))  # asked, never answers
        self.assertTrue(self.a.offer("honest", len(self.info)))
        self.assertEqual(self.fetch_all("honest"), self.info)
        for i in range(3):  # whatever the evicted ones send later is ignored, not a reason to ban
            self.assertIsNone(self.a.next_request(f"sybil{i}", self.clock()))

    def fill_with_block_holders(self):
        for i in range(3):
            size = 100_000 + i * 20_000
            self.a.offer(f"p{i}", size)
            piece = self.a.next_request(f"p{i}", self.clock())
            self.a.received(f"p{i}", piece, b"z" * metadata.BLOCK)

    def test_a_slot_holding_fresh_blocks_is_kept_and_the_new_size_waits(self):
        self.fill_with_block_holders()
        self.assertTrue(self.a.offer("late", len(self.info)))  # not invalid: keep it for the download
        self.assertIsNone(self.a.next_request("late", self.clock()))
        self.assertEqual(len(self.a._attempts), 3)

    def test_slots_holding_blocks_are_freed_when_their_peers_leave(self):
        self.fill_with_block_holders()
        self.assertTrue(self.a.offer("late", len(self.info)))
        self.assertIsNone(self.a.next_request("late", self.clock()))
        for i in range(3):
            self.a.peer_gone(f"p{i}")
        self.assertEqual(self.a._stored, 0)
        self.assertEqual(self.fetch_all("late"), self.info)  # offered again without reconnecting

    def test_slots_holding_stale_blocks_are_freed_while_their_peers_stay(self):
        self.fill_with_block_holders()
        self.assertTrue(self.a.offer("late", len(self.info)))
        self.assertIsNone(self.a.next_request("late", self.clock()))
        self.clock.now += metadata.STALE
        self.assertEqual(self.fetch_all("late"), self.info)
        self.assertEqual(self.a._stored, 0)
        self.assertLessEqual(len(self.a._attempts), metadata.MAX_SIZES)

    def test_an_evicted_peer_answering_late_is_ignored(self):
        for i, size in enumerate((100_000, 200_000, 300_000)):
            self.a.offer(f"s{i}", size)
        pieces = [self.a.next_request(f"s{i}", self.clock()) for i in range(3)]  # all three in flight
        piece = pieces[0]
        self.a.offer("honest", len(self.info))  # the first slot goes
        self.assertIsNone(self.a.received("s0", piece, b"z" * metadata.BLOCK))
        self.assertEqual(self.a._stored, 0)

    def test_unrequested_blocks_drop_the_peer(self):
        self.a.offer("a", len(self.info))
        with self.assertRaises(ValueError):
            self.feed("a", 0)  # nothing was asked
        with self.assertRaises(ValueError):
            self.feed("nobody", 0)  # never offered
        piece = self.a.next_request("a", self.clock())
        with self.assertRaises(ValueError):
            self.feed("a", (piece + 1) % 3)  # another block than the one asked

    def test_a_block_may_not_be_sent_twice(self):
        self.a.offer("a", len(self.info))
        piece = self.a.next_request("a", self.clock())
        self.feed("a", piece)
        with self.assertRaises(ValueError):
            self.feed("a", piece)

    def test_wrong_block_lengths_drop_the_peer(self):
        for bad in (b"", b"x" * (metadata.BLOCK - 1), b"x" * (metadata.BLOCK + 1)):
            with self.subTest(n=len(bad)):
                a = metadata.MetadataAssembler(self.ih, self.clock)
                a.offer("a", len(self.info))
                piece = a.next_request("a", self.clock())
                self.assertEqual(piece, 0)
                with self.assertRaises(ValueError):
                    a.received("a", piece, bad)

    def test_the_last_block_is_the_remainder(self):
        self.a.offer("a", len(self.info))
        for _ in range(2):
            self.feed("a", self.a.next_request("a", self.clock()))
        last = self.a.next_request("a", self.clock())
        with self.assertRaises(ValueError):
            self.feed("a", last, b"x" * metadata.BLOCK)

    def test_a_total_that_differs_from_the_announced_size_drops_the_peer(self):
        self.a.offer("a", len(self.info))
        piece = self.a.next_request("a", self.clock())
        with self.assertRaises(ValueError):
            self.feed("a", piece, total=len(self.info) + 1)

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

    def test_a_hash_mismatch_from_several_peers_bans_nobody(self):
        self.mismatch_from_two_peers()
        self.assertEqual(self.a.banned, set())
        self.assertIsNone(self.a.metadata)
        self.assertEqual(self.a._stored, 0)  # everything was thrown away

    def test_after_a_mismatch_every_block_of_a_set_comes_from_one_peer(self):
        self.mismatch_from_two_peers()
        first = self.a.next_request("good", self.clock())
        self.assertIsNotNone(first)
        self.assertIsNone(self.a.next_request("evil", self.clock()))  # good owns this attempt
        self.assertIsNone(self.a.received("good", first, self.parts[first]))
        self.assertIsNone(self.a.next_request("evil", self.clock()))
        self.assertEqual(self.fetch_all("good"), self.info)
        self.assertEqual(self.a.banned, set())

    def test_only_a_peer_whose_own_complete_set_fails_is_banned(self):
        self.mismatch_from_two_peers()
        corrupt = self.corrupt_set()
        piece = self.a.next_request("evil", self.clock())  # evil owns the next attempt
        self.assertIsNone(self.a.next_request("good", self.clock()))
        while piece is not None:
            self.a.received("evil", piece, corrupt[piece])
            piece = self.a.next_request("evil", self.clock())
        self.assertEqual(self.a.banned, {"evil"})
        self.assertEqual(self.fetch_all("good"), self.info)
        self.assertEqual(self.a.banned, {"evil"})

    def test_a_late_block_from_another_peer_does_not_join_a_single_source_set(self):
        corrupt = self.corrupt_set()
        for peer in ("late", "good", "third"):
            self.a.offer(peer, len(self.info))
        self.assertEqual(self.a.next_request("late", self.clock()), 0)  # answers only after the timeout
        self.clock.now += metadata.REQUEST_TIMEOUT + 1
        self.assertEqual(self.a.next_request("good", self.clock()), 0)
        self.assertEqual(self.a.next_request("third", self.clock()), 1)
        self.a.received("good", 0, corrupt[0])
        self.a.received("third", 1, corrupt[1])
        self.assertEqual(self.a.next_request("good", self.clock()), 2)
        self.assertIsNone(self.a.received("good", 2, corrupt[2]))  # complete, wrong, two senders
        self.assertEqual(self.a.banned, set())
        self.assertEqual(self.a.next_request("good", self.clock()), 0)  # good owns the next attempt
        self.assertIsNone(self.a.received("late", 0, self.parts[0]))  # late: ignored, not stored
        self.assertEqual(self.a._stored, 0)
        self.assertEqual(self.a.banned, set())

    def test_a_silent_owner_of_a_single_source_set_is_replaced(self):
        self.mismatch_from_two_peers()
        self.assertIsNotNone(self.a.next_request("evil", self.clock()))  # evil owns it and goes silent
        self.assertIsNone(self.a.next_request("good", self.clock()))
        self.clock.now += metadata.STALE + 1
        self.assertIsNotNone(self.a.next_request("good", self.clock()))
        self.assertIsNone(self.a.next_request("evil", self.clock()))

    def test_a_gone_owner_hands_the_attempt_over(self):
        self.mismatch_from_two_peers()
        self.assertIsNotNone(self.a.next_request("evil", self.clock()))
        self.a.peer_gone("evil")
        self.assertEqual(self.fetch_all("good"), self.info)

    def test_only_peers_that_sent_a_stored_block_are_banned(self):
        self.a.offer("evil", len(self.info))
        self.a.offer("idle", len(self.info))
        corrupt = [bytes([self.parts[0][0] ^ 1]) + self.parts[0][1:], *self.parts[1:]]
        for _ in range(3):
            piece = self.a.next_request("evil", self.clock())
            self.a.received("evil", piece, corrupt[piece])
        self.assertEqual(self.a.banned, {"evil"})
        self.assertEqual(self.fetch_all("idle"), self.info)

    def test_an_unanswered_request_goes_to_another_peer_after_the_timeout(self):
        self.a.offer("slow", len(self.info))
        self.a.offer("fast", len(self.info))
        slow_piece = self.a.next_request("slow", self.clock())
        fast_piece = self.a.next_request("fast", self.clock())
        self.feed("fast", fast_piece)
        # slow still holds its block
        self.assertNotEqual(slow_piece, self.a.next_request("fast", self.clock()))
        self.clock.now += metadata.REQUEST_TIMEOUT + 1
        self.assertEqual(self.a.next_request("fast", self.clock()), slow_piece)

    def test_when_every_missing_block_is_with_a_slow_peer_an_idle_peer_asks_for_one_too(self):
        info, ih = info_for(1000)
        a = metadata.MetadataAssembler(ih, self.clock)
        a.offer("silent", len(info))
        a.offer("honest", len(info))
        self.assertEqual(a.next_request("silent", self.clock()), 0)
        self.assertEqual(a.next_request("honest", self.clock()), 0)  # only one block, still unanswered
        self.assertEqual(a.received("honest", 0, info), info)

    def test_a_late_answer_after_the_timeout_is_still_accepted_once(self):
        self.a.offer("slow", len(self.info))
        piece = self.a.next_request("slow", self.clock())
        self.clock.now += metadata.REQUEST_TIMEOUT + 1
        self.assertIsNotNone(self.a.next_request("slow", self.clock()))  # asked for the next one
        self.feed("slow", piece)  # but the first answer arrives: fine

    def test_a_peer_is_not_asked_again_for_a_block_it_still_owes(self):
        info, ih = info_for(1000)  # one block
        a = metadata.MetadataAssembler(ih, self.clock)
        a.offer("slow", len(info))
        self.assertEqual(a.next_request("slow", self.clock()), 0)
        self.clock.now += metadata.REQUEST_TIMEOUT + 1
        self.assertIsNone(a.next_request("slow", self.clock()))
        self.assertEqual(a.received("slow", 0, info), info)  # its single slow answer is not an error

    def test_a_single_slow_seeder_completes_over_several_timeouts(self):
        self.a.offer("slow", len(self.info))
        got = None
        for _ in range(3):
            piece = self.a.next_request("slow", self.clock())
            self.assertIsNotNone(piece)
            self.clock.now += metadata.REQUEST_TIMEOUT + 1
            got = self.feed("slow", piece) or got
        self.assertEqual(got, self.info)

    def test_both_the_slow_and_the_fast_peer_may_answer_a_re_asked_block(self):
        self.a.offer("slow", len(self.info))
        self.a.offer("fast", len(self.info))
        slow_piece = self.a.next_request("slow", self.clock())
        self.clock.now += metadata.REQUEST_TIMEOUT + 1
        self.assertEqual(self.a.next_request("fast", self.clock()), slow_piece)
        self.assertIsNone(self.feed("fast", slow_piece))
        self.assertIsNone(self.feed("slow", slow_piece))  # late and a duplicate: dropped quietly
        self.assertEqual(self.a.banned, set())

    def test_a_peer_that_announces_a_second_size_starts_over_with_it(self):
        other, _ = info_for(20_000)
        self.a.offer("a", len(self.info))
        self.assertEqual(self.a.next_request("a", self.clock()), 0)
        self.assertTrue(self.a.offer("a", len(other)))
        self.assertNotIn(len(self.info), self.a._attempts)  # nobody offers the first size any more
        self.assertEqual(self.a._flying, {})
        self.assertEqual(self.a._asked, {})
        self.assertEqual(self.a.next_request("a", self.clock()), 0)
        with self.assertRaises(ValueError):  # the old request is void
            self.a.received("a", 1, self.parts[1])

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

    def test_success_clears_every_other_attempt(self):
        a = metadata.MetadataAssembler(self.ih, self.clock)
        a.offer("h", 100_000)
        piece = a.next_request("h", self.clock())
        a.received("h", piece, b"z" * metadata.BLOCK)
        a.offer("good", len(self.info))
        got = None
        while (piece := a.next_request("good", self.clock())) is not None:
            got = a.received("good", piece, self.parts[piece]) or got
        self.assertEqual(got, self.info)
        self.assertEqual(a._stored, 0)
        self.assertEqual(a._attempts[100_000].blocks, {})
        self.assertEqual(a._flying, {})

    def test_a_gone_peer_frees_its_block(self):
        self.a.offer("a", len(self.info))
        self.a.offer("b", len(self.info))
        piece = self.a.next_request("a", self.clock())
        self.a.peer_gone("a")
        self.assertEqual(self.a.next_request("b", self.clock()), piece)

    def test_a_rejecting_peer_is_not_asked_again(self):
        self.a.offer("a", len(self.info))
        piece = self.a.next_request("a", self.clock())
        self.a.rejected("a", piece)
        self.assertIsNone(self.a.next_request("a", self.clock()))

    def test_nothing_is_requested_from_a_peer_that_never_announced_a_size(self):
        self.assertIsNone(self.a.next_request("x", self.clock()))

    def test_blocks_for_different_sizes_are_kept_apart(self):
        other_info, other_ih = info_for(20_000)
        self.a.offer("liar", len(other_info))  # announces another size for the same hash
        self.a.offer("honest", len(self.info))
        for _ in range(2):
            piece = self.a.next_request("liar", self.clock())
            if piece is None:
                break
            self.a.received("liar", piece, b"x" * metadata.BLOCK if piece < len(blocks(other_info)) - 1
                            else b"x" * (len(other_info) - piece * metadata.BLOCK))
        self.assertEqual(self.fetch_all("honest"), self.info)

    def test_memory_never_exceeds_the_metadata_cap(self):
        """Hostile peers announcing several huge sizes and answering every request hold at most 10 MiB."""
        a = metadata.MetadataAssembler(b"\x00" * 20, self.clock)  # nothing will ever verify
        for i, peer in enumerate(("a", "b", "c")):
            self.assertTrue(a.offer(peer, metadata.MAX_METADATA - i))
        peak = 0
        for _ in range(3000):
            moved = False
            for peer in ("a", "b", "c"):
                piece = a.next_request(peer, self.clock())
                if piece is None:
                    continue
                moved = True
                size = metadata.MAX_METADATA - ("abc".index(peer))
                n = min(metadata.BLOCK, size - piece * metadata.BLOCK)
                try:
                    a.received(peer, piece, b"z" * n)
                except ValueError:
                    pass
                peak = max(peak, a._stored)
            if not moved:
                break
        self.assertLessEqual(peak, metadata.MAX_METADATA)

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


class AssemblerMemory(unittest.TestCase):
    """NFR1: whatever peers send, assembly holds at most MAX_METADATA plus a block per peer (and a little)."""

    def test_a_full_size_set_of_junk_blocks_peaks_within_the_bound_and_is_released(self):
        import tracemalloc

        peers = 3
        asm = metadata.MetadataAssembler(b"\x11" * 20)
        n = -(-metadata.MAX_METADATA // metadata.BLOCK)
        names = [("10.0.0.%d" % i, 1) for i in range(peers)]
        tracemalloc.start()
        try:
            tracemalloc.reset_peak()
            base = tracemalloc.get_traced_memory()[0]
            for name in names:
                self.assertTrue(asm.offer(name, metadata.MAX_METADATA))
            piece = 0
            while piece < n:  # peers take turns: the blocks of one set come from several of them
                name = names[piece % peers]
                asked = asm.next_request(name, 0.0)
                self.assertIsNotNone(asked)
                length = min(metadata.BLOCK, metadata.MAX_METADATA - asked * metadata.BLOCK)
                asm.received(name, asked, bytes(length), metadata.MAX_METADATA)
                piece += 1
            peak = tracemalloc.get_traced_memory()[1] - base
        finally:
            tracemalloc.stop()
        self.assertEqual(asm.metadata, None)
        self.assertLess(peak, metadata.MAX_METADATA + peers * metadata.BLOCK + 2 * 1024 * 1024,
                        f"peak {peak} bytes")
        print(f"\n[tracemalloc] assembler peak with a {metadata.MAX_METADATA} byte hostile set: {peak} bytes")

    def test_a_valid_set_near_the_cap_is_assembled_without_a_second_full_copy(self):
        import tracemalloc

        blob = bytes(metadata.MAX_METADATA - 5)  # not a whole number of blocks
        parts = blocks(blob)
        asm = metadata.MetadataAssembler(hashlib.sha1(blob).digest())
        asm.offer("a", len(blob))
        tracemalloc.start()
        try:
            tracemalloc.reset_peak()
            base = tracemalloc.get_traced_memory()[0]
            got = None
            while (piece := asm.next_request("a", 0.0)) is not None:
                got = asm.received("a", piece, bytearray(parts[piece])) or got  # stored as its own copy
            peak = tracemalloc.get_traced_memory()[1] - base
        finally:
            tracemalloc.stop()
        self.assertEqual(got, blob)
        self.assertEqual(asm._stored, 0)
        # one copy of the set (the blocks turn into the result as they are released) plus the growth slack of
        # the buffer; a second full copy would peak near twice the size
        bound = len(blob) * 6 // 5 + 2 * metadata.BLOCK
        self.assertLess(peak, bound, f"peak {peak} bytes")
        print(f"\n[tracemalloc] assembler peak assembling a valid {len(blob)} byte set: {peak} bytes "
              f"(bound {bound})")


class PeerBitfield(unittest.TestCase):
    """N5: a bitfield arriving around the switch to the download is checked against the real count."""

    def conn(self, meta=True):
        picker = mock.Mock()
        picker.verified_bytes.return_value = 0
        hashes = [b"\x00" * 20] * 20  # three bytes of bitfield
        sess = SimpleNamespace(meta=SimpleNamespace(hashes=hashes) if meta else None, picker=picker,
                               banned=set(), log=lambda m: None, interest_changed=lambda c: None)
        peer = PeerConn(sess, ("127.0.0.1", 1))
        peer.handshaken = True
        return peer, sess

    def test_a_bitfield_of_the_wrong_size_read_without_a_piece_count_is_refused_after_the_switch(self):
        for payload in (b"\xff\xff\xf0\x00", b"\xff\xff", b"\x00" * 125000):
            with self.subTest(n=len(payload)):
                async def go():
                    peer, _ = self.conn()
                    with self.assertRaises(ValueError):
                        peer.on_message(wire.BITFIELD, payload, 0)
                asyncio.run(go())

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

    def test_a_right_sized_bitfield_is_accepted_on_both_paths(self):
        async def go():
            peer, sess = self.conn()
            peer.on_message(wire.BITFIELD, b"\xff\xff\xf0", 0)
            sess.picker.peer_bitfield.assert_called_once()
            peer, sess = self.conn(meta=False)
            peer.on_message(wire.BITFIELD, b"\xff\xff\xf0", 0)
            sess.meta = SimpleNamespace(hashes=[b"\x00" * 20] * 20)
            peer.enter_download()
            sess.picker.peer_bitfield.assert_called_once()
        asyncio.run(go())

    def test_request_and_piece_before_the_metadata_drop_the_peer(self):
        async def go():
            for mid in (wire.REQUEST, wire.PIECE, wire.CANCEL):
                payload = b"\x00" * 12
                peer, _ = self.conn(meta=False)
                with self.assertRaises(ValueError):
                    peer.on_message(mid, payload, 0)
        asyncio.run(go())


class FromInfo(unittest.TestCase):
    """AC6 and AC11 unit parts: info bytes to Meta, and the saved .torrent."""

    def setUp(self):
        self.t = torrentkit.make_torrent([("a/x.bin", torrentkit.content(40000, "x")), ("b.bin", b"hello")],
                                         name="item", multi=True)
        self.info = bencode.encode(bencode.decode(self.t.data)[0][b"info"])

    def test_from_info_matches_the_torrent_it_came_from(self):
        meta = metainfo.from_info(self.info, ("udp://a.example:1", "http://b.example/x"))
        reference = metainfo.parse(self.t.data)
        self.assertEqual(meta.info_hash, self.t.info_hash)
        self.assertEqual((meta.name, meta.hashes, meta.files, meta.total), (reference.name, reference.hashes,
                                                                            reference.files, reference.total))
        self.assertEqual(meta.trackers, (("udp://a.example:1", "http://b.example/x"),))

    def test_from_info_without_trackers(self):
        self.assertEqual(metainfo.from_info(self.info).trackers, ())

    def test_invalid_info_is_a_torrent_error(self):
        for bad in (b"", b"i1e", b"de", b"d4:name1:xe", b"zzz"):
            with self.subTest(bad=bad):
                with self.assertRaises(metainfo.TorrentError):
                    metainfo.from_info(bad)

    def test_trailing_data_after_the_info_is_not_accepted(self):
        with self.assertRaises(metainfo.TorrentError):
            metainfo.from_info(self.info + b"x")

    def test_torrent_bytes_round_trip(self):
        trackers = ("udp://a.example:1", "http://b.example/x?k=v")
        data = metainfo.torrent_bytes(self.info, trackers)
        meta = metainfo.parse(data)
        self.assertEqual(meta.info_hash, self.t.info_hash)
        self.assertEqual(meta.trackers, (trackers,))
        top = bencode.decode(data)[0]
        self.assertEqual(top[b"announce"], b"udp://a.example:1")
        self.assertEqual(top[b"announce-list"], [[b"udp://a.example:1", b"http://b.example/x?k=v"]])
        self.assertEqual(hashlib.sha1(data[data.index(b"4:info") + 6:-1]).digest(), self.t.info_hash)

    def test_torrent_bytes_without_trackers_has_neither_announce_key(self):
        data = metainfo.torrent_bytes(self.info, ())
        top = bencode.decode(data)[0]
        self.assertEqual(list(top), [b"info"])
        self.assertEqual(metainfo.parse(data).trackers, ())


class ServeMetadata(unittest.TestCase):
    """R18 on one connection: requests are rejected before the metadata is known, then answered from a
    queue of at most 8 per peer; a bad piece index drops the peer."""

    def setUp(self):
        self.info = bytes(range(256)) * 160  # 40960 bytes: three blocks (16384, 16384, 8192)

    def conn(self, known=True):
        sess = SimpleNamespace(meta=None, metadata=self.info if known else None, picker=None, banned=set(),
                               log=lambda m: None, assembler=None)
        peer = PeerConn(sess, ("127.0.0.1", 1))
        peer.handshaken = True
        peer.their_ut = 7
        peer.writer = mock.Mock()
        return peer, sess

    def ask(self, peer, piece):
        peer.on_extended(bytes([metadata.OUR_UT_METADATA]) + metadata.request_msg(7, piece)[6:], 0)

    def sent(self, peer):
        return [c.args[0] for c in peer.writer.write.call_args_list]

    def test_a_request_before_the_metadata_is_known_is_rejected(self):
        async def go():
            peer, _ = self.conn(known=False)
            self.ask(peer, 1)
            self.assertEqual(self.sent(peer), [metadata.reject_msg(7, 1)])
        asyncio.run(go())

    def test_requests_queue_and_wait_for_the_serving_task(self):
        async def go():
            peer, _ = self.conn()
            self.ask(peer, 0)
            self.ask(peer, 2)
            self.assertEqual(list(peer.meta_queue), [0, 2])
            self.assertEqual(self.sent(peer), [])
        asyncio.run(go())

    def test_a_piece_past_the_end_drops_the_peer(self):
        async def go():
            peer, _ = self.conn()
            for piece in (3, 1000):
                with self.assertRaises(ValueError):
                    self.ask(peer, piece)
        asyncio.run(go())

    def test_the_ninth_waiting_request_drops_the_peer(self):
        async def go():
            peer, _ = self.conn()
            for _ in range(8):
                self.ask(peer, 0)
            with self.assertRaises(ValueError):
                self.ask(peer, 0)
        asyncio.run(go())

    def test_the_serving_task_sends_the_right_blocks(self):
        async def go():
            peer, sess = self.conn()
            sess.opts = SimpleNamespace(idle_timeout=5)
            peer.writer.drain = mock.AsyncMock()
            for piece in (2, 0, 1):
                self.ask(peer, piece)
            task = asyncio.ensure_future(peer.serve())
            for _ in range(20):
                await asyncio.sleep(0)
            task.cancel()
            B = metadata.BLOCK
            want = [metadata.data_msg(7, p, len(self.info), self.info[p * B:(p + 1) * B]) for p in (2, 0, 1)]
            self.assertEqual(self.sent(peer), want)
        asyncio.run(go())

    def test_a_request_without_their_extension_handshake_drops_the_peer(self):
        async def go():
            peer, _ = self.conn()
            peer.their_ut = None
            with self.assertRaises(ValueError):
                self.ask(peer, 0)
        asyncio.run(go())

    def test_entering_the_download_sends_an_updated_extension_handshake_with_the_size(self):
        async def go():
            peer, sess = self.conn()
            peer.ext = True
            sess.meta = SimpleNamespace(hashes=[b"\x00" * 20] * 20)
            sess.picker = mock.Mock()
            sess.picker.verified_bytes.return_value = 0
            sess.picker.interesting.return_value = False
            sess.interest_changed = lambda c: None
            peer.enter_download()
            self.assertIn(wire.extended(0, metadata.ext_handshake(len(self.info))), self.sent(peer))
        asyncio.run(go())


class StoredMeta(unittest.TestCase):
    """R14: the stored metadata file is used only when it is a plain file whose SHA-1 is the info-hash."""

    def setUp(self):
        import tempfile

        from dmcore import resume

        self.resume = resume
        self.dir = Path(tempfile.mkdtemp(prefix="dm-meta-")).resolve()
        self.addCleanup(__import__("shutil").rmtree, self.dir, ignore_errors=True)
        t = torrentkit.make_torrent([("a.bin", torrentkit.content(40000, "m"))], name="a.bin")
        self.info = bencode.encode(bencode.decode(t.data)[0][b"info"])
        self.ih = t.info_hash
        self.path = resume.meta_path(self.dir, "k" * 24)

    def test_the_name_is_hidden_and_keyed_without_the_item_name(self):
        self.assertEqual(self.path, self.dir / f".dm.{'k' * 24}.dm-meta")

    def test_write_then_read_round_trips(self):
        self.resume.write_meta(self.path, self.info)
        self.assertEqual(self.resume.read_meta(self.path, self.ih), self.info)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), [self.path.name])  # no temp left

    def test_missing_file_is_none(self):
        self.assertIsNone(self.resume.read_meta(self.path, self.ih))

    def test_a_file_that_does_not_hash_to_the_info_hash_is_deleted(self):
        self.path.write_bytes(self.info + b"x")
        self.assertIsNone(self.resume.read_meta(self.path, self.ih))
        self.assertFalse(self.path.exists())

    def test_an_oversized_file_is_deleted_without_reading_it_all(self):
        self.path.write_bytes(b"a" * (metadata.MAX_METADATA + 1))
        self.assertIsNone(self.resume.read_meta(self.path, self.ih))
        self.assertFalse(self.path.exists())

    def test_a_symlink_is_never_followed_and_is_removed(self):
        other = self.dir / "other"
        other.write_bytes(self.info)
        self.path.symlink_to(other)
        self.assertIsNone(self.resume.read_meta(self.path, self.ih))
        self.assertFalse(self.path.is_symlink())
        self.assertEqual(other.read_bytes(), self.info)

    def test_a_directory_in_the_way_is_not_read(self):
        self.path.mkdir()
        self.assertIsNone(self.resume.read_meta(self.path, self.ih))

    def test_write_replaces_an_older_file(self):
        self.path.write_bytes(b"old")
        self.resume.write_meta(self.path, self.info)
        self.assertEqual(self.path.read_bytes(), self.info)

    def test_write_never_follows_a_symlink_at_the_target(self):
        other = self.dir / "other"
        other.write_bytes(b"keep")
        self.path.symlink_to(other)
        self.resume.write_meta(self.path, self.info)
        self.assertEqual(other.read_bytes(), b"keep")
        self.assertEqual(self.path.read_bytes(), self.info)


if __name__ == "__main__":
    unittest.main()
