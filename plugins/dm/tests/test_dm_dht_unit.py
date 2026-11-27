"""Unit tests for the DHT client of dm: KRPC codec, routing table, lookup and node cache (no network)."""

import random
import socket
import struct
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from dmcore.torrent import bencode
from dmcore.torrent.dht import krpc, lookup, table

NID = b"abcdefghij0123456789"
OTHER = b"mnopqrstuvwxyz123456"


def node_bytes(nid, ip, port):
    return nid + socket.inet_aton(ip) + struct.pack(">H", port)


def reply(**r):
    r.setdefault("id", NID)
    return bencode.encode({"t": b"aa", "y": b"r", "r": r})


class Usable(unittest.TestCase):
    PRIVATE = (
        ("10.1.2.3", 1),
        ("10.255.255.255", 80),
        ("172.16.0.1", 5000),
        ("172.31.255.255", 5000),
        ("192.168.0.5", 65535),
        ("169.254.169.254", 80),
        ("100.64.0.1", 80),
        ("100.127.255.255", 80),
        ("240.0.0.1", 80),
        ("254.1.2.3", 80),
        ("198.18.0.1", 80),
        ("198.19.255.255", 80),
    )

    def test_unusable_addresses(self):
        for addr in (
            ("0.0.0.0", 6881),
            ("0.1.2.3", 6881),
            ("255.255.255.255", 6881),
            ("224.0.0.1", 6881),
            ("239.255.255.250", 6881),
            ("127.0.0.1", 6881),
            ("8.8.8.8", 0),
            ("8.8.8.8", 65536),
            ("::1", 6881),
            ("not an ip", 6881),
            ("8.8.8.8", "80"),
            ("8.8.8.8",),
            None,
            ("1.2.3", 80),
            ("1.2.3.4.5", 80),
            ("1.2.3.256", 80),
            ("1.2.3.04", 80),
            ("1.2.3.-4", 80),
            ("1.2.3.+4", 80),
            (" 1.2.3.4", 80),
            ("1.2.3.4 ", 80),
            ("", 80),
            ("1..3.4", 80),
        ):
            self.assertFalse(krpc.usable(addr), addr)


class ParseStages(unittest.TestCase):
    def test_the_header_rejects_what_parse_rejects_early(self):
        for data in (
            b"",
            b"x" * 1501,
            bencode.encode([1]),
            bencode.encode({"y": b"r"}),
            bencode.encode({"t": b"", "y": b"r"}),
            bencode.encode({"t": b"aa", "y": b"z"}),
        ):
            with self.assertRaises(krpc.KrpcError):
                krpc.parse_header(data)

    def test_the_body_stage_of_a_query_and_an_error(self):
        q = krpc.parse_body(krpc.parse_header(b"d1:ad2:id20:" + NID + b"e1:q4:ping1:t2:aa1:y1:qe"))
        self.assertEqual((q.tid, q.method), (b"aa", "ping"))
        e = krpc.parse_body(krpc.parse_header(b"d1:eli201e1:xe1:t2:aa1:y1:ee"))
        self.assertIsInstance(e, krpc.ErrorMsg)


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
    def test_it_stops_at_max_queries_against_an_endless_stream_of_closer_nodes(self):
        target = nid(0)
        counter = [1 << 150]

        def fresh():
            counter[0] -= 1  # each new node is closer than every earlier one
            return krpc.Node(
                nid(counter[0]),
                (
                    "11.%d.%d.%d" % (counter[0] >> 16 & 255, counter[0] >> 8 & 255, counter[0] & 255 or 1),
                    6881,
                ),
            )

        queried = []

        def answer(node):
            queried.append(node.addr)
            return krpc.Reply(b"t", node.id, [tuple(fresh()) for _ in range(8)], [], None)

        lk = lookup.Lookup(target, [fresh()], want_peers=True)
        drive(lk, answer)
        self.assertEqual(len(queried), lookup.MAX_QUERIES)
        self.assertEqual(lk.sent, lookup.MAX_QUERIES)


def sample_nodes(n):
    return [
        krpc.Node(bytes([i % 250 + 1]) * 20, ("12.%d.%d.1" % (i // 250, i % 250), 6000 + i)) for i in range(n)
    ]


if __name__ == "__main__":
    unittest.main()
