"""The DHT client: iterative get_peers lookups over one unconnected UDP socket.

Read-only (BEP 43): it asks and never answers or announces. It never calls bind or listen: the OS picks the
port on the first send, as for a UDP tracker. Only replies to outstanding queries are accepted: the
transaction id must match, from the address it was sent to, before its timeout. Everything else is dropped.

Nothing here logs an address.
"""

import asyncio
import os
import socket
import time
from collections import OrderedDict

from ..tracker import in_thread
from . import cache as _cache
from . import lookup as _lookup
from .krpc import MAX_DATAGRAM, ErrorMsg, KrpcError, Node, parse_body, parse_header, query, usable
from .table import RoutingTable

SEND_RATE = 50  # datagrams per second, never more than one every 1/SEND_RATE s
RESOLVE_TIMEOUT = 5  # seconds per bootstrap host name
MAX_BOOTSTRAP = 16  # addresses kept from the bootstrap hosts
MAX_SEEDS = 16  # table and cache nodes a lookup starts from
MAX_CACHE_SEEDS = 8  # of those, the ones read from the cache file: it is attacker-influenced
ERROR_LIMIT = 4  # sends that all failed before a lookup gives up: the network is down
CACHE_PRIVACY_BITS = 8  # nodes sharing this many leading bits with the info-hash are not saved
MAX_ANSWERED = _cache.MAX_CACHE_NODES


class _Pending:
    __slots__ = ("addr", "lookup", "node", "timer")

    def __init__(self, addr, node, lk, timer):
        self.addr, self.node, self.lookup, self.timer = addr, node, lk, timer


class _Proto(asyncio.DatagramProtocol):
    def __init__(self, owner):
        self.owner = owner
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        self.owner._received(data, addr)

    def error_received(self, exc):
        # asyncio hands a failed sendto (and an ICMP error) here instead of raising
        self.owner._send_failed()

    def connection_lost(self, exc):
        # only if it is still the socket in use: the notice of an old socket can arrive after a reopen
        if self.owner._transport is self.transport:
            self.owner._transport = None


