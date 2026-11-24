"""Unit tests for the wire format and the piece picker of the torrent support in dm."""

import asyncio
import random
import struct
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "plugin" / "dm"))
sys.path.insert(0, str(ROOT / "tests"))

import torrentkit  # noqa: E402
from dmcore.torrent import bencode, metainfo, picker, tracker, wire  # noqa: E402

IH = b"\x11" * 20
PID = b"-DM0200-" + b"x" * 12


def feed(data, eof=True):
    """A StreamReader holding data; build it inside a running loop (3.12 needs one)."""
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    if eof:
        reader.feed_eof()
    return reader


def run_with(data, fn, eof=True):
    async def go():
        return await fn(feed(data, eof))
    return asyncio.run(go())


def read(data, npieces=100, eof=True):
    return run_with(data, lambda r: wire.read_message(r, npieces), eof)


def framed(mid, payload=b""):
    return struct.pack(">I", len(payload) + 1) + bytes([mid]) + payload


class Wire(unittest.TestCase):
    def test_handshake_layout(self):
        hs = wire.handshake(IH, PID)
        self.assertEqual(len(hs), 68)
        self.assertEqual(hs[:20], b"\x13BitTorrent protocol")
        self.assertEqual(hs[28:48], IH)
        self.assertEqual(hs[48:], PID)

    def test_read_handshake_returns_the_peer_id_and_reserved_bytes(self):
        got = run_with(wire.handshake(IH, PID), lambda r: wire.read_handshake(r, IH))
        self.assertEqual(got, (PID, wire.RESERVED))

    def test_handshake_sets_the_extension_bit_and_supports_extensions_reads_it(self):
        hs = wire.handshake(IH, PID)
        self.assertEqual(hs[20:28], bytes([0, 0, 0, 0, 0, 0x10, 0, 0]))
        self.assertTrue(wire.supports_extensions(hs[20:28]))
        self.assertFalse(wire.supports_extensions(bytes(8)))
        self.assertFalse(wire.supports_extensions(b""))

    def test_extended_message_framing(self):
        self.assertEqual(wire.extended(0, b"d1:ai1ee"), framed(wire.EXTENDED, b"\x00d1:ai1ee"))
        self.assertEqual(read(wire.extended(3, b"xyz")), (wire.EXTENDED, b"\x03xyz"))

    def test_extended_payload_limits(self):
        biggest = wire.MAX_EXT
        self.assertEqual(read(framed(wire.EXTENDED, b"\x00" * biggest))[0], wire.EXTENDED)
        with self.assertRaises(ValueError):
            read(framed(wire.EXTENDED, b""))
        with self.assertRaises(ValueError):
            read(framed(wire.EXTENDED, b"\x00" * (wire.MAX_MSG + 1)))

    def test_an_oversized_extension_message_is_read_and_skipped(self):
        async def go(reader):
            return [await wire.read_message(reader, 100) for _ in range(2)]
        data = framed(wire.EXTENDED, b"\x00" * (wire.MAX_EXT + 1)) + wire.have(7)
        got = run_with(data, go)
        self.assertEqual(got, [None, (wire.HAVE, struct.pack(">I", 7))])

    def test_bitfield_limit_while_the_piece_count_is_unknown(self):
        most = (metainfo.MAX_PIECES + 7) // 8
        self.assertEqual(read(framed(wire.BITFIELD, b"\x00" * most), None)[0], wire.BITFIELD)
        self.assertEqual(read(framed(wire.BITFIELD, b"\x00"), None)[0], wire.BITFIELD)
        for n in (0, most + 1):
            with self.subTest(n=n):
                with self.assertRaises(ValueError):
                    read(framed(wire.BITFIELD, b"\x00" * n), None)

    def test_fixed_messages_keep_their_limits_while_the_piece_count_is_unknown(self):
        with self.assertRaises(ValueError):
            read(framed(wire.HAVE, b"abc"), None)
        self.assertEqual(read(wire.have(5), None), (wire.HAVE, struct.pack(">I", 5)))

    def test_read_handshake_rejects_wrong_info_hash_protocol_and_short_data(self):
        bad_proto = bytearray(wire.handshake(IH, PID))
        bad_proto[1] = ord("X")
        for data in (wire.handshake(b"\x22" * 20, PID), bytes(bad_proto), wire.handshake(IH, PID)[:30]):
            with self.subTest(data=data[:8]):
                with self.assertRaises(ValueError):
                    run_with(data, lambda r: wire.read_handshake(r, IH))

    def test_keep_alive_is_none(self):
        self.assertIsNone(read(b"\x00\x00\x00\x00"))

    def test_each_known_message_round_trips(self):
        cases = [
            (wire.CHOKE, wire.msg(wire.CHOKE), b""),
            (wire.UNCHOKE, wire.msg(wire.UNCHOKE), b""),
            (wire.INTERESTED, wire.msg(wire.INTERESTED), b""),
            (wire.NOT_INTERESTED, wire.msg(wire.NOT_INTERESTED), b""),
            (wire.HAVE, wire.have(7), struct.pack(">I", 7)),
            (wire.BITFIELD, wire.bitfield(b"\xff" * 13), b"\xff" * 13),
            (wire.REQUEST, wire.request(1, 2, 3), struct.pack(">III", 1, 2, 3)),
            (wire.CANCEL, wire.cancel(1, 2, 3), struct.pack(">III", 1, 2, 3)),
            (wire.PIECE, wire.piece(4, 16384, b"ab"), struct.pack(">II", 4, 16384) + b"ab"),
        ]
        for mid, data, payload in cases:
            with self.subTest(mid=mid):
                self.assertEqual(read(data), (mid, payload))

    def test_wrong_fixed_lengths_are_refused(self):
        for mid, payload in ((wire.CHOKE, b"x"), (wire.UNCHOKE, b"x"), (wire.INTERESTED, b"x"),
                             (wire.NOT_INTERESTED, b"x"), (wire.HAVE, b"abc"), (wire.HAVE, b"abcde"),
                             (wire.REQUEST, b"x" * 11), (wire.REQUEST, b"x" * 13), (wire.CANCEL, b"x" * 11)):
            with self.subTest(mid=mid, n=len(payload)):
                with self.assertRaises(ValueError):
                    read(framed(mid, payload))

    def test_bitfield_length_must_match_the_piece_count(self):
        self.assertEqual(read(framed(wire.BITFIELD, b"\x00" * 13), 100)[0], wire.BITFIELD)
        for n in (12, 14):
            with self.assertRaises(ValueError):
                read(framed(wire.BITFIELD, b"\x00" * n), 100)

    def test_piece_limits(self):
        wire_max = 8 + wire.MAX_BLOCK  # payload: index, begin and a full block
        self.assertEqual(read(framed(wire.PIECE, b"\x00" * wire_max))[0], wire.PIECE)
        for n in (8, wire_max + 1):
            with self.assertRaises(ValueError):
                read(framed(wire.PIECE, b"\x00" * n))

    def test_oversized_unknown_message_is_refused_before_reading_it(self):
        with self.assertRaises(ValueError):
            read(struct.pack(">I", wire.MAX_MSG + 1), 10, eof=False)

    def test_unknown_ids_up_to_the_limit_are_returned(self):
        self.assertEqual(read(framed(21, b"hello")), (21, b"hello"))
        self.assertEqual(read(framed(99, b"")), (99, b""))

    def test_truncated_message_raises(self):
        with self.assertRaises((asyncio.IncompleteReadError, ValueError)):
            read(struct.pack(">I", 5) + b"\x04ab")

    def test_bitfield_parsing_rejects_spare_bits(self):
        self.assertEqual(wire.parse_bitfield(b"\xa0", 3), [0, 2])
        with self.assertRaises(ValueError):
            wire.parse_bitfield(b"\xa1", 3)

    def test_parse_helpers(self):
        self.assertEqual(wire.parse_request(struct.pack(">III", 1, 2, 3)), (1, 2, 3))
        self.assertEqual(wire.parse_piece(struct.pack(">II", 1, 2) + b"zz"), (1, 2, b"zz"))
        self.assertEqual(wire.parse_have(struct.pack(">I", 9)), 9)


