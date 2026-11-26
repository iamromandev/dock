"""The DHT client against in-process fake DHT networks (tests/fixtures/torrent/dht.py): no Docker."""

import asyncio
import errno
import os
import random
import sys
import tempfile
import time
import tracemalloc
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tests" / "fixtures" / "torrent"))

import dht as fixture
from dmcore.torrent import bencode
from dmcore.torrent.dht import cache, client, krpc, lookup

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
    c = cls(IH, net.addrs if bootstrap is None else bootstrap, cache_path, lambda m: None,
            allow_loopback=True, **kw)
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


class Honest(Base):
    async def test_it_finds_a_planted_peer_in_under_5_seconds(self):
        net = await self.network(n=50, info_hash=IH, peers=[PEER])
        c = await self.client(net)
        start = time.monotonic()
        peers = await asyncio.wait_for(c.lookup(), 5)
        self.assertEqual(peers, [PEER])
        self.assertEqual(c.found, [PEER])
        self.assertLess(time.monotonic() - start, 5)
        self.assertEqual(c.stats["lookups"], 1)
        self.assertEqual(c.stats["peers"], 1)
        self.assertGreater(c.stats["nodes"], 0)

    async def test_every_query_is_read_only_and_only_asks_for_peers(self):
        net = await self.network(n=50, info_hash=IH, peers=[PEER])
        c = await self.client(net)
        await c.lookup()
        self.assertGreater(len(net.queries), 0)
        self.assertTrue(all(q["ro"] == 1 for q in net.queries))
        self.assertEqual({q["method"] for q in net.queries}, {"get_peers"})
        for _, _, data in c.sends:
            msg, _ = bencode.decode(data)
            self.assertEqual((msg[b"y"], msg[b"q"], msg[b"ro"]), (b"q", b"get_peers", 1))
            self.assertLessEqual(len(data), 120)
        self.assertEqual(c.stats["sent"], len(c.sends))

    async def test_the_os_picks_the_port_and_nothing_is_bound_by_us(self):
        net = await self.network(n=5, info_hash=IH, peers=[PEER])
        c = await self.client(net)
        self.assertIsNone(c._transport)
        await c.lookup()
        self.assertNotEqual(c._transport.get_extra_info("socket").getsockname()[1], 0)

    async def test_queries_sent_to_dm_get_no_reply(self):
        net = await self.network(n=20, info_hash=IH, peers=[PEER], probe=True)
        c = await self.client(net)
        await c.lookup()
        await asyncio.sleep(0.3)
        self.assertGreater(c.stats["dropped"], 0)  # the probes arrived and were dropped
        self.assertEqual(net.dm_replies, [])
        self.assertEqual(net.announces, [])

    async def test_a_lookup_without_peers_ends_with_none(self):
        net = await self.network(n=30)
        c = await self.client(net)
        self.assertEqual(await asyncio.wait_for(c.lookup(), 10), [])

    async def test_a_second_lookup_starts_from_the_nodes_the_first_one_learned(self):
        net = await self.network(n=50, info_hash=IH, peers=[PEER])
        c = await self.client(net)
        await c.lookup()
        known = set(c._answered)
        c.bootstrap, c._boot = [], []
        before = len(c.sends)
        self.assertEqual(await c.lookup(), [PEER])
        self.assertGreater(len(c.sends), before)
        self.assertIn(c.sends[before][1], known)

    async def test_bootstrap_hosts_are_resolved_and_failures_skipped(self):
        net = await self.network(n=20, info_hash=IH, peers=[PEER])
        calls = []

        async def resolve(host, port):
            calls.append(host)
            if host == "dead.invalid":
                return []
            return [net.addrs[0], net.addrs[1]]

        c = make(net, bootstrap=[("dead.invalid", 6881), ("good.example", 6881)], resolve=resolve)
        self.addAsyncCleanup(c.close)
        self.assertEqual(await c.lookup(), [PEER])
        self.assertEqual(sorted(calls), ["dead.invalid", "good.example"])

    async def test_a_resolver_that_hangs_does_not_hold_up_cached_nodes(self):
        net = await self.network(n=20, info_hash=IH, peers=[PEER])
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / cache.CACHE_NAME
            first = make(net, path)
            await first.lookup()
            first.save_cache()
            await first.close()
            self.assertTrue(path.exists())

            async def hang(host, port):
                await asyncio.sleep(30)

            c = make(net, path, bootstrap=[("slow.example", 6881)], resolve=hang)
            self.addAsyncCleanup(c.close)
            self.assertEqual(await asyncio.wait_for(c.lookup(), 5), [PEER])

    async def test_it_never_sends_to_an_unusable_address(self):
        net = await self.network(n=20, info_hash=IH, peers=[PEER])
        c = await self.client(net, bootstrap=[("0.0.0.0", 6881), ("255.255.255.255", 6881),
                                              ("224.0.0.1", 6881),
                                              ("1.2.3.4", 0), net.addrs[0]])
        await c.lookup()
        self.assertTrue(c.sends)
        for _, addr, _ in c.sends:
            self.assertTrue(krpc.usable(addr, allow_loopback=True), addr)

    async def test_the_send_rate_is_limited(self):
        net = await self.network(n=50, info_hash=IH, peers=[PEER])
        c = await self.client(net)
        await c.lookup()
        self.assertLessEqual(busiest_second([t for t, _, _ in c.sends]), client.SEND_RATE)


