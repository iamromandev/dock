"""The DHT client against in-process fake DHT networks (tests/fixtures/torrent/dht.py): no Docker."""

import asyncio
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tests" / "fixtures" / "torrent"))

import dht as fixture
from dmcore.torrent import bencode
from dmcore.torrent.dht import client, krpc, lookup

IH = bytes.fromhex("c9e15763f722f23e98a29decdfae341b98d53056")
PEER = ("203.0.113.7", 51413)
DHT_DIR = str(ROOT / "dmcore" / "torrent" / "dht")


class Recording(client.DhtClient):
    """A client that notes every datagram it sends."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.sends = []

    def _sendto(self, data, addr):
        self.sends.append((time.monotonic(), addr, data))
        super()._sendto(data, addr)


def make(net, cache_path=None, cls=Recording, bootstrap=None, **kw):
    c = cls(
        IH,
        net.addrs if bootstrap is None else bootstrap,
        cache_path,
        lambda m: None,
        allow_loopback=True,
        **kw,
    )
    c.found = []
    c.on_peers = c.found.extend
    return c


def busiest_second(times):
    times = sorted(times)
    return max((sum(1 for u in times if t <= u < t + 1) for t in times), default=0)


class Base(unittest.IsolatedAsyncioTestCase):
    async def network(self, **kw):
        net = fixture.DhtNetwork(**kw)
        await net.start("127.0.0.1")
        self.addCleanup(net.stop)
        return net

    async def client(self, net, **kw):
        c = make(net, **kw)
        self.addAsyncCleanup(c.close)
        return c


def public(i):
    return "%d.%d.%d.%d" % (20 + i // 60000, i // 250 % 240 + 1, i % 250 + 1, i % 200 + 1)


def big_reply(rnd, tid=None):
    """A valid-looking reply of maximum size: 16 nodes, 100 values, a token."""
    nodes = b"".join(
        fixture.compact_node(rnd.randbytes(20), (public(rnd.randrange(10**6)), 6881)) for _ in range(16)
    )
    values = [fixture.compact_peer((public(rnd.randrange(10**6)), 51413)) for _ in range(100)]
    return bencode.encode(
        {
            "t": tid or rnd.randbytes(8),
            "y": "r",
            "r": {"id": rnd.randbytes(20), "nodes": nodes, "values": values, "token": b"t" * 8},
        }
    )


def arm(c, node, lk, tid):
    """Make the client wait for tid from node, as _send_query does."""
    timer = asyncio.get_running_loop().call_later(60, lambda: None)
    c._pending[tid] = client._Pending(node.addr, node, lk, timer)
    c._waiting[node.addr] = c._waiting.get(node.addr, 0) + 1
    return timer


# What the wrong-transaction-id path may cost per datagram, and the share of a full parse it may take.
# Measured on a laptop at about 55 us (the bencode decode, which 100 values dominate) against 200 us for a
# full parse, so 0.3 of one. The bounds leave a margin of about 9x and 2x for a slow CI machine.
FLOOD_CPU_PER_DATAGRAM = 0.0005
FLOOD_SHARE_OF_FULL_PARSE = 0.6


class Accepted(unittest.IsolatedAsyncioTestCase):
    """Only what the routing table accepted is remembered for the cache."""

    async def asyncSetUp(self):
        self.c = client.DhtClient(IH, [], None, lambda m: None)
        self.addr = ("8.8.8.8", 6881)
        self.node = krpc.Node(b"n" * 20, self.addr)

    def answer(self, node_id, tid):
        lk = lookup.Lookup(IH, [self.node], want_peers=True, own_id=self.c.own_id)
        lk.next_queries(0)
        timer = arm(self.c, self.node, lk, tid)
        self.addCleanup(timer.cancel)
        nodes = fixture.compact_node(b"z" * 20, ("1.2.3.4", 80))
        msg = {"t": tid, "y": "r", "r": {"id": node_id, "nodes": nodes}}
        self.c._received(bencode.encode(msg), self.addr)
        return lk

    async def test_a_reply_that_names_our_own_id_is_not_cached_and_counts_for_nothing(self):
        lk = self.answer(self.c.own_id, b"tid00001")
        self.assertEqual((dict(self.c._answered), len(self.c.table), lk.answered), ({}, 0, []))
        self.assertEqual(self.c.stats["nodes"], 0)


class Failures(Base):
    async def test_a_send_that_raises_oserror_is_counted_and_not_a_crash(self):
        net = await self.network(n=20, info_hash=IH, peers=[PEER])
        c = await self.client(net)
        with mock.patch.object(Recording, "_sendto", side_effect=OSError("network is unreachable")):
            self.assertEqual(await asyncio.wait_for(c.lookup(), 5), [])
        self.assertGreater(c.stats["errors"], 0)
        self.assertEqual(await asyncio.wait_for(c.lookup(), 5), [PEER])

    async def test_a_socket_that_cannot_be_opened_is_not_a_crash(self):
        net = await self.network(n=5, info_hash=IH, peers=[PEER])
        c = await self.client(net)
        loop = asyncio.get_running_loop()
        with mock.patch.object(loop, "create_datagram_endpoint", side_effect=OSError("no route")):
            self.assertEqual(await c.lookup(), [])
        self.assertEqual(c.stats["errors"], 1)


if __name__ == "__main__":
    unittest.main()
