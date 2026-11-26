"""The routing table: nodes that answered us, in k-buckets by distance from our own id.

A node enters only through seen(), after a valid reply. A full bucket never pings: a newcomer replaces
the oldest node that has failed twice, or is dropped.
"""

import time

from .krpc import Node

K = 8
MAX_TABLE = 1000
MAX_FAILS = 2


class _Entry:
    __slots__ = ("addr", "fails", "id", "last")

    def __init__(self, nid, addr, last):
        self.id = nid
        self.addr = addr
        self.fails = 0
        self.last = last


class RoutingTable:
    def __init__(self, own_id, clock=time.monotonic):
        self.own_id = own_id
        self._own = int.from_bytes(own_id, "big")
        self._clock = clock
        self._buckets = {}  # prefix length shared with our id -> [_Entry]
        self._by_addr = {}  # addr -> _Entry
        self._ids = set()

    def __len__(self):
        return len(self._by_addr)

    def _bucket(self, nid):
        return 160 - (int.from_bytes(nid, "big") ^ self._own).bit_length()

    def seen(self, node_id, addr):
        """A valid reply came from addr, claiming node_id.

        False when the claim is refused: our own id, a malformed id, an address that changes its id, or an id
        already held for another address. True otherwise, also when a full bucket or table has no room for it.
        """
        if not isinstance(node_id, bytes) or len(node_id) != 20 or node_id == self.own_id:
            return False
        entry = self._by_addr.get(addr)
        if entry is not None:
            if entry.id != node_id:
                return False  # an address that changes its id keeps the first
            entry.last = self._clock()
            entry.fails = 0
            return True
        if node_id in self._ids:
            return False  # the same id from a second address keeps the first
        bucket = self._buckets.setdefault(self._bucket(node_id), [])
        if len(bucket) >= K:
            stale = [e for e in bucket if e.fails >= MAX_FAILS]
            if not stale:
                return True
            self._remove(bucket, min(stale, key=lambda e: e.last))
        elif len(self._by_addr) >= MAX_TABLE:
            return True
        entry = _Entry(node_id, addr, self._clock())
        bucket.append(entry)
        self._by_addr[addr] = entry
        self._ids.add(node_id)
        return True

    def _remove(self, bucket, entry):
        bucket.remove(entry)
        del self._by_addr[entry.addr]
        self._ids.discard(entry.id)

    def failed(self, addr):
        """A query to addr timed out."""
        entry = self._by_addr.get(addr)
        if entry is not None:
            entry.fails += 1

    def closest(self, target, n=K):
        t = int.from_bytes(target, "big")
        found = sorted(self._by_addr.values(), key=lambda e: int.from_bytes(e.id, "big") ^ t)
        return [Node(e.id, e.addr) for e in found[:n]]

    def recent(self, n):
        """The n nodes that answered most recently, latest first."""
        found = sorted(self._by_addr.values(), key=lambda e: e.last, reverse=True)
        return [Node(e.id, e.addr) for e in found[:n]]