class Rules(unittest.IsolatedAsyncioTestCase):
    async def test_sends_are_spaced_to_the_rate_limit(self):
        c = client.DhtClient(IH, [], None, lambda m: None)
        start = time.monotonic()
        for _ in range(61):
            await c._throttle()
        self.assertGreaterEqual(time.monotonic() - start, 60 / client.SEND_RATE - 0.05)

    async def test_a_reply_with_the_right_transaction_id_from_the_wrong_address_is_dropped(self):
        c = client.DhtClient(IH, [], None, lambda m: None)
        a, b = ("1.1.1.1", 1), ("2.2.2.2", 2)
        lk = lookup.Lookup(IH, [krpc.Node(b"a" * 20, a), krpc.Node(b"b" * 20, b)], want_peers=True)
        first, second = lk.next_queries(0)
        timer = asyncio.get_running_loop().call_later(60, lambda: None)
        c._pending = {b"aaaa": client._Pending(first.addr, first, lk, timer),
                      b"bbbb": client._Pending(second.addr, second, lk, timer)}
        c._waiting = {first.addr: 1, second.addr: 1}
        data = bencode.encode({"t": b"aaaa", "y": "r", "r": {"id": b"a" * 20}})
        c._received(data, second.addr)
        self.assertEqual((c.stats["dropped"], len(lk.answered), sorted(c._pending)),
                         (1, 0, [b"aaaa", b"bbbb"]))
        c._received(data, first.addr)
        self.assertEqual((c.stats["dropped"], len(lk.answered), sorted(c._pending)), (1, 1, [b"bbbb"]))
        c._received(data, first.addr)  # a repeat of the same reply
        self.assertEqual(c.stats["dropped"], 2)
        timer.cancel()


class SingleNode(Base):
    """One node that holds the peer and misbehaves in one way: its answer must be dropped or capped."""

    async def one(self, mode, timeout=0.2, **kw):
        net = await self.network(n=1, info_hash=IH, peers=[PEER], late_delay=0.6, probe=False, **kw)
        net.nodes[0].mode = mode
        c = await self.client(net)
        with mock.patch.object(lookup, "TIMEOUT", timeout):
            peers = await asyncio.wait_for(c.lookup(), 10)
        await asyncio.sleep(0.7 if mode == "late" else 0.1)
        return c, peers

    async def test_replies_that_break_the_rules_are_dropped(self):
        for mode in ("oversize", "garbage", "deep", "spoof", "wrong-tid", "late", "silent"):
            with self.subTest(mode):
                c, peers = await self.one(mode)
                self.assertEqual(peers, [])
                self.assertEqual(c.found, [])
                if mode != "silent":
                    self.assertGreaterEqual(c.stats["dropped"], 1)

    async def test_a_node_that_names_only_itself_is_asked_once(self):
        c, peers = await self.one("self")
        self.assertEqual(peers, [])
        self.assertEqual(c.stats["sent"], 1)

    async def test_a_reply_sent_many_times_counts_once(self):
        c, peers = await self.one("dup-replies")
        self.assertEqual(peers, [PEER])
        self.assertEqual(c.found, [PEER])
        self.assertGreaterEqual(c.stats["dropped"], 40)

    async def test_a_reply_with_170_values_adds_at_most_100(self):
        c, peers = await self.one("many-values")
        self.assertEqual(len(peers), krpc.MAX_VALUES)
        self.assertEqual(len(c.found), krpc.MAX_VALUES)

    async def test_nodes_with_bad_addresses_are_never_queried(self):
        c, peers = await self.one("bad-addrs")
        self.assertEqual(c.stats["sent"], 1)

    async def test_private_nodes_and_peers_are_never_queried_or_reported(self):
        c, peers = await self.one("private")
        self.assertEqual(peers, [])
        self.assertEqual(c.found, [])
        self.assertEqual([addr for _, addr, _ in c.sends], [c.sends[0][1]])  # only the node we asked
        self.assertEqual(c.stats["sent"], 1)

    async def test_a_node_that_names_55_nodes_gets_at_most_16_queries_after_it(self):
        c, peers = await self.one("many-nodes", timeout=0.05)
        self.assertLessEqual(c.stats["sent"], 1 + krpc.MAX_NODES)