class DhtClient:
    def __init__(self, info_hash, bootstrap, cache_path, log, clock=time.monotonic, resolve=None,
                 allow_loopback=False, allow_private=False):
        self.info_hash = info_hash
        self.bootstrap = list(bootstrap)  # [(host, port)]
        self.cache_path = cache_path
        self.log = log
        self.clock = clock
        self.resolve = resolve or self._resolve
        self.allow_loopback = allow_loopback
        self.allow_private = allow_private
        self.on_peers = None  # called with each batch of new peer addresses
        self.stats = {"lookups": 0, "nodes": 0, "peers": 0, "sent": 0, "dropped": 0, "errors": 0,
                      "dropped_early": 0, "dropped_late": 0}  # before the bencode decode, and after it
        self.own_id = os.urandom(20)
        self.table = RoutingTable(self.own_id, clock)
        self._answered = OrderedDict()  # addr -> Node, the latest answer last
        self._transport = None
        self._proto = None
        self._closed = False
        self._attempts = 0  # sends in the running lookup
        self._failures = 0  # of those, the ones that failed
        self._pending = {}  # tid -> _Pending
        self._waiting = {}  # addr -> outstanding queries to it
        self._next_send = 0.0
        self._cached = None  # nodes read from the cache file, once
        self._boot = None  # Nodes made from the resolved bootstrap hosts
        self._wake = asyncio.Event()
        self._lock = asyncio.Lock()

    # sockets and datagrams

    async def _open(self):
        if self._transport is None and not self._closed:
            loop = asyncio.get_running_loop()
            transport, proto = await loop.create_datagram_endpoint(
                lambda: _Proto(self), family=socket.AF_INET)
            if self._closed:  # close() ran while the socket was being made
                transport.close()
                return
            self._transport, self._proto = transport, proto

    def _sendto(self, data, addr):
        if self._transport is None:
            raise OSError("the DHT socket is closed")
        self._transport.sendto(data, addr)

    def _send_failed(self):
        self.stats["errors"] += 1
        self._failures += 1

    async def _throttle(self):
        loop = asyncio.get_running_loop()
        now = loop.time()
        wait = self._next_send - now
        self._next_send = max(now, self._next_send) + 1 / SEND_RATE
        if wait > 0:
            await asyncio.sleep(wait)

    def _forget(self, tid):
        pending = self._pending.pop(tid, None)
        if pending is not None:
            pending.timer.cancel()
            left = self._waiting.get(pending.addr, 1) - 1
            if left > 0:
                self._waiting[pending.addr] = left
            else:
                self._waiting.pop(pending.addr, None)
        return pending

    def _expire(self, tid):
        pending = self._forget(tid)
        if pending is not None:
            pending.lookup.on_timeout(pending.node)
            self.table.failed(pending.addr)
            self._wake.set()

    async def _send_query(self, node, lk):
        if not usable(node.addr, self.allow_loopback, self.allow_private):
            lk.on_timeout(node)
            self._wake.set()
            return
        await self._throttle()
        if self._closed:
            return
        tid = os.urandom(8)
        while tid in self._pending:
            tid = os.urandom(8)
        self._attempts += 1
        try:
            self._sendto(query(tid, "get_peers", {"id": self.own_id, "info_hash": self.info_hash}), node.addr)
        except OSError:
            self._send_failed()
            lk.on_timeout(node)
            self.table.failed(node.addr)
            self._wake.set()
            return
        self.stats["sent"] += 1
        timer = asyncio.get_running_loop().call_later(_lookup.TIMEOUT, self._expire, tid)
        self._pending[tid] = _Pending(node.addr, node, lk, timer)
        self._waiting[node.addr] = self._waiting.get(node.addr, 0) + 1

    def _drop(self, early):
        self.stats["dropped"] += 1
        self.stats["dropped_early" if early else "dropped_late"] += 1

    def _received(self, data, addr):
        """A datagram arrived. It counts only as the one reply to an outstanding query.

        The cheap checks come first: size, the address we are waiting on, the decode, the transaction id.
        The nodes and values are validated only for a reply that passed them all.
        """
        if len(data) > MAX_DATAGRAM or addr not in self._waiting:
            self._drop(True)
            return
        try:
            head = parse_header(data)
        except KrpcError:
            self._drop(False)
            return
        pending = self._pending.get(head.tid)
        if head.kind == b"q" or pending is None or pending.addr != addr:  # read-only: nobody gets an answer
            self._drop(False)
            return
        try:
            msg = parse_body(head, self.allow_loopback, self.allow_private)
        except KrpcError:
            self._drop(False)
            return
        self._forget(msg.tid)
        if isinstance(msg, ErrorMsg):
            pending.lookup.on_timeout(pending.node)
            self.table.failed(addr)
            self._wake.set()
            return
        if self.table.seen(msg.id, addr) and (msg.nodes or msg.values):
            self._answered.pop(addr, None)
            self._answered[addr] = Node(msg.id, addr)
            if len(self._answered) > MAX_ANSWERED:
                self._answered.popitem(last=False)
            self.stats["nodes"] = len(self._answered)
        new = pending.lookup.on_reply(pending.node, msg, self.clock())
        if new:
            self.stats["peers"] += len(new)
            if self.on_peers is not None:
                try:
                    self.on_peers(new)
                except Exception as e:
                    self.log(f"dht: peer callback failed: {type(e).__name__}")
        self._wake.set()

    # bootstrap

    async def _resolve(self, host, port):
        loop = asyncio.get_running_loop()
        try:
            infos = await asyncio.wait_for(
                in_thread(loop, socket.getaddrinfo, host, port, socket.AF_INET, socket.SOCK_DGRAM),
                RESOLVE_TIMEOUT)
        except (TimeoutError, OSError):
            return []
        return [(info[4][0], info[4][1]) for info in infos]

    async def _resolve_one(self, host, port):
        try:
            return await asyncio.wait_for(self.resolve(host, port), RESOLVE_TIMEOUT)
        except Exception:  # a failed resolution is skipped silently
            return []

    async def _resolve_all(self):
        found = await asyncio.gather(*(self._resolve_one(h, p) for h, p in self.bootstrap))
        nodes, seen = [], set()
        # one address per host in turn, so a host with many addresses cannot crowd out the others
        for i in range(max(map(len, found), default=0)):
            for addrs in found:
                if i < len(addrs):
                    addr = addrs[i]
                    if addr not in seen and usable(addr, self.allow_loopback, self.allow_private) \
                            and len(nodes) < MAX_BOOTSTRAP:
                        seen.add(addr)
                        # the id is unknown: a placeholder until it answers
                        nodes.append(Node(self.info_hash, addr))
        return nodes

    def _seeds(self):
        if self._cached is None:
            self._cached = (_cache.load(self.cache_path, self.allow_loopback, self.allow_private)
                            if self.cache_path else [])
        # The cache file is attacker-influenced: a few of its nodes, and their claimed ids rank for nothing
        # (the farthest possible, until a node answers with its own id), so they never push bootstrap nodes
        # aside.
        far = bytes(b ^ 0xFF for b in self.info_hash)
        seeds = {n.addr: Node(far, n.addr) for n in self._cached[:MAX_CACHE_SEEDS]}
        seeds.update({n.addr: n for n in self.table.closest(self.info_hash, MAX_SEEDS)})
        t = int.from_bytes(self.info_hash, "big")
        return sorted(seeds.values(), key=lambda n: int.from_bytes(n.id, "big") ^ t)[:MAX_SEEDS]

    # lookups

    async def lookup(self):
        """One full get_peers lookup; the peers found (on_peers also gets them as they arrive).

        Returns at once, with what it has, once close() is called.
        """
        async with self._lock:
            if self._closed:
                return []
            return await self._lookup()

    async def _lookup(self):
        self.stats["lookups"] += 1
        try:
            await self._open()
        except OSError as e:
            self.stats["errors"] += 1
            self.log(f"dht: cannot open the socket: {type(e).__name__}")
            return []
        if self._closed:
            return []
        self._attempts = self._failures = 0
        lk = _lookup.Lookup(self.info_hash, self._seeds(), want_peers=True, own_id=self.own_id)
        wake = self._wake = asyncio.Event()
        resolving = None
        if self._boot:
            lk.add(self._boot)
        elif self.bootstrap:
            resolving = asyncio.ensure_future(self._resolve_all())
            resolving.add_done_callback(lambda _: wake.set())
        try:
            while not self._closed:
                wake.clear()
                if resolving is not None and resolving.done():
                    self._boot = resolving.result()
                    lk.add(self._boot)
                    resolving = None
                for node in lk.next_queries(self.clock()):
                    await self._send_query(node, lk)
                    if self._closed or (self._attempts >= ERROR_LIMIT and self._failures >= self._attempts):
                        return list(lk.peers)  # closed, or every send so far failed: the network is down
                # with no peers yet, a dead end is not the end until the bootstrap hosts have been tried
                if lk.done() and (resolving is None or lk.peers):
                    break
                await wake.wait()
        finally:
            if resolving is not None:
                resolving.cancel()
                await asyncio.gather(resolving, return_exceptions=True)
            for tid in [t for t, p in self._pending.items() if p.lookup is lk]:
                self._forget(tid)
        return list(lk.peers)

    def save_cache(self):
        """Write the cache from the nodes that answered, latest first. True when it was written.

        Only after a run that found peers: a run that found none may have seen only a hostile or cut-off
        corner of the network, and must not replace a good cache. Nodes near the info-hash are left out:
        the file must not say what was downloaded.
        """
        if not self.cache_path or not self.stats["peers"]:
            return False
        target = int.from_bytes(self.info_hash, "big")
        keep = [n for n in reversed(self._answered.values())
                if (int.from_bytes(n.id, "big") ^ target).bit_length() > 160 - CACHE_PRIVACY_BITS]
        if not keep:
            return False
        _cache.save(self.cache_path, keep)
        return True

    async def close(self):
        """Stop: a running lookup ends at once, and the socket is closed. Does not write the cache."""
        self._closed = True
        for tid in list(self._pending):
            self._forget(tid)
        self._wake.set()
        if self._transport is not None:
            transport, self._transport = self._transport, None
            transport.close()