class DecodePrefix(unittest.TestCase):
    def test_returns_the_value_and_where_it_ended(self):
        data = b"d8:msg_typei1e5:piecei0ee" + b"RAW"
        value, end = bencode.decode_prefix(data)
        self.assertEqual(value, {b"msg_type": 1, b"piece": 0})
        self.assertEqual(data[end:], b"RAW")

    def test_without_trailing_data_it_ends_at_the_length(self):
        self.assertEqual(bencode.decode_prefix(b"i5e"), (5, 3))

    def test_is_strict_like_decode(self):
        for data in (b"d1:bi1e1:ai2ee", b"i05e", b"d1:ai1e", b"x", b"", b"d1:a" + b"l" * 100 + b"e"):
            with self.subTest(data=data[:12]):
                with self.assertRaises(bencode.BencodeError):
                    bencode.decode_prefix(data)

    def test_node_budget_applies(self):
        with self.assertRaises(bencode.BencodeError):
            bencode.decode_prefix(b"l" + b"i1e" * 50 + b"e", max_nodes=10)


class Picker(unittest.TestCase):
    PL = 4 * picker.BLOCK  # 4 blocks per piece

    def make(self, npieces=4, total=None, **kw):
        total = total if total is not None else npieces * self.PL
        return picker.Picker(npieces, self.PL, total, rng=random.Random(1), **kw)

    def drain(self, p, pid, n=1000):
        return p.next_requests(pid, n)

    def test_mark_verified_counts_bytes_and_is_never_requested(self):
        p = self.make(npieces=4, total=3 * self.PL + 100)
        p.mark_verified([1, 3])
        self.assertEqual(p.finished, {1, 3})
        self.assertEqual(p.verified_bytes(), self.PL + 100)
        self.assertEqual(p.missing, {0, 2})
        p.peer_bitfield("a", [0, 1, 2, 3])
        asked = {r[0] for r in self.drain(p, "a")}
        self.assertEqual(asked, {0, 2})
        self.assertEqual(p.want["a"], 2)  # only the unverified pieces are of interest

    def test_mark_verified_is_idempotent_and_finishes_the_download(self):
        p = self.make(npieces=2)
        p.mark_verified([0, 1, 1])
        p.mark_verified([0])
        self.assertTrue(p.done())
        self.assertEqual(p.verified_bytes(), 2 * self.PL)

    def test_mark_verified_after_a_peer_announced_the_piece_lowers_its_want(self):
        p = self.make()
        p.peer_bitfield("a", [0, 1])
        p.mark_verified([0])
        self.assertEqual(p.want["a"], 1)
        self.assertFalse(p.interesting("a") and p.next_requests("a", 100)[0][0] == 0)

    def test_mark_verified_rejects_an_index_out_of_range(self):
        with self.assertRaises(ValueError):
            self.make().mark_verified([4])

    def test_nothing_is_requested_from_a_peer_without_pieces(self):
        p = self.make()
        self.assertEqual(p.next_requests("a", 10), [])
        p.peer_bitfield("a", [])
        self.assertEqual(p.next_requests("a", 10), [])

    def test_requests_are_blocks_of_a_piece_the_peer_has(self):
        p = self.make()
        p.peer_has("a", 2)
        reqs = p.next_requests("a", 10)
        self.assertEqual(reqs, [(2, i * picker.BLOCK, picker.BLOCK) for i in range(4)])

    def test_max_n_limits_the_requests(self):
        p = self.make()
        p.peer_has("a", 0)
        self.assertEqual(len(p.next_requests("a", 3)), 3)
        self.assertEqual(len(p.next_requests("a", 3)), 1)  # finishes piece 0 first

    def test_rarest_piece_first(self):
        p = self.make()
        p.peer_bitfield("a", [0, 1, 2])
        p.peer_bitfield("b", [0, 1])
        p.peer_bitfield("c", [0])
        # availability: 0 -> 3, 1 -> 2, 2 -> 1
        self.assertEqual(p.next_requests("a", 1)[0][0], 2)

    def test_ties_are_random_but_always_a_piece_the_peer_has(self):
        seen = set()
        for seed in range(20):
            p = picker.Picker(4, self.PL, 4 * self.PL, rng=random.Random(seed))
            p.peer_bitfield("a", [1, 3])
            seen.add(p.next_requests("a", 1)[0][0])
        self.assertEqual(seen, {1, 3})

    def test_the_last_piece_may_be_short(self):
        total = 3 * self.PL + picker.BLOCK + 5
        p = self.make(npieces=4, total=total)
        p.peer_has("a", 3)
        self.assertEqual(p.next_requests("a", 10), [(3, 0, picker.BLOCK), (3, picker.BLOCK, 5)])

    def test_blocks_requested_from_one_peer_are_not_requested_from_another_before_endgame(self):
        p = self.make()
        p.peer_bitfield("a", [0, 1, 2, 3])
        p.peer_bitfield("b", [0, 1, 2, 3])
        first = set(p.next_requests("a", 8))
        second = set(p.next_requests("b", 8))
        self.assertFalse(first & second)
        self.assertEqual(len(first | second), 16)

    def test_piece_completes_when_all_blocks_arrive(self):
        p = self.make(npieces=1)
        p.peer_has("a", 0)
        reqs = p.next_requests("a", 10)
        data = bytes(range(256)) * (self.PL // 256)
        out = None
        for i, (idx, begin, n) in enumerate(reqs):
            out = p.block_received("a", idx, begin, data[begin:begin + n])
            self.assertEqual(out is None, i < 3)
        self.assertEqual(out, data)
        self.assertFalse(p.done())
        p.piece_done(0)
        self.assertTrue(p.done())
        self.assertEqual(p.verified_bytes(), self.PL)

    def test_unrequested_block_is_refused(self):
        p = self.make()
        p.peer_has("a", 0)
        p.next_requests("a", 1)  # block 0 of piece 0 only
        for idx, begin, n in ((0, picker.BLOCK, picker.BLOCK), (1, 0, picker.BLOCK)):
            with self.assertRaises(picker.BadBlock):
                p.block_received("a", idx, begin, b"x" * n)
        with self.assertRaises(picker.BadBlock):
            p.block_received("zzz", 0, 0, b"x" * picker.BLOCK)

    def test_wrong_block_length_is_refused(self):
        p = self.make()
        p.peer_has("a", 0)
        p.next_requests("a", 1)
        with self.assertRaises(picker.BadBlock):
            p.block_received("a", 0, 0, b"x" * 10)

    def test_a_block_cannot_be_delivered_twice(self):
        p = self.make()
        p.peer_has("a", 0)
        p.next_requests("a", 1)
        p.block_received("a", 0, 0, b"x" * picker.BLOCK)
        with self.assertRaises(picker.BadBlock):
            p.block_received("a", 0, 0, b"x" * picker.BLOCK)

    def test_failed_piece_is_requested_again_and_names_its_contributors(self):
        p = self.make(npieces=1)
        p.peer_bitfield("a", [0])
        p.peer_bitfield("b", [0])
        reqs = p.next_requests("a", 2) + p.next_requests("b", 2)
        for pid, (idx, begin, n) in zip("aabb", reqs):
            p.block_received(pid, idx, begin, b"x" * n)
        self.assertEqual(p.piece_failed(0), {"a", "b"})
        again = p.next_requests("a", 10)
        self.assertEqual(len(again), 4)

    def test_peer_gone_releases_its_blocks_and_availability(self):
        p = self.make(npieces=2)
        p.peer_bitfield("a", [0, 1])
        p.peer_bitfield("b", [0])
        taken = p.next_requests("a", 4)
        self.assertEqual({r[0] for r in taken}, {1})  # piece 1 is the rarer one
        p.peer_gone("a")
        self.assertEqual(p.next_requests("b", 4)[0][0], 0)
        p.peer_has("b", 1)
        self.assertEqual({r[0] for r in p.next_requests("b", 8)}, {1})  # b already owns piece 0's blocks

    def test_choke_release_returns_blocks(self):
        p = self.make(npieces=2)  # piece 1 stays unstarted, so there is no endgame
        p.peer_has("a", 0)
        p.peer_has("b", 0)
        self.assertEqual(len(p.next_requests("a", 4)), 4)
        self.assertEqual(p.next_requests("b", 4), [])
        p.release("a")
        self.assertEqual(p.outstanding("a"), 0)
        self.assertEqual(len(p.next_requests("b", 4)), 4)

    def test_endgame_duplicates_remaining_blocks_and_cancels_them(self):
        p = self.make(npieces=1)
        p.peer_bitfield("a", [0])
        p.peer_bitfield("b", [0])
        a = p.next_requests("a", 4)
        self.assertEqual(p.next_requests("b", 4), [r for r in a])  # every block already requested: endgame
        idx, begin, n = a[0]
        p.block_received("a", idx, begin, b"x" * n)
        self.assertEqual(p.drain_cancels(), [("b", idx, begin, n)])
        self.assertEqual(p.drain_cancels(), [])
        # b's late copy of the cancelled block is dropped quietly, not an error
        self.assertIsNone(p.block_received("b", idx, begin, b"x" * n))

    def test_endgame_does_not_repeat_a_request_to_the_same_peer(self):
        p = self.make(npieces=1)
        p.peer_has("a", 0)
        self.assertEqual(len(p.next_requests("a", 10)), 4)
        self.assertEqual(p.next_requests("a", 10), [])

    def test_no_endgame_while_unrequested_pieces_remain(self):
        p = self.make(npieces=2)
        p.peer_bitfield("a", [0])
        p.peer_bitfield("b", [0, 1])
        p.next_requests("a", 10)
        # piece 1 has not been started yet, so b takes it instead of duplicating a's piece 0
        self.assertEqual({r[0] for r in p.next_requests("b", 4)}, {1})

    def test_in_flight_pieces_are_capped(self):
        p = self.make(npieces=10, max_active=2)
        p.peer_bitfield("a", range(10))
        reqs = p.next_requests("a", 1000)
        self.assertEqual(len({r[0] for r in reqs}), 2)

    def test_have_for_an_unknown_piece_is_refused(self):
        p = self.make(npieces=4)
        with self.assertRaises(ValueError):
            p.peer_has("a", 4)
        with self.assertRaises(ValueError):
            p.peer_bitfield("a", [9])

    def test_interesting_follows_what_we_still_need(self):
        p = self.make(npieces=2)
        p.peer_has("a", 0)
        self.assertTrue(p.interesting("a"))
        p.next_requests("a", 4)
        for idx, begin, n in [(0, i * picker.BLOCK, picker.BLOCK) for i in range(4)]:
            p.block_received("a", idx, begin, b"x" * n)
        p.piece_done(0)
        self.assertFalse(p.interesting("a"))
        self.assertFalse(p.interesting("nobody"))


    def test_opening_pieces_stays_fast_with_a_million_pieces(self):
        import statistics
        import time
        n = 1 << 20
        p = picker.Picker(n, picker.BLOCK, n * picker.BLOCK, rng=random.Random(1))
        p.peer_bitfield("seed", range(n))
        p.peer_bitfield("half", range(0, n, 2))
        times = []
        for _ in range(5):
            start = time.perf_counter()
            reqs = p.next_requests("half", 16)  # one block per piece: opens 16 pieces
            times.append(time.perf_counter() - start)
            self.assertEqual(len(reqs), 16)
        self.assertLess(statistics.median(times), 0.1)
        start = time.perf_counter()
        for _ in range(100):
            p.interesting("half")
        self.assertLess(time.perf_counter() - start, 0.1)

    def test_availability_changes_move_pieces_between_rarity_classes(self):
        p = self.make()
        p.peer_bitfield("a", [0, 1, 2])
        p.peer_bitfield("b", [0, 1, 2])
        p.peer_has("c", 1)
        p.peer_gone("b")
        p.peer_has("d", 0)
        p.peer_has("d", 2)
        # availability: 0 -> 2, 1 -> 2, 2 -> 2; then c leaves: 1 -> 1
        p.peer_gone("c")
        self.assertEqual(p.next_requests("a", 1)[0][0], 1)

    def test_a_completed_piece_hands_over_its_buffer(self):
        p = self.make(npieces=1)
        p.peer_has("a", 0)
        reqs = p.next_requests("a", 10)
        data = None
        for idx, begin, n in reqs:
            data = p.block_received("a", idx, begin, b"x" * n) or data
        self.assertEqual(bytes(data), b"x" * self.PL)
        self.assertIsNone(p.active[0].buf)  # no second copy is kept while the hash is checked
        p.piece_done(0)

    def test_interesting_follows_finished_pieces_and_peers(self):
        p = self.make()
        p.peer_bitfield("a", [0, 1])
        self.assertTrue(p.interesting("a"))
        for i in (0, 1):
            p.next_requests("a", 1000)
            p.piece_done(i)
        self.assertFalse(p.interesting("a"))
        p.peer_has("a", 2)
        self.assertTrue(p.interesting("a"))
        p.peer_gone("a")
        self.assertFalse(p.interesting("a"))


class Tracker(unittest.TestCase):
    """AC3: announces against the fixture tracker (HTTP and UDP) running on a background loop."""

    @classmethod
    def setUpClass(cls):
        cls.bg = torrentkit.Background()
        cls.addClassCleanup(cls.bg.close)
        cls.fix = torrentkit.load_fixture("tracker")

    def serve(self, host="127.0.0.1", **kw):
        t = self.fix.Tracker(**kw)
        try:
            self.bg.call(t.start(host))
        except OSError:
            self.skipTest(f"cannot bind {host}")
        self.addCleanup(self.bg.call, t.stop())
        return t

    def req(self, event="started", left=1000):
        return tracker.Announce(IH, PID, 6881, 0, 0, left, event)

    def announce(self, url, **kw):
        return asyncio.run(tracker.announce(url, self.req(**kw), timeout=5))

    def test_http_compact_peers(self):
        t = self.serve(peers=[("10.1.2.3", 6881), ("10.1.2.4", 51413)])
        res = self.announce(f"http://127.0.0.1:{t.http_port}/announce")
        self.assertEqual(res.peers, [("10.1.2.3", 6881), ("10.1.2.4", 51413)])
        self.assertEqual(res.interval, 60)
        sent = t.announces[0]
        self.assertEqual((sent["info_hash"], sent["peer_id"], sent["event"], sent["left"], sent["port"]),
                         (IH, PID, "started", 1000, 6881))
        self.assertTrue(sent["compact"])

    def test_http_dictionary_peers(self):
        t = self.serve(mode="dict", peers=[("10.1.2.3", 6881)])
        res = self.announce(f"http://127.0.0.1:{t.http_port}/announce")
        self.assertEqual(res.peers, [("10.1.2.3", 6881)])

    def test_http_empty_peer_list(self):
        t = self.serve(mode="empty")
        self.assertEqual(self.announce(f"http://127.0.0.1:{t.http_port}/announce").peers, [])

    def test_http_failure_reason_is_the_error_and_never_the_url(self):
        t = self.serve(mode="fail")
        with self.assertRaises(tracker.TrackerError) as cm:
            self.announce(f"http://127.0.0.1:{t.http_port}/announce?passkey=SECRET")
        self.assertEqual(str(cm.exception), "tracker says no")

    def test_http_unreachable_error_hides_url_and_query(self):
        port = torrentkit.free_port()
        with self.assertRaises(tracker.TrackerError) as cm:
            self.announce(f"http://user:pw@127.0.0.1:{port}/announce?passkey=SECRET")
        for secret in ("SECRET", "user", "pw", "127.0.0.1"):
            self.assertNotIn(secret, str(cm.exception))

    def test_passkey_in_the_url_is_kept_and_joined_with_an_ampersand(self):
        t = self.serve(peers=[])
        self.announce(f"http://127.0.0.1:{t.http_port}/announce?passkey=SECRET")
        self.assertIn("passkey=SECRET&", t.announces[0]["query"] + "&")
        self.assertIn("info_hash=", t.announces[0]["query"])

    def test_events_are_sent_and_empty_event_is_omitted(self):
        t = self.serve(peers=[])
        for event in ("completed", "stopped", ""):
            self.announce(f"http://127.0.0.1:{t.http_port}/announce", event=event)
        self.assertEqual([a["event"] for a in t.announces], ["completed", "stopped", ""])
        self.assertNotIn("event=", t.announces[2]["query"])

    def test_oversized_reply_is_refused(self):
        async def handler(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.0 200 OK\r\n\r\n" + b"d" * (tracker.MAX_REPLY + 10))
            await writer.drain()
            writer.close()

        server = self.bg.call(asyncio.start_server(handler, "127.0.0.1", 0))
        self.addCleanup(server.close)
        port = server.sockets[0].getsockname()[1]
        with self.assertRaises(tracker.TrackerError) as cm:
            self.announce(f"http://127.0.0.1:{port}/announce")
        self.assertIn("too large", str(cm.exception))

    def test_garbage_reply_is_a_tracker_error(self):
        async def handler(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.0 200 OK\r\n\r\nnot bencode")
            await writer.drain()
            writer.close()

        server = self.bg.call(asyncio.start_server(handler, "127.0.0.1", 0))
        self.addCleanup(server.close)
        port = server.sockets[0].getsockname()[1]
        with self.assertRaises(tracker.TrackerError):
            self.announce(f"http://127.0.0.1:{port}/announce")

    def test_udp_peers(self):
        t = self.serve(peers=[("10.9.8.7", 4000)])
        res = self.announce(f"udp://127.0.0.1:{t.udp_port}/announce", left=77)
        self.assertEqual(res.peers, [("10.9.8.7", 4000)])
        self.assertEqual(res.interval, 60)
        sent = t.announces[0]
        self.assertEqual((sent["proto"], sent["info_hash"], sent["peer_id"], sent["event"], sent["left"],
                          sent["port"]), ("udp", IH, PID, "started", 77, 6881))

    def test_udp_over_ipv6_reads_18_byte_peer_records(self):
        t = self.serve("::1", peers=[("2001:db8::1", 5000)])
        res = self.announce(f"udp://[::1]:{t.udp_port}/announce")
        self.assertEqual(res.peers, [("2001:db8::1", 5000)])

    def test_udp_localhost_reaches_a_tracker_listening_only_on_ipv6(self):
        t = self.serve("::1", peers=[("2001:db8::2", 5001)])
        res = self.announce(f"udp://localhost:{t.udp_port}/announce")  # tries 127.0.0.1 first: refused
        self.assertEqual(res.peers, [("2001:db8::2", 5001)])

    def test_udp_localhost_reaches_a_tracker_listening_only_on_ipv4(self):
        t = self.serve(peers=[("10.9.8.7", 4000)])
        res = self.announce(f"udp://localhost:{t.udp_port}/announce")
        self.assertEqual(res.peers, [("10.9.8.7", 4000)])

    def test_udp_budget_caps_the_retry_schedule(self):
        import time
        t = self.serve(drop=1000)
        url = f"udp://127.0.0.1:{t.udp_port}/announce"
        start = time.monotonic()
        with self.assertRaises(tracker.TrackerError) as cm:  # default UDP_TIMEOUT alone would take minutes
            asyncio.run(asyncio.wait_for(tracker.announce(url, self.req(), timeout=0.5), 5))
        self.assertEqual(str(cm.exception), "timed out")
        self.assertLess(time.monotonic() - start, 4)

    def drip_server(self, seconds=6):
        async def handler(reader, writer):
            try:
                await reader.readuntil(b"\r\n\r\n")
                writer.write(b"HTTP/1.0 200 OK\r\nContent-Length: 100000\r\n\r\n")
                end = asyncio.get_running_loop().time() + seconds
                while asyncio.get_running_loop().time() < end:
                    writer.write(b"d")
                    await writer.drain()
                    await asyncio.sleep(0.05)
            except (ConnectionError, asyncio.IncompleteReadError):
                pass
            finally:
                writer.close()

        server = self.bg.call(asyncio.start_server(handler, "127.0.0.1", 0))
        self.addCleanup(server.close)
        return f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/announce"

    def tracker_threads(self):
        import threading
        return [t for t in threading.enumerate() if t.name == "dm-tracker"]

    def wait_no_threads(self, seconds=3):
        import time
        end = time.monotonic() + seconds
        while self.tracker_threads() and time.monotonic() < end:
            time.sleep(0.02)
        return not self.tracker_threads()

    def test_a_dripping_http_tracker_hits_the_overall_deadline(self):
        import time
        url = self.drip_server()
        start = time.monotonic()
        with self.assertRaises(tracker.TrackerError):
            asyncio.run(tracker.announce(url, self.req(), timeout=0.6))
        self.assertLess(time.monotonic() - start, 3)
        self.assertTrue(self.wait_no_threads(), "the reader thread outlived the announce")

    def test_cancelling_an_http_announce_closes_its_connection(self):
        url = self.drip_server()

        async def go():
            task = asyncio.ensure_future(tracker.announce(url, self.req(), timeout=30))
            await asyncio.sleep(0.4)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        asyncio.run(go())
        self.assertTrue(self.wait_no_threads(), "the reader thread outlived the cancel")

    def test_udp_survives_two_dropped_packets(self):
        t = self.serve(peers=[("10.9.8.7", 4000)], drop=2)
        with mock.patch.object(tracker, "UDP_TIMEOUT", 0.1):
            res = self.announce(f"udp://127.0.0.1:{t.udp_port}/announce")
        self.assertEqual(res.peers, [("10.9.8.7", 4000)])

    def test_udp_gives_up_after_the_retries(self):
        t = self.serve(drop=1000)
        with mock.patch.object(tracker, "UDP_TIMEOUT", 0.02), mock.patch.object(tracker, "UDP_RETRIES", 2):
            with self.assertRaises(tracker.TrackerError) as cm:
                self.announce(f"udp://127.0.0.1:{t.udp_port}/announce")
        self.assertEqual(str(cm.exception), "timed out")
        self.assertEqual(t.datagrams, 3)  # the first try and two retries

    def test_udp_error_message(self):
        t = self.serve(mode="fail")
        with self.assertRaises(tracker.TrackerError) as cm:
            self.announce(f"udp://127.0.0.1:{t.udp_port}/announce")
        self.assertEqual(str(cm.exception), "tracker says no")

    def test_unsupported_scheme(self):
        with self.assertRaises(tracker.TrackerError):
            self.announce("wss://example.invalid/announce")


class NoListen(unittest.TestCase):
    """AC5 (the static half): the plugin never opens a server socket."""

    def test_the_torrent_package_never_listens(self):
        import ast

        banned = {"start_server", "create_server", "bind", "listen", "start_unix_server"}
        found = []
        for path in sorted((ROOT / "plugin" / "dm" / "dmcore").rglob("*.py")):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Attribute) and node.attr in banned or \
                        isinstance(node, ast.Name) and node.id in banned:
                    found.append(f"{path.name}:{node.lineno}")
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
