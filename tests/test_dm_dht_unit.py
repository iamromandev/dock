"""Unit tests for the DHT client of dm: KRPC codec, routing table, lookup and node cache (no network)."""

import os
import random
import socket
import stat
import struct
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "plugin" / "dm"))
sys.path.insert(0, str(ROOT / "tests"))

from dmcore.torrent import bencode  # noqa: E402
from dmcore.torrent.dht import cache, krpc, lookup, table  # noqa: E402

NID = b"abcdefghij0123456789"
OTHER = b"mnopqrstuvwxyz123456"


def node_bytes(nid, ip, port):
    return nid + socket.inet_aton(ip) + struct.pack(">H", port)


def reply(**r):
    r.setdefault("id", NID)
    return bencode.encode({"t": b"aa", "y": b"r", "r": r})


class Usable(unittest.TestCase):
    PRIVATE = (("10.1.2.3", 1), ("10.255.255.255", 80), ("172.16.0.1", 5000), ("172.31.255.255", 5000),
               ("192.168.0.5", 65535), ("169.254.169.254", 80), ("100.64.0.1", 80), ("100.127.255.255", 80),
               ("240.0.0.1", 80), ("254.1.2.3", 80), ("198.18.0.1", 80), ("198.19.255.255", 80))

    def test_ordinary_addresses_are_usable(self):
        for addr in (("8.8.8.8", 6881), ("1.2.3.4", 1), ("9.255.255.255", 65535), ("11.0.0.1", 5000),
                     ("172.15.255.255", 80), ("172.32.0.1", 80), ("192.167.1.1", 80), ("192.169.1.1", 80),
                     ("169.253.1.1", 80), ("169.255.1.1", 80), ("100.63.255.255", 80), ("100.128.0.1", 80),
                     ("198.17.255.255", 80), ("198.20.0.1", 80), ("223.255.255.254", 80),
                     ("203.0.113.7", 80)):
            self.assertTrue(krpc.usable(addr), addr)

    def test_private_addresses_are_unusable_by_default(self):
        for addr in self.PRIVATE:
            self.assertFalse(krpc.usable(addr), addr)
            self.assertFalse(krpc.usable(addr, allow_loopback=True), addr)

    def test_private_addresses_pass_with_allow_private(self):
        for addr in self.PRIVATE:
            self.assertTrue(krpc.usable(addr, allow_private=True), addr)

    def test_unusable_addresses(self):
        for addr in (("0.0.0.0", 6881), ("0.1.2.3", 6881), ("255.255.255.255", 6881), ("224.0.0.1", 6881),
                     ("239.255.255.250", 6881), ("127.0.0.1", 6881), ("8.8.8.8", 0), ("8.8.8.8", 65536),
                     ("::1", 6881), ("not an ip", 6881), ("8.8.8.8", "80"), ("8.8.8.8",), None,
                     ("1.2.3", 80), ("1.2.3.4.5", 80), ("1.2.3.256", 80), ("1.2.3.04", 80), ("1.2.3.-4", 80),
                     ("1.2.3.+4", 80), (" 1.2.3.4", 80), ("1.2.3.4 ", 80), ("", 80), ("1..3.4", 80)):
            self.assertFalse(krpc.usable(addr), addr)

    def test_broadcast_multicast_and_zero_net_are_never_allowed(self):
        for addr in (("0.0.0.0", 6881), ("0.1.2.3", 6881), ("255.255.255.255", 80), ("224.0.0.1", 80),
                     ("239.255.255.250", 80)):
            self.assertFalse(krpc.usable(addr, allow_loopback=True, allow_private=True), addr)

    def test_loopback_is_allowed_only_on_request(self):
        self.assertFalse(krpc.usable(("127.0.0.1", 6881)))
        self.assertFalse(krpc.usable(("127.0.0.1", 6881), allow_private=True))
        self.assertTrue(krpc.usable(("127.0.0.1", 6881), allow_loopback=True))
        self.assertFalse(krpc.usable(("0.0.0.0", 6881), allow_loopback=True))

    def test_parse_drops_private_nodes_and_values_unless_allowed(self):
        nodes = (b"".join(node_bytes(NID, ip, port) for ip, port in self.PRIVATE)
                 + node_bytes(OTHER, "4.3.2.1", 99))
        values = [node_bytes(b"", ip, port) for ip, port in self.PRIVATE]
        data = reply(nodes=nodes[:16 * 26], values=values)
        r = krpc.parse(data)
        self.assertEqual((r.nodes, r.values), ([(OTHER, ("4.3.2.1", 99))], []))
        r = krpc.parse(data, allow_private=True)
        self.assertEqual((len(r.nodes), len(r.values)), (13, 12))


