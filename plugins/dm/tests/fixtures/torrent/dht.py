"""A fake BitTorrent DHT for the dm tests: UDP nodes that answer ping, find_node and get_peers (stdlib only).

In-process: `net = DhtNetwork(n=50); await net.start("127.0.0.1")`, then read `.addrs` (every node's
address) and `.queries` (every query received: node, method, ro, src, ...). `net.plant(info_hash, peers)`
makes the 8 nodes closest to the hash hold those peers; `announce_peer` stores too. Every node has a real
routing table (dmcore's) filled with the whole network.

Hostile nodes: `DhtNetwork(modes={index: mode})` or `net.hostile(mode, count)` (which spares the
holders of planted hashes). Modes:
  silent       never answers
  oversize     a valid reply padded past 1500 bytes
  garbage      random bytes
  deep         a valid reply with a list nested 100 deep
  flood        the honest reply plus 300 forged replies with random transaction ids
  self         names itself as the only closer node
  bad-addrs    names 0.0.0.0, 255.255.255.255, multicast and port-0 nodes
  many-nodes   names 55 nodes on closed loopback ports
  many-values  answers get_peers with 170 peer values
  dup-replies  sends the honest reply 50 times
  spoof        sends the reply from another port
  wrong-tid    sends the reply with another transaction id
  late         sends the reply `late_delay` seconds late
  empty        answers every query with its id only: no nodes, no values
  private      names private addresses (10/8, 172.16/12, 192.168/16, 169.254/16, 100.64/10, 240/4, 198.18/15)
               as both closer nodes and peer values
`probe=True` makes each node send ping, find_node, get_peers and announce_peer queries back to whoever
queried it; any reply to them is recorded in `.dm_replies`.

In a container:
`python3 dht.py --nodes 50 --port-base 7000 --advertise <ip>|auto --peers <ip:port,...> --info-hash <hex>`
(needs dmcore on PYTHONPATH, or run from the repository). `--advertise auto` names the container's own
address. It prints `dht nodes: <ip:port> ...` when ready, and `announce <info-hash prefix>` on each announce_peer
it stores.
"""

import argparse
import asyncio
import hashlib
import os
import random
import socket
import struct
import sys
import time

try:
    from dmcore.torrent import bencode
    from dmcore.torrent.dht.table import RoutingTable
except ImportError:  # run straight from the repository
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.path.join(here, "..", "..", ".."))
    from dmcore.torrent import bencode
    from dmcore.torrent.dht.table import RoutingTable

K = 8
MODES = (
    "silent",
    "oversize",
    "garbage",
    "deep",
    "flood",
    "self",
    "bad-addrs",
    "many-nodes",
    "many-values",
    "dup-replies",
    "spoof",
    "wrong-tid",
    "late",
    "empty",
    "private",
)
PRIVATE = [
    "10.0.0.1",
    "172.16.0.1",
    "192.168.1.1",
    "169.254.169.254",
    "100.64.0.1",
    "240.0.0.1",
    "198.18.0.1",
]


def compact_node(node_id, addr):
    return node_id + socket.inet_aton(addr[0]) + struct.pack(">H", addr[1])


def compact_peer(addr):
    return socket.inet_aton(addr[0]) + struct.pack(">H", addr[1])


def distance(a, b):
    return int.from_bytes(a, "big") ^ int.from_bytes(b, "big")