class Hostile(Base):
    async def test_the_download_of_peers_survives_each_hostile_mode(self):
        for mode in fixture.MODES:
            with self.subTest(mode):
                net = await self.network(n=50, info_hash=IH, peers=[PEER])
                net.hostile(mode, 20)
                c = make(net)
                tracemalloc.start()
                peak = 0
                done = False

                async def sample():
                    nonlocal peak
                    while not done:
                        snap = tracemalloc.take_snapshot().filter_traces(
                            [tracemalloc.Filter(True, DHT_DIR + "/*")])
                        peak = max(peak, sum(s.size for s in snap.statistics("filename")))
                        await asyncio.sleep(0.05)

                sampler = asyncio.create_task(sample())
                try:
                    with mock.patch.object(lookup, "TIMEOUT", 0.3):
                        peers = await asyncio.wait_for(c.lookup(), 30)
                finally:
                    done = True
                    await sampler
                    tracemalloc.stop()
                    await c.close()
                    net.stop()
                self.assertIn(PEER, peers)
                self.assertLessEqual(c.stats["sent"], lookup.MAX_QUERIES)
                self.assertLessEqual(busiest_second([t for t, _, _ in c.sends]), client.SEND_RATE)
                self.assertLess(peak, 2 * 1024 * 1024)

    async def test_a_flood_of_big_forged_replies_from_a_queried_address_is_cheap_and_ignored(self):
        net = await self.network(n=50, info_hash=IH, peers=[PEER])
        c = await self.client(net)
        spent = []
        real = c._received

        def timed(data, addr):
            t = time.perf_counter()
            real(data, addr)
            spent.append(time.perf_counter() - t)

        c._received = timed

        async def flood():
            while not net.queries:
                await asyncio.sleep(0.001)
            net.flood(net.queries[0]["src"], 10000, big=True)

        task = asyncio.create_task(flood())
        peers = await asyncio.wait_for(c.lookup(), 10)
        await task
        self.assertIn(PEER, peers)
        await asyncio.sleep(0.2)
        dropped = c.stats["dropped_early"] + c.stats["dropped_late"]
        # How much of a 13 MB burst gets past the kernel depends on the socket's receive buffer (on Linux's default
        # only about 92 of these datagrams fit), so no fixed count: every datagram that arrived and was not the
        # answer to a query we sent is dropped.
        forged = len(spent) - c.stats["sent"]
        self.assertGreater(forged, 0, "none of the flood reached the client")
        self.assertGreaterEqual(dropped, forged)
        # forged tids from a queried address are decoded, then dropped
        self.assertGreaterEqual(c.stats["dropped_late"], 1)
        self.assertEqual(c.stats["dropped"], dropped)
        self.assertLess(sum(spent) / len(spent), FLOOD_CPU_PER_DATAGRAM)