class Encode(unittest.TestCase):
    def test_a_query_carries_ro_and_is_bencoded(self):
        data = krpc.query(b"aa", "get_peers", {"id": NID, "info_hash": OTHER})
        msg, _ = bencode.decode(data)
        self.assertEqual(msg, {b"t": b"aa", b"y": b"q", b"q": b"get_peers", b"ro": 1,
                               b"a": {b"id": NID, b"info_hash": OTHER}})

    def test_the_bep5_ping_example_round_trips_as_a_query(self):
        data = b"d1:ad2:id20:abcdefghij0123456789e1:q4:ping1:t2:aa1:y1:qe"
        q = krpc.parse(data)
        self.assertIsInstance(q, krpc.Query)
        self.assertEqual((q.tid, q.method), (b"aa", "ping"))


class ParseReplies(unittest.TestCase):
    def test_the_bep5_ping_reply(self):
        r = krpc.parse(b"d1:rd2:id20:mnopqrstuvwxyz123456e1:t2:aa1:y1:re")
        self.assertIsInstance(r, krpc.Reply)
        self.assertEqual((r.tid, r.id, r.nodes, r.values, r.token), (b"aa", OTHER, [], [], None))

    def test_the_bep5_get_peers_reply_with_values(self):
        data = (b"d1:rd2:id20:abcdefghij01234567895:token8:aoeusnth6:valuesl6:axje.u6:idhtnmee"
                b"1:t2:aa1:y1:re")
        r = krpc.parse(data)
        self.assertEqual(r.token, b"aoeusnth")
        self.assertEqual(r.values, [("97.120.106.101", 11893), ("105.100.104.116", 28269)])

    def test_nodes_are_split_into_id_and_address(self):
        nodes = node_bytes(OTHER, "1.2.3.4", 6881) + node_bytes(NID, "5.6.7.8", 80)
        r = krpc.parse(reply(nodes=nodes))
        self.assertEqual(r.nodes, [(OTHER, ("1.2.3.4", 6881)), (NID, ("5.6.7.8", 80))])

    def test_a_reply_adds_at_most_16_nodes(self):
        nodes = b"".join(node_bytes(bytes([i]) * 20, "1.2.3.%d" % (i + 1), 1000 + i) for i in range(50))
        self.assertEqual(len(krpc.parse(reply(nodes=nodes)).nodes), krpc.MAX_NODES)

    def test_a_reply_adds_at_most_100_values(self):
        values = [socket.inet_aton("9.9.%d.%d" % (i // 250, i % 250 + 1)) + struct.pack(">H", 2000)
                  for i in range(150)]
        self.assertEqual(len(krpc.parse(reply(values=values)).values), krpc.MAX_VALUES)

    def test_unusable_nodes_and_values_are_skipped(self):
        nodes = (node_bytes(OTHER, "0.0.0.0", 6881) + node_bytes(OTHER, "1.2.3.4", 0)
                 + node_bytes(OTHER, "224.0.0.9", 6881) + node_bytes(OTHER, "127.0.0.1", 6881)
                 + node_bytes(NID, "1.2.3.4", 6881))
        values = [node_bytes(b"", "255.255.255.255", 80), node_bytes(b"", "1.2.3.4", 0),
                  node_bytes(b"", "4.3.2.1", 99), b"short"]
        r = krpc.parse(reply(nodes=nodes, values=values))
        self.assertEqual(r.nodes, [(NID, ("1.2.3.4", 6881))])
        self.assertEqual(r.values, [("4.3.2.1", 99)])

    def test_loopback_passes_with_the_flag(self):
        r = krpc.parse(reply(nodes=node_bytes(NID, "127.0.0.1", 7000)), allow_loopback=True)
        self.assertEqual(r.nodes, [(NID, ("127.0.0.1", 7000))])

    def test_unknown_keys_are_ignored(self):
        data = bencode.encode({"t": b"aa", "y": b"r", "v": b"XX01", "ip": b"abcdef",
                               "r": {"id": NID, "x": 1}})
        self.assertIsInstance(krpc.parse(data), krpc.Reply)

    def test_a_token_is_kept_up_to_64_bytes(self):
        self.assertEqual(krpc.parse(reply(token=b"t" * 64)).token, b"t" * 64)

    def test_an_error_message_is_parsed(self):
        e = krpc.parse(b"d1:eli201e23:A Generic Error Ocurrede1:t2:aa1:y1:ee")
        self.assertIsInstance(e, krpc.ErrorMsg)
        self.assertEqual(e.tid, b"aa")


class ParseStages(unittest.TestCase):
    def test_the_header_reads_the_transaction_id_without_checking_nodes_or_values(self):
        nodes = b"x" * 27  # a bad body: the header stage must not look at it
        head = krpc.parse_header(reply(nodes=nodes))
        self.assertEqual((head.tid, head.kind), (b"aa", b"r"))
        with self.assertRaises(krpc.KrpcError):
            krpc.parse_body(head)

    def test_the_body_stage_gives_the_same_result_as_parse(self):
        data = reply(nodes=node_bytes(OTHER, "1.2.3.4", 6881), values=[node_bytes(b"", "4.3.2.1", 99)],
                     token=b"tk")
        self.assertEqual(krpc.parse_body(krpc.parse_header(data)), krpc.parse(data))

    def test_the_header_rejects_what_parse_rejects_early(self):
        for data in (b"", b"x" * 1501, bencode.encode([1]), bencode.encode({"y": b"r"}),
                     bencode.encode({"t": b"", "y": b"r"}), bencode.encode({"t": b"aa", "y": b"z"})):
            with self.assertRaises(krpc.KrpcError):
                krpc.parse_header(data)

    def test_the_body_stage_of_a_query_and_an_error(self):
        q = krpc.parse_body(krpc.parse_header(b"d1:ad2:id20:" + NID + b"e1:q4:ping1:t2:aa1:y1:qe"))
        self.assertEqual((q.tid, q.method), (b"aa", "ping"))
        e = krpc.parse_body(krpc.parse_header(b"d1:eli201e1:xe1:t2:aa1:y1:ee"))
        self.assertIsInstance(e, krpc.ErrorMsg)


class ParseHostile(unittest.TestCase):
    def bad(self, data, **kw):
        with self.assertRaises(krpc.KrpcError, msg=data[:60]):
            krpc.parse(data, **kw)

    def test_oversize_datagram(self):
        good = reply()
        self.bad(good + b"\x00" * (krpc.MAX_DATAGRAM - len(good) + 1))
        padded = bencode.encode({"t": b"aa", "y": b"r", "r": {"id": NID}, "zz": b"x" * 1300})
        self.assertLessEqual(len(padded), krpc.MAX_DATAGRAM)
        krpc.parse(padded)

    def test_not_bencode(self):
        for data in (b"", b"garbage", b"\xff" * 40, b"d", b"i1e", b"le", b"de", b"0:"):
            self.bad(data)

    def test_trailing_data(self):
        self.bad(reply() + b"x")

    def test_unsorted_keys_are_accepted_but_duplicate_keys_are_not(self):
        r = krpc.parse(b"d1:y1:r1:t2:aa1:rd2:id20:" + NID + b"ee")
        self.assertEqual((r.tid, r.id), (b"aa", NID))
        self.bad(b"d1:t2:aa1:t2:bb1:y1:r1:rd2:id20:" + NID + b"ee")
        self.bad(b"d1:t2:aa1:y1:r1:rd2:id20:" + NID + b"2:id20:" + NID + b"ee")
        self.bad(b"d1:t2:aa1:y1:r1:t2:bb1:rd2:id20:" + NID + b"ee")

    def test_deep_nesting(self):
        deep = b"l" * 100 + b"e" * 100
        self.bad(b"d1:rd2:id20:" + NID + b"1:zl" + deep + b"ee1:t2:aa1:y1:re")
        self.bad(bencode.encode({"t": b"aa", "y": b"r", "r": {"id": NID, "z": [[[[[[[[1]]]]]]]]}}))

    def test_too_many_items(self):
        self.bad(bencode.encode({"t": b"aa", "y": b"r", "r": {"id": NID, "z": list(range(2100))}}))

    def test_top_level_must_be_a_dict(self):
        self.bad(bencode.encode([b"t", b"y"]))
        self.bad(bencode.encode(5))

    def test_transaction_id_and_type(self):
        self.bad(bencode.encode({"y": b"r", "r": {"id": NID}}))
        self.bad(bencode.encode({"t": 5, "y": b"r", "r": {"id": NID}}))
        self.bad(bencode.encode({"t": b"123456789", "y": b"r", "r": {"id": NID}}))
        self.bad(bencode.encode({"t": b"aa", "r": {"id": NID}}))
        self.bad(bencode.encode({"t": b"aa", "y": b"x", "r": {"id": NID}}))
        self.bad(bencode.encode({"t": b"aa", "y": 1, "r": {"id": NID}}))

    def test_reply_body(self):
        for r in (None, 5, [NID], {}, {"id": b"short"}, {"id": 7}, {"id": NID + b"x"}):
            body = {"t": b"aa", "y": b"r"}
            if r is not None:
                body["r"] = r
            self.bad(bencode.encode(body))

    def test_bad_nodes(self):
        self.bad(reply(nodes=b"x" * 27))
        self.bad(reply(nodes=b"x" * 25))
        self.bad(reply(nodes=5))
        self.bad(reply(nodes=[b"x" * 26]))

    def test_bad_values(self):
        self.bad(reply(values=b"abcdef"))
        self.bad(reply(values=5))
        self.bad(reply(values=[5]))
        self.bad(reply(values=[[b"abcdef"]]))

    def test_bad_token(self):
        self.bad(reply(token=b"t" * 65))
        self.bad(reply(token=5))

    def test_a_query_without_a_method_or_arguments_is_rejected(self):
        self.bad(bencode.encode({"t": b"aa", "y": b"q"}))
        self.bad(bencode.encode({"t": b"aa", "y": b"q", "q": 5}))

    def test_an_error_message_needs_a_list(self):
        self.bad(bencode.encode({"t": b"aa", "y": b"e", "e": b"x"}))


def nid(n):
    """A 20-byte id whose integer value is n."""
    return n.to_bytes(20, "big")


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class Table(unittest.TestCase):
    OWN = nid(0)

    def make(self):
        return table.RoutingTable(self.OWN, clock=FakeClock())

    def test_a_node_that_answered_is_kept_and_found(self):
        t = self.make()
        t.seen(nid(5), ("1.1.1.1", 1))
        self.assertEqual(len(t), 1)
        self.assertEqual(t.closest(nid(5)), [krpc.Node(nid(5), ("1.1.1.1", 1))])

    def test_closest_is_ordered_by_xor_distance_and_limited(self):
        t = self.make()
        for i in range(1, 30):
            t.seen(nid(i << 8), ("1.1.1.%d" % i, 1000 + i))
        target = nid(9 << 8)
        got = t.closest(target, 5)
        self.assertEqual(len(got), 5)
        dist = [int.from_bytes(n.id, "big") ^ int.from_bytes(target, "big") for n in got]
        self.assertEqual(dist, sorted(dist))
        self.assertEqual(got[0].id, target)
        self.assertEqual(len(t.closest(target)), table.K)

    def test_a_bucket_holds_k_nodes(self):
        t = self.make()
        for i in range(table.K + 5):  # all share the top bit set: the same bucket
            t.seen(nid((1 << 159) + i), ("2.2.2.%d" % i, 2000 + i))
        self.assertEqual(len(t), table.K)

    def test_the_table_is_capped(self):
        t = self.make()
        rng = random.Random(1)
        for i in range(2000):  # ids spread over every bucket: 160 * k > MAX_TABLE
            bits = 1 + i % 159
            t.seen(((1 << bits) | rng.getrandbits(bits)).to_bytes(20, "big"),
                   ("3.%d.%d.1" % (i // 250, i % 250), 3000))
        self.assertEqual(len(t), table.MAX_TABLE)

    def test_our_own_id_is_ignored(self):
        t = self.make()
        self.assertFalse(t.seen(self.OWN, ("4.4.4.4", 4)))
        self.assertEqual(len(t), 0)

    def test_seen_says_whether_the_node_was_accepted(self):
        t = self.make()
        self.assertTrue(t.seen(nid(7), ("5.5.5.5", 5)))
        self.assertTrue(t.seen(nid(7), ("5.5.5.5", 5)))  # the same node again
        self.assertFalse(t.seen(nid(8), ("5.5.5.5", 5)))  # an address that changes its id
        self.assertFalse(t.seen(nid(7), ("6.6.6.6", 6)))  # an id taken from another address
        self.assertFalse(t.seen(b"short", ("7.7.7.7", 7)))
        self.assertFalse(t.seen(None, ("7.7.7.7", 7)))

    def test_a_valid_node_that_finds_its_bucket_full_still_counts_as_accepted(self):
        t = self.make()
        base = 1 << 159
        for i in range(table.K):
            self.assertTrue(t.seen(nid(base + i), ("8.8.%d.1" % i, 80)))
        self.assertTrue(t.seen(nid(base + 99), ("8.9.9.1", 80)))
        self.assertEqual(len(t), table.K)

    def test_an_address_that_changes_its_id_keeps_the_first(self):
        t = self.make()
        t.seen(nid(7), ("5.5.5.5", 5))
        t.seen(nid(8), ("5.5.5.5", 5))
        self.assertEqual([n.id for n in t.closest(nid(0))], [nid(7)])

    def test_an_id_seen_from_a_second_address_keeps_the_first(self):
        t = self.make()
        t.seen(nid(7), ("5.5.5.5", 5))
        t.seen(nid(7), ("6.6.6.6", 6))
        self.assertEqual([n.addr for n in t.closest(nid(0))], [("5.5.5.5", 5)])

    def test_a_full_bucket_drops_newcomers_but_replaces_a_node_that_failed_twice(self):
        t = self.make()
        base = 1 << 159
        for i in range(table.K):
            t.seen(nid(base + i), ("7.7.7.%d" % i, 7000))
        t.seen(nid(base + 100), ("7.7.8.1", 7000))
        self.assertNotIn(nid(base + 100), [n.id for n in t.closest(nid(base), 50)])
        t.failed(("7.7.7.3", 7000))
        t.seen(nid(base + 100), ("7.7.8.1", 7000))
        self.assertNotIn(nid(base + 100), [n.id for n in t.closest(nid(base), 50)])
        t.failed(("7.7.7.3", 7000))
        t.seen(nid(base + 100), ("7.7.8.1", 7000))
        ids = [n.id for n in t.closest(nid(base), 50)]
        self.assertIn(nid(base + 100), ids)
        self.assertNotIn(nid(base + 3), ids)
        self.assertEqual(len(ids), table.K)

    def test_an_answer_clears_the_failures(self):
        t = self.make()
        base = 1 << 159
        for i in range(table.K):
            t.seen(nid(base + i), ("7.7.7.%d" % i, 7000))
        t.failed(("7.7.7.3", 7000))
        t.failed(("7.7.7.3", 7000))
        t.seen(nid(base + 3), ("7.7.7.3", 7000))
        t.seen(nid(base + 100), ("7.7.8.1", 7000))
        self.assertIn(nid(base + 3), [n.id for n in t.closest(nid(base), 50)])

    def test_bad_ids_are_ignored(self):
        t = self.make()
        t.seen(b"short", ("1.1.1.1", 1))
        t.seen(None, ("1.1.1.1", 1))
        self.assertEqual(len(t), 0)

    def test_recent_lists_the_latest_answers_first(self):
        clock = FakeClock()
        t = table.RoutingTable(self.OWN, clock=clock)
        for i in range(1, 6):
            clock.t = i
            t.seen(nid(i << 100), ("8.8.8.%d" % i, 8000))
        self.assertEqual([n.addr[0] for n in t.recent(3)], ["8.8.8.5", "8.8.8.4", "8.8.8.3"])


def dist(a, b):
    return int.from_bytes(a, "big") ^ int.from_bytes(b, "big")


class SimNetwork:
    """500 nodes. Each knows its 8 nearest and 30 random others, and answers with the 8 nearest it knows."""

    def __init__(self, n=500, seed=7):
        rng = random.Random(seed)
        self.nodes = {}
        ids = [rng.randbytes(20) for _ in range(n)]
        for i, node_id in enumerate(ids):
            self.nodes[("10.%d.%d.1" % (i // 250, i % 250), 6881)] = node_id
        self.by_id = {v: k for k, v in self.nodes.items()}
        self.known = {}
        for addr, node_id in self.nodes.items():
            near = sorted(ids, key=lambda x: dist(x, node_id))[1:9]
            self.known[addr] = set(near) | set(rng.sample(ids, 30))
        self.sent = []

    def node(self, addr):
        return krpc.Node(self.nodes[addr], addr)

    def answer(self, addr, target, holder=None):
        self.sent.append(addr)
        known = sorted(self.known[addr], key=lambda x: dist(x, target))[:8]
        nodes = [(i, self.by_id[i]) for i in known]
        if addr == holder:
            return krpc.Reply(b"tt", self.nodes[addr], [], [("9.9.9.9", 9999)], b"tok")
        return krpc.Reply(b"tt", self.nodes[addr], nodes, [], b"tok")


def drive(lk, answer, now=0.0):
    """Run a lookup to its end; answer(node) is a Reply, or None for a timeout. Returns peers found."""
    found = []
    for _ in range(10000):
        queries = lk.next_queries(now)
        if not queries and lk.done():
            return found
        assert queries or lk.inflight, "stuck"
        for node in queries:
            reply = answer(node)
            if reply is None:
                lk.on_timeout(node)
            else:
                found += lk.on_reply(node, reply, now)
    raise AssertionError("did not end")


class Lookups(unittest.TestCase):
    def test_it_converges_on_a_simulated_network_and_finds_the_values(self):
        net = SimNetwork()
        target = random.Random(3).randbytes(20)
        holder = min(net.nodes, key=lambda a: dist(net.nodes[a], target))
        seeds = [net.node(a) for a in random.Random(4).sample(sorted(net.nodes), 3)]
        lk = lookup.Lookup(target, seeds, want_peers=True)
        found = drive(lk, lambda n: net.answer(n.addr, target, holder))
        self.assertEqual(found, [("9.9.9.9", 9999)])
        self.assertLessEqual(len(net.sent), 60)
        self.assertEqual(len(net.sent), len(set(net.sent)))

    def test_it_ends_when_the_closest_nodes_have_answered(self):
        net = SimNetwork()
        target = random.Random(5).randbytes(20)
        lk = lookup.Lookup(target, [net.node(sorted(net.nodes)[0])], want_peers=True)
        drive(lk, lambda n: net.answer(n.addr, target))
        true_closest = sorted(net.nodes.values(), key=lambda x: dist(x, target))[:8]
        answered = {net.nodes[a] for a in net.sent}
        self.assertGreaterEqual(len(answered & set(true_closest)), 6)

    def test_at_most_alpha_queries_are_in_flight(self):
        net = SimNetwork()
        seeds = [net.node(a) for a in sorted(net.nodes)[:30]]
        lk = lookup.Lookup(b"\x01" * 20, seeds, want_peers=True)
        first = lk.next_queries(0)
        self.assertEqual(len(first), lookup.ALPHA)
        self.assertEqual(lk.next_queries(0), [])
        lk.on_timeout(first[0])
        self.assertEqual(len(lk.next_queries(0)), 1)

    def test_queries_go_to_the_closest_candidates_first(self):
        target = nid(1 << 100)
        seeds = [krpc.Node(nid(i << 10), ("1.1.1.%d" % i, 1000)) for i in range(1, 20)]
        lk = lookup.Lookup(target, seeds, want_peers=True)
        got = lk.next_queries(0)
        want = sorted(seeds, key=lambda n: dist(n.id, target))[:lookup.ALPHA]
        self.assertEqual(got, want)

    def test_an_address_is_never_queried_twice(self):
        node = krpc.Node(nid(5), ("1.1.1.1", 1))
        lk = lookup.Lookup(nid(1), [node, node], want_peers=True)
        self.assertEqual(lk.next_queries(0), [node])
        lk.on_reply(node, krpc.Reply(b"t", nid(5), [(nid(5), node.addr), (nid(6), node.addr)], [], None), 0)
        self.assertEqual(lk.next_queries(0), [])
        self.assertTrue(lk.done())

    def test_it_stops_at_max_queries_against_an_endless_stream_of_closer_nodes(self):
        target = nid(0)
        counter = [1 << 150]

        def fresh():
            counter[0] -= 1  # each new node is closer than every earlier one
            return krpc.Node(nid(counter[0]), ("11.%d.%d.%d" % (counter[0] >> 16 & 255, counter[0] >> 8 & 255,
                                                                counter[0] & 255 or 1), 6881))

        queried = []

        def answer(node):
            queried.append(node.addr)
            return krpc.Reply(b"t", node.id, [tuple(fresh()) for _ in range(8)], [], None)

        lk = lookup.Lookup(target, [fresh()], want_peers=True)
        drive(lk, answer)
        self.assertEqual(len(queried), lookup.MAX_QUERIES)
        self.assertEqual(lk.sent, lookup.MAX_QUERIES)

    def test_timeouts_free_the_slot_and_the_node_is_not_retried(self):
        a, b = krpc.Node(nid(1), ("1.1.1.1", 1)), krpc.Node(nid(2), ("1.1.1.2", 1))
        lk = lookup.Lookup(nid(0), [a, b], want_peers=True)
        self.assertEqual(lk.next_queries(0), [a, b])
        lk.on_timeout(a)
        lk.on_timeout(b)
        self.assertEqual(lk.next_queries(0), [])
        self.assertTrue(lk.done())

    def test_a_lookup_with_no_seeds_is_done(self):
        lk = lookup.Lookup(nid(0), [], want_peers=True)
        self.assertEqual(lk.next_queries(0), [])
        self.assertTrue(lk.done())

    def test_it_is_not_done_while_a_query_is_in_flight(self):
        a = krpc.Node(nid(1), ("1.1.1.1", 1))
        lk = lookup.Lookup(nid(0), [a], want_peers=True)
        lk.next_queries(0)
        self.assertFalse(lk.done())

    def test_values_count_only_from_nodes_that_were_queried(self):
        a, stranger = krpc.Node(nid(1), ("1.1.1.1", 1)), krpc.Node(nid(2), ("2.2.2.2", 2))
        lk = lookup.Lookup(nid(0), [a], want_peers=True)
        lk.next_queries(0)
        r = krpc.Reply(b"t", nid(2), [], [("8.8.8.8", 80)], None)
        self.assertEqual(lk.on_reply(stranger, r, 0), [])
        self.assertEqual(lk.peers, [])
        self.assertEqual(lk.on_reply(a, r, 0), [("8.8.8.8", 80)])

    def test_a_reply_that_claims_our_own_id_is_rejected(self):
        a = krpc.Node(nid(1), ("1.1.1.1", 1))
        own = nid(99)
        lk = lookup.Lookup(nid(0), [a], want_peers=True, own_id=own)
        lk.next_queries(0)
        r = krpc.Reply(b"t", own, [(nid(3), ("3.3.3.3", 3))], [("8.8.8.8", 80)], None)
        self.assertEqual(lk.on_reply(a, r, 0), [])
        self.assertEqual((lk.peers, lk.answered, lk.inflight), ([], [], {}))
        self.assertEqual(lk.next_queries(0), [])  # its nodes were not learned either
        self.assertTrue(lk.done())

    def test_a_second_reply_from_the_same_node_adds_nothing(self):
        a = krpc.Node(nid(1), ("1.1.1.1", 1))
        lk = lookup.Lookup(nid(0), [a], want_peers=True)
        lk.next_queries(0)
        r = krpc.Reply(b"t", nid(1), [(nid(3), ("3.3.3.3", 3))], [("8.8.8.8", 80)], None)
        self.assertEqual(lk.on_reply(a, r, 0), [("8.8.8.8", 80)])
        r2 = krpc.Reply(b"t", nid(1), [(nid(4), ("4.4.4.4", 4))], [("7.7.7.7", 70)], None)
        self.assertEqual(lk.on_reply(a, r2, 0), [])
        self.assertEqual([n.addr for n in lk.next_queries(0)], [("3.3.3.3", 3)])

    def test_peers_are_not_collected_when_not_wanted(self):
        a = krpc.Node(nid(1), ("1.1.1.1", 1))
        lk = lookup.Lookup(nid(0), [a], want_peers=False)
        lk.next_queries(0)
        self.assertEqual(lk.on_reply(a, krpc.Reply(b"t", nid(1), [], [("8.8.8.8", 80)], None), 0), [])

    def test_only_new_peers_are_returned_and_the_total_is_capped(self):
        nodes = [krpc.Node(nid(i + 1), ("1.1.1.%d" % (i + 1), 1)) for i in range(8)]
        lk = lookup.Lookup(nid(0), nodes, want_peers=True)
        lk.next_queries(0)
        same = [("8.8.8.8", 80)]
        self.assertEqual(lk.on_reply(nodes[0], krpc.Reply(b"t", nid(1), [], same, None), 0), same)
        self.assertEqual(lk.on_reply(nodes[1], krpc.Reply(b"t", nid(2), [], same, None), 0), [])
        total = 0
        for i, node in enumerate(nodes[2:]):
            vals = [("20.%d.%d.1" % (i, j), 1000) for j in range(100)]
            total += len(lk.on_reply(node, krpc.Reply(b"t", node.id, [], vals, None), 0))
        self.assertLessEqual(len(lk.peers), lookup.MAX_PEERS)
        self.assertEqual(total + 1, len(lk.peers))

    def test_a_node_without_a_known_id_takes_the_id_it_answers_with(self):
        guess = krpc.Node(nid(0), ("1.1.1.1", 1))  # a bootstrap host: its id is a placeholder
        lk = lookup.Lookup(nid(0), [guess], want_peers=True)
        lk.next_queries(0)
        lk.on_reply(guess, krpc.Reply(b"t", nid(1 << 100), [], [], None), 0)
        self.assertEqual([n.id for n in lk.answered], [nid(1 << 100)])

    def test_the_candidate_list_stays_bounded(self):
        a = krpc.Node(nid(1 << 159), ("1.1.1.1", 1))
        lk = lookup.Lookup(nid(0), [a], want_peers=True)
        lk.next_queries(0)
        nodes = [(nid((i + 1) << 20), ("5.%d.%d.1" % (i // 250, i % 250), 1)) for i in range(2000)]
        for i in range(0, 2000, 16):
            lk.add([krpc.Node(*n) for n in nodes[i:i + 16]])
        self.assertLessEqual(len(lk._cands), lookup.MAX_CANDIDATES + lookup.ALPHA)


def sample_nodes(n):
    return [krpc.Node(bytes([i % 250 + 1]) * 20, ("12.%d.%d.1" % (i // 250, i % 250), 6000 + i))
            for i in range(n)]


class Cache(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / cache.CACHE_NAME

    def entry(self, nid_, ip, port):
        return nid_ + socket.inet_aton(ip) + struct.pack(">H", port)

    def test_a_round_trip(self):
        nodes = sample_nodes(5)
        cache.save(self.path, nodes)
        self.assertEqual(cache.load(self.path), nodes)
        self.assertEqual(self.path.read_bytes()[:7], b"DMDHT1\n")
        self.assertEqual(self.path.stat().st_size, 7 + 5 * 26)

    def test_save_keeps_at_most_200_nodes(self):
        cache.save(self.path, sample_nodes(500))
        self.assertEqual(cache.load(self.path), sample_nodes(500)[:cache.MAX_CACHE_NODES])
        self.assertLess(self.path.stat().st_size, cache.MAX_CACHE_BYTES)

    def test_save_replaces_the_file_and_leaves_nothing_else(self):
        cache.save(self.path, sample_nodes(3))
        cache.save(self.path, sample_nodes(2))
        self.assertEqual(len(cache.load(self.path)), 2)
        self.assertEqual(os.listdir(self.dir.name), [cache.CACHE_NAME])

    def test_a_missing_file_gives_nothing(self):
        self.assertEqual(cache.load(self.path), [])

    def test_garbage_gives_nothing(self):
        for data in (b"", b"garbage", b"DMDHT1\n" + b"x" * 25, b"DMDHT2\n" + b"\x01" * 26, os.urandom(500)):
            self.path.write_bytes(data)
            self.assertEqual(cache.load(self.path), [], data[:20])

    def test_a_huge_file_gives_nothing(self):
        self.path.write_bytes(b"DMDHT1\n" + self.entry(NID, "1.2.3.4", 80) * 41000)
        self.assertGreater(self.path.stat().st_size, 1 << 20)
        self.assertEqual(cache.load(self.path), [])

    def test_a_symlink_is_ignored_and_replaced(self):
        target = Path(self.dir.name) / "elsewhere"
        target.write_bytes(b"DMDHT1\n" + self.entry(NID, "1.2.3.4", 80))
        self.path.symlink_to(target)
        self.assertEqual(cache.load(self.path), [])
        cache.save(self.path, sample_nodes(2))
        self.assertFalse(self.path.is_symlink())
        self.assertEqual(len(cache.load(self.path)), 2)
        self.assertEqual(target.read_bytes(), b"DMDHT1\n" + self.entry(NID, "1.2.3.4", 80))

    @unittest.skipUnless(hasattr(os, "mkfifo"), "needs mkfifo")
    def test_a_fifo_is_ignored_without_blocking(self):
        os.mkfifo(self.path)
        self.assertEqual(cache.load(self.path), [])

    def test_a_directory_is_ignored(self):
        self.path.mkdir()
        self.assertEqual(cache.load(self.path), [])

    def test_unusable_entries_are_skipped(self):
        data = b"DMDHT1\n" + b"".join([
            self.entry(NID, "0.0.0.0", 6881), self.entry(NID, "1.2.3.4", 0),
            self.entry(NID, "255.255.255.255", 80), self.entry(NID, "224.0.0.1", 80),
            self.entry(NID, "127.0.0.1", 80), self.entry(NID, "0.9.9.9", 80),
            self.entry(OTHER, "4.3.2.1", 99)])
        self.path.write_bytes(data)
        self.assertEqual(cache.load(self.path), [krpc.Node(OTHER, ("4.3.2.1", 99))])

    def test_loopback_loads_only_on_request(self):
        self.path.write_bytes(b"DMDHT1\n" + self.entry(NID, "127.0.0.1", 7000))
        self.assertEqual(cache.load(self.path), [])
        self.assertEqual(cache.load(self.path, allow_loopback=True), [krpc.Node(NID, ("127.0.0.1", 7000))])

    def test_private_entries_load_only_on_request(self):
        self.path.write_bytes(b"DMDHT1\n" + self.entry(NID, "10.0.0.1", 7000)
                              + self.entry(OTHER, "169.254.169.254", 80) + self.entry(NID, "4.3.2.1", 99))
        self.assertEqual(cache.load(self.path), [krpc.Node(NID, ("4.3.2.1", 99))])
        self.assertEqual(len(cache.load(self.path, allow_private=True)), 3)

    def test_load_removes_stale_temp_files_only(self):
        d = Path(self.dir.name)
        old, fresh, other, old_other = d / ".dht-old", d / ".dht-fresh", d / "keep.dat", d / "other-old.dat"
        for f in (old, fresh, other, old_other):
            f.write_bytes(b"x")
        two_hours_ago = time.time() - 7200
        for f in (old, old_other):
            os.utime(f, (two_hours_ago, two_hours_ago))
        cache.load(self.path)
        self.assertEqual(sorted(os.listdir(d)), [".dht-fresh", "keep.dat", "other-old.dat"])

    def test_load_does_not_follow_a_stale_temp_symlink(self):
        d = Path(self.dir.name)
        target = d / "target.dat"
        target.write_bytes(b"x")
        link = d / ".dht-link"
        link.symlink_to(target)
        cache.load(self.path)
        self.assertTrue(target.exists())

    def test_load_reads_at_most_200_entries(self):
        self.path.write_bytes(b"DMDHT1\n" + b"".join(
            self.entry(NID, "5.5.%d.1" % (i % 250), 1 + i) for i in range(300)))
        self.assertEqual(len(cache.load(self.path)), cache.MAX_CACHE_NODES)

    def test_a_read_only_folder_is_not_an_error(self):
        os.chmod(self.dir.name, stat.S_IRUSR | stat.S_IXUSR)
        self.addCleanup(os.chmod, self.dir.name, stat.S_IRWXU)
        if os.access(self.dir.name, os.W_OK):
            self.skipTest("running as a user that ignores permissions")
        cache.save(self.path, sample_nodes(2))
        self.assertEqual(cache.load(self.path), [])

    def test_a_missing_folder_is_not_an_error(self):
        cache.save(Path(self.dir.name) / "nope" / "x.dat", sample_nodes(2))

    def test_bad_nodes_are_not_written(self):
        cache.save(self.path, [krpc.Node(b"short", ("1.2.3.4", 80)), krpc.Node(NID, ("0.0.0.0", 80)),
                               krpc.Node(NID, ("1.2.3.4", 80))])
        self.assertEqual(cache.load(self.path), [krpc.Node(NID, ("1.2.3.4", 80))])


if __name__ == "__main__":
    unittest.main()