class _Node(asyncio.DatagramProtocol):
    def __init__(self, net, index, node_id, mode):
        self.net, self.index, self.id, self.mode = net, index, node_id, mode
        self.table = RoutingTable(node_id)
        self.store = {}  # info_hash -> {(ip, port)}
        self.transport = None
        self.alt = None
        self.probed = set()

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        net = self.net
        net.datagrams += 1
        try:
            msg, _ = bencode.decode(data)
            kind = msg[b"y"]
        except bencode.BencodeError, KeyError, TypeError:
            return
        if kind != b"q":
            net.dm_replies.append({"node": self.index, "src": addr})
            return
        args = msg.get(b"a") if isinstance(msg.get(b"a"), dict) else {}
        method = msg.get(b"q", b"").decode("latin-1")
        net.queries.append(
            {
                "node": self.index,
                "method": method,
                "ro": msg.get(b"ro"),
                "src": addr,
                "tid": msg.get(b"t"),
                "args": args,
                "time": time.monotonic(),
            }
        )
        if net.probe and addr not in self.probed:
            self.probed.add(addr)
            announce = {"info_hash": self.id, "port": 1, "token": b"x"}
            for m, a in (
                ("ping", {}),
                ("find_node", {"target": self.id}),
                ("get_peers", {"info_hash": self.id}),
                ("announce_peer", announce),
            ):
                body = {"id": self.id, **a}
                self.transport.sendto(bencode.encode({"t": b"pb", "y": b"q", "q": m, "a": body}), addr)
        body = self.answer(method, args, addr)
        if body is not None:
            self.respond(msg.get(b"t", b""), body, addr)

    def token(self, addr):
        return hashlib.sha1(self.net.secret + addr[0].encode()).digest()[:8]

    def answer(self, method, args, addr):
        body = {"id": self.id}
        if method == "ping":
            return body
        if method == "find_node":
            target = args.get(b"target", b"")
            if len(target) != 20:
                return None
            body["nodes"] = b"".join(compact_node(n.id, n.addr) for n in self.table.closest(target, K))
            return body
        if method == "get_peers":
            ih = args.get(b"info_hash", b"")
            if len(ih) != 20:
                return None
            body["token"] = self.token(addr)
            held = sorted(self.store.get(ih, ()))
            if held or self.mode == "many-values":
                if self.mode == "many-values":
                    held = [("198.51.%d.%d" % (100 + i // 250, i % 250 + 1), 6881) for i in range(170)]
                body["values"] = [compact_peer(p) for p in held]
            else:
                body["nodes"] = b"".join(compact_node(n.id, n.addr) for n in self.table.closest(ih, K))
            return body
        if method == "announce_peer":
            ih = args.get(b"info_hash", b"")
            if len(ih) == 20 and args.get(b"token") == self.token(addr):
                port = addr[1] if args.get(b"implied_port") else args.get(b"port", 0)
                self.store.setdefault(ih, set()).add((addr[0], port))
                self.net.announces.append({"info_hash": ih, "peer": (addr[0], port)})
                if self.net.verbose:
                    print("announce", ih.hex()[:8], flush=True)
            return body
        return None

    def respond(self, tid, body, addr):
        mode = self.mode
        if mode == "silent":
            return
        if mode == "self":
            body = {"id": self.id, "nodes": compact_node(self.id, self.net.addrs[self.index])}
        elif mode == "bad-addrs":
            rnd = self.net.rng
            bad = [
                ("0.0.0.0", 6881),
                ("255.255.255.255", 6881),
                ("224.0.0.1", 6881),
                ("1.2.3.4", 0),
                ("0.5.5.5", 80),
            ]
            body = {"id": self.id, "nodes": b"".join(compact_node(rnd.randbytes(20), a) for a in bad)}
        elif mode == "empty":
            body = {"id": self.id}
        elif mode == "private":
            rnd = self.net.rng
            body = {
                "id": self.id,
                "token": b"x",
                "nodes": b"".join(compact_node(rnd.randbytes(20), (ip, 6881)) for ip in PRIVATE),
                "values": [compact_peer((ip, 51413)) for ip in PRIVATE],
            }
        elif mode == "many-nodes":
            rnd = self.net.rng
            body = {
                "id": self.id,
                "nodes": b"".join(
                    compact_node(rnd.randbytes(20), ("127.0.0.1", rnd.randrange(20000, 60000)))
                    for _ in range(55)
                ),
            }
        message = {"t": tid, "y": "r", "r": body}
        if mode == "oversize":
            message["zz"] = b"x" * 3000
        elif mode == "deep":
            message["zz"] = [[[[[[[[[[[[[[[[[[[[1]]]]]]]]]]]]]]]]]]]]
            data = bencode.encode(message)
            data = data.replace(b"l" * 20, b"l" * 120).replace(b"e" * 20, b"e" * 120)
            self.transport.sendto(data, addr)
            return
        elif mode == "wrong-tid":
            message["t"] = bytes(b ^ 0xFF for b in tid)
        data = os.urandom(300) if mode == "garbage" else bencode.encode(message)
        if mode == "spoof":
            self.alt.sendto(data, addr)
        elif mode == "late":
            self.net.timers.append(
                asyncio.get_running_loop().call_later(self.net.late_delay, self.transport.sendto, data, addr)
            )
        elif mode == "dup-replies":
            for _ in range(50):
                self.transport.sendto(data, addr)
        elif mode == "flood":
            self.transport.sendto(data, addr)
            for _ in range(300):
                self.transport.sendto(
                    bencode.encode({"t": self.net.rng.randbytes(4), "y": "r", "r": {"id": self.id}}), addr
                )
        else:
            self.transport.sendto(data, addr)


class DhtNetwork:
    def __init__(
        self, n=50, modes=None, info_hash=None, peers=(), probe=False, late_delay=0.5, seed=1, verbose=False
    ):
        self.rng = random.Random(seed)
        self.secret = self.rng.randbytes(8)
        self.probe = probe
        self.verbose = verbose
        self.late_delay = late_delay
        self.modes = dict(modes or {})
        self.n = n
        self.nodes = []
        self.addrs = []
        self.queries = []
        self.announces = []
        self.dm_replies = []
        self.datagrams = 0
        self.timers = []  # call_later handles of `late` replies; stop() cancels them
        self.planted = {}
        self.plan = (info_hash, list(peers))
        self._transports = []

    @property
    def ids(self):
        return [node.id for node in self.nodes]

    async def start(self, host="127.0.0.1", port_base=0, advertise=None):
        loop = asyncio.get_running_loop()
        for i in range(self.n):
            node = _Node(self, i, self.rng.randbytes(20), self.modes.get(i))
            port = port_base + i if port_base else 0
            transport, _ = await loop.create_datagram_endpoint(
                lambda node=node: node, local_addr=(host, port)
            )
            alt, _ = await loop.create_datagram_endpoint(asyncio.DatagramProtocol, local_addr=(host, 0))
            node.alt = alt
            self._transports += [transport, alt]
            self.nodes.append(node)
            self.addrs.append((advertise or host, transport.get_extra_info("sockname")[1]))
        for node in self.nodes:
            for other in self.nodes:
                if other is not node:
                    node.table.seen(other.id, self.addrs[other.index])
        if self.plan[0]:
            self.plant(*self.plan)
        return self

    def stop(self):
        for handle in self.timers:
            handle.cancel()
        for transport in self._transports:
            transport.close()
        self._transports = []

    def holders(self, info_hash):
        """Indexes of the K nodes closest to info_hash."""
        order = sorted(range(self.n), key=lambda i: distance(self.nodes[i].id, info_hash))
        return order[:K]

    def plant(self, info_hash, peers):
        self.planted[info_hash] = list(peers)
        for i in self.holders(info_hash):
            self.nodes[i].store.setdefault(info_hash, set()).update(peers)

    def hostile(self, mode, count):
        """Make `count` random nodes (never a holder of a planted hash) hostile; their indexes."""
        spared = {i for ih in self.planted for i in self.holders(ih)}
        chosen = self.rng.sample([i for i in range(self.n) if i not in spared], count)
        for i in chosen:
            self.nodes[i].mode = mode
        return chosen

    def flood(self, addr, count=10000, big=False):
        """Spray `count` forged replies (random transaction ids) at addr from every node's socket.

        big=True makes each a maximum-size reply (16 nodes, 100 values), the dearest kind to validate.
        """
        for i in range(count):
            body = {"id": self.nodes[i % self.n].id}
            if big:
                body["nodes"] = b"".join(
                    compact_node(self.rng.randbytes(20), ("8.%d.%d.1" % (j, i % 250 + 1), 6881))
                    for j in range(16)
                )
                body["values"] = [
                    compact_peer(("9.%d.%d.%d" % (j // 250, j % 250 + 1, i % 250 + 1), 51413))
                    for j in range(100)
                ]
                body["token"] = b"t" * 8
            data = bencode.encode({"t": self.rng.randbytes(8), "y": "r", "r": body})
            self._transports[i % len(self._transports)].sendto(data, addr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--nodes", type=int, default=50)
    ap.add_argument("--port-base", type=int, default=7000)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--advertise", default=None, help="the address the nodes name in their replies")
    ap.add_argument("--peers", default="", help="ip:port,ip:port")
    ap.add_argument("--info-hash", default="")
    args = ap.parse_args()
    peers = [(p.rsplit(":", 1)[0], int(p.rsplit(":", 1)[1])) for p in args.peers.split(",") if p]
    info_hash = bytes.fromhex(args.info_hash) if args.info_hash else None
    net = DhtNetwork(n=args.nodes, info_hash=info_hash, peers=peers, verbose=True)

    async def run():
        advertise = socket.gethostbyname(socket.gethostname()) if args.advertise == "auto" else args.advertise
        await net.start(args.host, args.port_base, advertise)
        print("dht nodes:", " ".join(f"{h}:{p}" for h, p in net.addrs), flush=True)
        await asyncio.Event().wait()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