def public(i):
    return "%d.%d.%d.%d" % (20 + i // 60000, i // 250 % 240 + 1, i % 250 + 1, i % 200 + 1)


def big_reply(rnd, tid=None):
    """A valid-looking reply of maximum size: 16 nodes, 100 values, a token."""
    nodes = b"".join(fixture.compact_node(rnd.randbytes(20), (public(rnd.randrange(10 ** 6)), 6881))
                     for _ in range(16))
    values = [fixture.compact_peer((public(rnd.randrange(10 ** 6)), 51413)) for _ in range(100)]
    return bencode.encode({"t": tid or rnd.randbytes(8), "y": "r",
                           "r": {"id": rnd.randbytes(20), "nodes": nodes, "values": values,
                                 "token": b"t" * 8}})


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


class FloodCost(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.c = client.DhtClient(IH, [], None, lambda m: None)
        self.addr = ("8.8.8.8", 6881)
        self.node = krpc.Node(b"n" * 20, self.addr)
        self.lk = lookup.Lookup(IH, [self.node], want_peers=True)
        self.lk.next_queries(0)
        self.timer = arm(self.c, self.node, self.lk, b"realtid1")
        self.addCleanup(self.timer.cancel)

    def cpu(self, data, addr, n):
        t = time.process_time()
        for d in data:
            self.c._received(d, addr)
        return (time.process_time() - t) / n

    async def test_a_flood_of_max_size_replies_with_wrong_tids_costs_little_cpu(self):
        rnd = random.Random(5)
        data = [big_reply(rnd) for _ in range(3000)]
        self.assertGreater(len(data[0]), 1100)
        self.assertLessEqual(len(data[0]), krpc.MAX_DATAGRAM)
        per = self.cpu(data, self.addr, len(data))
        self.assertLess(per, FLOOD_CPU_PER_DATAGRAM, f"{per * 1e6:.0f} us per datagram")
        self.assertEqual(self.c.stats["dropped_late"], len(data))
        self.assertEqual(self.c.stats["dropped_early"], 0)
        self.assertEqual((self.lk.answered, list(self.c._pending)), ([], [b"realtid1"]))

    async def test_the_wrong_tid_path_costs_less_than_a_full_parse(self):
        rnd = random.Random(6)
        data = [big_reply(rnd) for _ in range(1000)]
        t = time.process_time()
        for d in data:
            krpc.parse(d, False, False)
        full = (time.process_time() - t) / len(data)
        per = self.cpu(data, self.addr, len(data))
        self.assertLess(per, FLOOD_SHARE_OF_FULL_PARSE * full, f"{per * 1e6:.0f} us of {full * 1e6:.0f} us")

    async def test_datagrams_from_other_addresses_and_oversize_ones_are_dropped_before_decoding(self):
        rnd = random.Random(7)
        data = [big_reply(rnd) for _ in range(500)]
        per = self.cpu(data, ("9.9.9.9", 1), len(data))  # not waiting for that address
        self.assertLess(per, 0.00002, f"{per * 1e6:.0f} us per datagram")
        self.assertEqual((self.c.stats["dropped_early"], self.c.stats["dropped_late"]), (500, 0))
        self.c._received(b"x" * 1501, self.addr)  # too big, from a queried address
        self.assertEqual((self.c.stats["dropped_early"], self.c.stats["dropped_late"]), (501, 0))

    async def test_a_datagram_that_does_not_decode_counts_as_a_late_drop(self):
        self.c._received(b"garbage", self.addr)
        self.assertEqual((self.c.stats["dropped_early"], self.c.stats["dropped_late"]), (0, 1))

    async def test_the_right_tid_from_the_right_address_is_still_answered(self):
        rnd = random.Random(8)
        self.c._received(big_reply(rnd, b"realtid1"), self.addr)
        self.assertEqual((len(self.lk.answered), self.c._pending, self.c.stats["dropped"]), (1, {}, 0))
        self.assertEqual(len(self.lk.peers), 100)


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

    async def test_an_address_that_changes_its_id_keeps_the_first_in_the_cache(self):
        self.answer(b"a" * 20, b"tid00001")
        self.answer(b"b" * 20, b"tid00002")
        self.assertEqual(list(self.c._answered.values()), [krpc.Node(b"a" * 20, self.addr)])

    async def test_a_reply_with_neither_nodes_nor_values_is_not_cached(self):
        lk = lookup.Lookup(IH, [self.node], want_peers=True)
        lk.next_queries(0)
        timer = arm(self.c, self.node, lk, b"tid00001")
        self.addCleanup(timer.cancel)
        self.c._received(bencode.encode({"t": b"tid00001", "y": "r", "r": {"id": b"a" * 20}}), self.addr)
        self.assertEqual(dict(self.c._answered), {})
        self.assertEqual(len(lk.answered), 1)


class Seeds(unittest.IsolatedAsyncioTestCase):
    def cached(self, nodes, **kw):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        path = Path(d.name) / cache.CACHE_NAME
        cache.save(path, nodes)
        return client.DhtClient(IH, [], path, lambda m: None, **kw)

    async def test_at_most_8_cache_nodes_seed_a_lookup_and_their_claimed_ids_do_not_rank_them(self):
        nodes = [krpc.Node(IH, (public(i), 6881)) for i in range(40)]  # every one claims to be the info-hash
        c = self.cached(nodes)
        seeds = c._seeds()
        self.assertEqual(len(seeds), client.MAX_CACHE_SEEDS)
        self.assertEqual(client.MAX_CACHE_SEEDS, 8)
        self.assertTrue(all(n.id != IH for n in seeds))
        boot = krpc.Node(IH, ("5.5.5.5", 6881))  # a bootstrap node: the id is a placeholder at distance 0
        lk = lookup.Lookup(IH, seeds, want_peers=True)
        lk.add([boot])
        self.assertEqual(lk._closest()[0], boot)

    async def test_private_cache_entries_are_ignored_unless_allowed(self):
        nodes = [krpc.Node(b"a" * 20, ("10.0.0.5", 6881)), krpc.Node(b"b" * 20, ("169.254.169.254", 80)),
                 krpc.Node(b"c" * 20, ("8.8.4.4", 53))]
        self.assertEqual([n.addr for n in self.cached(nodes)._seeds()], [("8.8.4.4", 53)])
        self.assertEqual(len(self.cached(nodes, allow_private=True)._seeds()), 3)

    async def test_bootstrap_addresses_are_taken_round_robin_per_host_up_to_16(self):
        first = {"a.example": 11, "b.example": 12}

        async def resolve(host, port):
            if host == "single.example":
                return [("7.7.7.7", port)]
            return [(f"{first[host]}.1.1.{i + 1}", port) for i in range(20)]

        hosts = [("a.example", 1), ("b.example", 2), ("single.example", 3)]
        c = client.DhtClient(IH, hosts, None, lambda m: None, resolve=resolve)
        addrs = [n.addr for n in await c._resolve_all()]
        self.assertEqual(len(addrs), client.MAX_BOOTSTRAP)
        self.assertIn(("7.7.7.7", 3), addrs)  # the third host is not starved by the first two
        self.assertEqual(addrs[:3], [("11.1.1.1", 1), ("12.1.1.1", 2), ("7.7.7.7", 3)])
        self.assertEqual(sum(1 for a in addrs if a[1] == 1), 8)
        self.assertEqual(sum(1 for a in addrs if a[1] == 2), 7)


class PoisonedCache(Base):
    async def hostile_node(self):
        """One node that answers every query with no nodes and no values."""
        evil = fixture.DhtNetwork(n=1, modes={0: "empty"})
        await evil.start("127.0.0.1")
        self.addCleanup(evil.stop)
        return evil

    async def test_a_cached_node_that_claims_the_info_hash_and_answers_empty_does_not_end_the_lookup(self):
        net = await self.network(n=30, info_hash=IH, peers=[PEER])
        evil = await self.hostile_node()
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / cache.CACHE_NAME
            cache.save(path, [krpc.Node(IH, evil.addrs[0])])
            c = make(net, path)
            self.addAsyncCleanup(c.close)
            self.assertEqual(await asyncio.wait_for(c.lookup(), 10), [PEER])
            self.assertEqual(len(evil.queries), 1)
            self.assertGreater(len(net.queries), 3)  # the bootstrap network was consulted

    async def test_a_lookup_waits_for_a_slow_bootstrap_before_giving_up_on_a_dead_end(self):
        net = await self.network(n=30, info_hash=IH, peers=[PEER])
        evil = await self.hostile_node()

        async def slow(host, port):
            await asyncio.sleep(0.4)
            return net.addrs

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / cache.CACHE_NAME
            cache.save(path, [krpc.Node(IH, evil.addrs[0])])
            c = make(net, path, bootstrap=[("slow.example", 1)], resolve=slow)
            self.addAsyncCleanup(c.close)
            self.assertEqual(await asyncio.wait_for(c.lookup(), 10), [PEER])

    async def test_the_poisoned_node_is_not_saved_again_and_the_next_run_is_clean(self):
        net = await self.network(n=30, info_hash=IH, peers=[PEER])
        evil = await self.hostile_node()
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / cache.CACHE_NAME
            cache.save(path, [krpc.Node(IH, evil.addrs[0])])
            first = make(net, path)
            self.assertEqual(await first.lookup(), [PEER])
            first.save_cache()
            await first.close()
            saved = cache.load(path, allow_loopback=True)
            self.assertTrue(saved)
            self.assertNotIn(evil.addrs[0], [n.addr for n in saved])
            second = make(net, path, bootstrap=[])
            self.addAsyncCleanup(second.close)
            self.assertEqual(await second.lookup(), [PEER])
            self.assertEqual(evil.queries[1:], [])

    async def test_a_lookup_that_found_no_peers_does_not_replace_the_cache(self):
        net = await self.network(n=30)  # nobody holds the peer
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / cache.CACHE_NAME
            old = [krpc.Node(bytes([i + 1]) * 20, (public(i), 6881)) for i in range(5)]
            cache.save(path, old)
            before = path.read_bytes()
            c = make(net, path)
            self.assertEqual(await asyncio.wait_for(c.lookup(), 10), [])
            self.assertTrue(c._answered)
            self.assertFalse(c.save_cache())
            await c.close()
            self.assertEqual(path.read_bytes(), before)

    async def test_close_alone_writes_nothing(self):
        net = await self.network(n=20, info_hash=IH, peers=[PEER])
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / cache.CACHE_NAME
            c = make(net, path)
            await c.lookup()
            await c.close()
            self.assertFalse(path.exists())

    async def test_nodes_close_to_the_info_hash_are_not_saved(self):
        net = await self.network(n=5)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / cache.CACHE_NAME
            c = make(net, path)
            c.stats["peers"] = 1
            near = bytes([IH[0], IH[1] ^ 0x01]) + b"\x00" * 18  # shares 15 leading bits with the info-hash
            edge = bytes([IH[0] ^ 0x01]) + b"\x00" * 19  # shares 7 bits
            far = bytes([IH[0] ^ 0x80]) + b"\x00" * 19
            for i, nid in enumerate((near, edge, far)):
                c._answered[("12.0.0.%d" % (i + 1), 80)] = krpc.Node(nid, ("12.0.0.%d" % (i + 1), 80))
            self.assertTrue(c.save_cache())
            self.assertEqual([n.id for n in cache.load(path)], [far, edge])  # latest first

class FixtureTidy(Base):
    async def test_stopping_the_network_cancels_the_late_replies_it_scheduled(self):
        net = fixture.DhtNetwork(n=1, info_hash=IH, peers=[PEER], late_delay=0.3)
        await net.start("127.0.0.1")
        net.nodes[0].mode = "late"
        c = make(net)
        with mock.patch.object(lookup, "TIMEOUT", 0.1):
            await c.lookup()
        await c.close()
        self.assertTrue(net.timers)
        net.stop()
        self.assertTrue(all(h.cancelled() for h in net.timers))


class Failures(Base):
    async def test_every_send_failing_at_the_socket_ends_the_lookup_early_and_the_client_recovers(self):
        net = await self.network(n=20, info_hash=IH, peers=[PEER])
        c = await self.client(net)
        await c._open()
        real = c._transport._sock

        class Dead:
            def sendto(self, *a):
                raise OSError(errno.ENETUNREACH, "network is unreachable")

            def __getattr__(self, name):
                return getattr(real, name)

        c._transport._sock = Dead()
        start = time.monotonic()
        self.assertEqual(await asyncio.wait_for(c.lookup(), 5), [])
        self.assertLess(time.monotonic() - start, 1.0)  # not 2 s per round of timeouts
        self.assertEqual(len(c.sends), client.ERROR_LIMIT)
        self.assertEqual(c.stats["errors"], client.ERROR_LIMIT)
        self.assertEqual((c._pending, c._waiting), ({}, {}))
        c._transport._sock = real
        self.assertEqual(await asyncio.wait_for(c.lookup(), 5), [PEER])

    async def test_a_send_that_raises_oserror_is_counted_and_not_a_crash(self):
        net = await self.network(n=20, info_hash=IH, peers=[PEER])
        c = await self.client(net)
        with mock.patch.object(Recording, "_sendto", side_effect=OSError("network is unreachable")):
            self.assertEqual(await asyncio.wait_for(c.lookup(), 5), [])
        self.assertGreater(c.stats["errors"], 0)
        self.assertEqual(await asyncio.wait_for(c.lookup(), 5), [PEER])

    async def test_one_failing_send_among_working_ones_does_not_end_the_lookup(self):
        net = await self.network(n=20, info_hash=IH, peers=[PEER])
        c = await self.client(net)
        await c._open()
        c._proto.error_received(OSError(errno.ECONNREFUSED, "refused"))  # an ICMP error between lookups
        self.assertEqual(await asyncio.wait_for(c.lookup(), 5), [PEER])

    async def test_a_lost_connection_is_reopened_by_the_next_lookup(self):
        net = await self.network(n=20, info_hash=IH, peers=[PEER])
        c = await self.client(net)
        self.assertEqual(await c.lookup(), [PEER])
        first = c._transport
        first.close()  # the socket goes away under us: connection_lost
        await asyncio.sleep(0.05)
        self.assertIsNone(c._transport)
        self.assertEqual(await asyncio.wait_for(c.lookup(), 5), [PEER])
        self.assertIsNot(c._transport, first)

    async def test_a_late_connection_lost_of_an_old_socket_does_not_drop_the_new_one(self):
        net = await self.network(n=5, info_hash=IH, peers=[PEER])
        c = await self.client(net)
        await c._open()
        old_proto = c._proto
        c._transport.close()
        await asyncio.sleep(0.05)
        await c._open()
        new = c._transport
        old_proto.connection_lost(None)
        self.assertIs(c._transport, new)

    async def test_a_socket_that_cannot_be_opened_is_not_a_crash(self):
        net = await self.network(n=5, info_hash=IH, peers=[PEER])
        c = await self.client(net)
        loop = asyncio.get_running_loop()
        with mock.patch.object(loop, "create_datagram_endpoint", side_effect=OSError("no route")):
            self.assertEqual(await c.lookup(), [])
        self.assertEqual(c.stats["errors"], 1)

    async def test_a_cancelled_lookup_leaves_nothing_pending(self):
        net = await self.network(n=1, info_hash=IH, peers=[PEER])
        net.nodes[0].mode = "silent"
        c = await self.client(net)
        task = asyncio.create_task(c.lookup())
        await asyncio.sleep(0.1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual((c._pending, c._waiting), ({}, {}))

    async def test_a_cancelled_lookup_waits_for_the_bootstrap_resolution_it_cancels(self):
        net = await self.network(n=1, info_hash=IH, peers=[PEER])

        async def hang(host, port):
            await asyncio.sleep(30)

        c = make(net, None, bootstrap=[("slow.example", 1)], resolve=hang)
        self.addAsyncCleanup(c.close)
        task = asyncio.create_task(c.lookup())
        await asyncio.sleep(0.1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(asyncio.all_tasks() - {asyncio.current_task()}, set())
        self.assertEqual((c._pending, c._waiting), ({}, {}))

    async def test_close_in_the_middle_of_a_lookup_ends_it_at_once_without_an_error(self):
        net = await self.network(n=3)
        for node in net.nodes:
            node.mode = "silent"
        c = await self.client(net)
        task = asyncio.create_task(c.lookup())
        await asyncio.sleep(0.2)
        self.assertFalse(task.done())
        start = time.monotonic()
        await c.close()
        self.assertEqual(await asyncio.wait_for(task, 0.5), [])
        self.assertLess(time.monotonic() - start, 0.5)
        self.assertEqual((c._pending, c._waiting), ({}, {}))
        self.assertIsNone(c._transport)

    async def test_close_while_the_lookup_waits_for_the_send_rate_does_not_raise(self):
        net = await self.network(n=20)
        c = await self.client(net)
        task = asyncio.create_task(c.lookup())
        await asyncio.sleep(0.05)
        await c.close()
        self.assertIsInstance(await asyncio.wait_for(task, 0.5), list)

    async def test_a_closed_client_does_no_more_lookups(self):
        net = await self.network(n=3, info_hash=IH, peers=[PEER])
        c = await self.client(net)
        await c.close()
        self.assertEqual(await asyncio.wait_for(c.lookup(), 0.5), [])
        self.assertIsNone(c._transport)
        self.assertEqual(net.queries, [])
        await c.close()  # twice is fine

    async def test_sending_on_a_closed_client_is_an_oserror_not_an_attribute_error(self):
        c = client.DhtClient(IH, [], None, lambda m: None)
        with self.assertRaises(OSError):
            c._sendto(b"x", ("8.8.8.8", 80))

    async def test_transaction_ids_are_8_bytes_and_all_different(self):
        net = await self.network(n=30, info_hash=IH, peers=[PEER])
        c = await self.client(net)
        await c.lookup()
        tids = [bencode.decode(data)[0][b"t"] for _, _, data in c.sends]
        self.assertTrue(tids)
        self.assertEqual({len(t) for t in tids}, {8})
        self.assertEqual(len(set(tids)), len(tids))

    async def test_a_callback_that_raises_does_not_break_the_lookup(self):
        net = await self.network(n=20, info_hash=IH, peers=[PEER])
        c = await self.client(net)

        def boom(peers):
            raise RuntimeError("boom")

        c.on_peers = boom
        self.assertEqual(await asyncio.wait_for(c.lookup(), 5), [PEER])


class NodeCache(Base):
    async def test_save_cache_writes_the_answered_nodes_and_a_new_client_starts_from_them(self):
        net = await self.network(n=50, info_hash=IH, peers=[PEER])
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / cache.CACHE_NAME
            first = make(net, path)
            await first.lookup()
            first.save_cache()
            await first.close()
            saved = cache.load(path, allow_loopback=True)
            self.assertTrue(0 < len(saved) <= cache.MAX_CACHE_NODES)
            self.assertTrue(all(n.addr in net.addrs for n in saved))
            self.assertEqual(os.listdir(d), [cache.CACHE_NAME])

            second = make(net, path, bootstrap=[])
            self.addAsyncCleanup(second.close)
            self.assertEqual(await second.lookup(), [PEER])
            self.assertIn(second.sends[0][1], [n.addr for n in saved])

    async def test_a_hostile_cache_is_ignored_and_replaced(self):
        net = await self.network(n=20, info_hash=IH, peers=[PEER])
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / cache.CACHE_NAME
            path.write_bytes(os.urandom(1 << 20))
            c = make(net, path)
            self.assertEqual(await c.lookup(), [PEER])
            c.save_cache()
            await c.close()
            self.assertLess(path.stat().st_size, cache.MAX_CACHE_BYTES)
            self.assertTrue(cache.load(path, allow_loopback=True))

    async def test_a_read_only_folder_does_not_fail_close(self):
        net = await self.network(n=20, info_hash=IH, peers=[PEER])
        c = make(net, Path("/nonexistent-folder-for-dht-test") / cache.CACHE_NAME)
        self.assertEqual(await c.lookup(), [PEER])
        c.save_cache()
        await c.close()

    async def test_without_a_cache_path_nothing_is_written(self):
        net = await self.network(n=20, info_hash=IH, peers=[PEER])
        c = make(net, None)
        await c.lookup()
        c.save_cache()
        await c.close()


class Resolve(unittest.IsolatedAsyncioTestCase):
    async def test_the_default_resolver_returns_ipv4_addresses_and_skips_failures(self):
        c = client.DhtClient(IH, [], None, lambda m: None)
        self.assertEqual(await c._resolve("127.0.0.1", 6881), [("127.0.0.1", 6881)])
        self.assertEqual(await c._resolve("nonexistent.invalid", 6881), [])


if __name__ == "__main__":
    unittest.main()
