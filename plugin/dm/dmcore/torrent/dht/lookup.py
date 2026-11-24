"""An iterative lookup toward a target id, as a pure state machine (no sockets, no clock).

The caller asks next_queries() what to send, then reports each outcome with on_reply() or on_timeout().
"""

from .krpc import Node
from .table import K

ALPHA = 8  # queries in flight
MAX_QUERIES = 300  # sent in one lookup
TIMEOUT = 2.0  # seconds a query may stay unanswered; enforced by the caller
MAX_PEERS = 1000  # peer values kept from one lookup
MAX_CANDIDATES = 256  # nodes remembered; the farthest are forgotten first


class Lookup:
    def __init__(self, target, seeds, *, want_peers, own_id=None):
        self.target = target
        self.own_id = own_id
        self._t = int.from_bytes(target, "big")
        self.want_peers = want_peers
        self.sent = 0
        self.inflight = {}  # addr -> time sent
        self.answered = []  # [Node], in answer order
        self.peers = []
        self._peer_set = set()
        self._cands = {}  # addr -> Node
        self._queried = set()
        self._answered = set()
        self._failed = set()
        self.add(seeds)

    def _distance(self, node):
        return int.from_bytes(node.id, "big") ^ self._t

    def add(self, nodes):
        for node in nodes:
            if node.addr not in self._cands:
                self._cands[node.addr] = Node(*node)
        if len(self._cands) > MAX_CANDIDATES:  # _queried remembers what was asked, so forgetting is safe
            far = sorted(self._cands.values(), key=self._distance, reverse=True)
            for node in far[:len(self._cands) - MAX_CANDIDATES]:
                del self._cands[node.addr]

    def _closest(self):
        """The K closest candidates that have not failed."""
        live = (n for a, n in self._cands.items() if a not in self._failed)
        return sorted(live, key=self._distance)[:K]

    def next_queries(self, now):
        """The nodes to query now: the unqueried ones among the K closest, up to ALPHA in flight."""
        room = min(ALPHA - len(self.inflight), MAX_QUERIES - self.sent)
        if room <= 0:
            return []
        out = [n for n in self._closest() if n.addr not in self._queried][:room]
        for node in out:
            self._queried.add(node.addr)
            self.inflight[node.addr] = now
        self.sent += len(out)
        return out

    def on_reply(self, node, reply, now):
        """A valid reply from node. Returns the peer values it adds.

        Replies from nodes not queried count for nothing, and so does one that claims our own id.
        """
        if self.inflight.pop(node.addr, None) is None:
            return []
        if reply.id == self.own_id:  # nobody answers with our id but a liar
            self._failed.add(node.addr)
            return []
        self._answered.add(node.addr)
        real = Node(reply.id, node.addr)
        self._cands[node.addr] = real
        self.answered.append(real)
        self.add(Node(*n) for n in reply.nodes)
        new = []
        if self.want_peers:
            for peer in reply.values:
                if len(self.peers) >= MAX_PEERS:
                    break
                if peer not in self._peer_set:
                    self._peer_set.add(peer)
                    self.peers.append(peer)
                    new.append(peer)
        return new

    def on_timeout(self, node):
        if self.inflight.pop(node.addr, None) is not None:
            self._failed.add(node.addr)

    def done(self):
        if self.inflight:
            return False
        if self.sent >= MAX_QUERIES:
            return True
        return all(n.addr in self._answered for n in self._closest())
