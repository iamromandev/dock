"""Test tracker for the dm torrent tests: HTTP and UDP (BEP 15) on one swarm table (stdlib only).

In-process: `await Tracker(mode=...).start("127.0.0.1")` on any asyncio loop, then read `.http_port`,
`.udp_port` and `.announces`. In a container: `python3 tracker.py --http-port 8000 --udp-port 8001`
(needs dmcore on PYTHONPATH).

Modes: compact (default), dict (peer list of dictionaries), fail (failure reason / UDP error), empty (no
peers).
`drop=N` makes the UDP side ignore its first N datagrams. `peers=[(ip, port), ...]` replaces the swarm table.
"""

import argparse
import asyncio
import os
import random
import socket
import struct
import sys
import time
from urllib.parse import unquote_to_bytes

try:
    from dmcore.torrent import bencode
except ImportError:  # run straight from the repository
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.path.join(here, "..", "..", "..", "plugin", "dm"))
    from dmcore.torrent import bencode

FAIL_REASON = "tracker says no"
EVENTS = {0: "", 1: "completed", 2: "started", 3: "stopped"}


def _pack(host, port, v6=False):
    if ":" in host:
        return socket.inet_pton(socket.AF_INET6, host) + struct.pack(">H", port)
    if v6:
        return socket.inet_pton(socket.AF_INET6, "::ffff:" + host) + struct.pack(">H", port)
    return socket.inet_aton(host) + struct.pack(">H", port)


class Tracker:
    def __init__(self, mode="compact", peers=None, drop=0, interval=60, reason=FAIL_REASON, delay=None):
        self.mode = mode
        self.static = list(peers) if peers is not None else None
        self.drop = drop
        self.interval = interval
        self.reason = reason
        self.delay = dict(delay or {})  # event -> seconds the HTTP reply to that event waits
        self.swarm = {}  # info_hash -> set((ip, port))
        self.announces = []  # one dict per accepted announce
        self.datagrams = 0
        self.http_port = self.udp_port = None
        self._server = None
        self._transport = None
        self._conn_ids = set()

    # shared logic

    def _peers_for(self, info_hash, me):
        if self.mode == "empty":
            return []
        if self.static is not None:
            return list(self.static)
        return sorted(p for p in self.swarm.get(info_hash, ()) if p != me)

    def _record(self, proto, info_hash, peer_id, ip, port, left, event, extra=None):
        self.announces.append({"proto": proto, "info_hash": info_hash, "peer_id": peer_id, "ip": ip,
                               "port": port, "left": left, "event": event, "time": time.monotonic(),
                               **(extra or {})})
        peers = self.swarm.setdefault(info_hash, set())
        if event == "stopped":
            peers.discard((ip, port))
        else:
            peers.add((ip, port))

    # HTTP

    async def _http(self, reader, writer):
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
            line = head.split(b"\r\n", 1)[0].decode("latin-1")
            target = line.split(" ")[1]
            path, _, query = target.partition("?")
            params = {}
            for pair in query.split("&"):
                key, _, value = pair.partition("=")
                params[unquote_to_bytes(key).decode("latin-1")] = unquote_to_bytes(value)
            ip = writer.get_extra_info("peername")[0]
            if path != "/announce":
                body, status = b"not found", "404 Not Found"
            else:
                body, status = self._http_body(params, ip, query), "200 OK"
                await asyncio.sleep(self.delay.get(params.get("event", b"").decode(), 0))
            writer.write(f"HTTP/1.0 {status}\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body)
            await writer.drain()
        except (asyncio.TimeoutError, ConnectionError, asyncio.IncompleteReadError, ValueError, IndexError):
            pass
        finally:
            writer.close()

    def _http_body(self, params, ip, query):
        info_hash = params.get("info_hash", b"")
        port = int(params.get("port", b"0") or 0)
        event = params.get("event", b"").decode()
        left = int(params.get("left", b"0") or 0)
        self._record("http", info_hash, params.get("peer_id", b""), ip, port, left, event,
                     {"query": query, "compact": params.get("compact", b"") == b"1"})
        if self.mode == "fail":
            return bencode.encode({"failure reason": self.reason})
        peers = self._peers_for(info_hash, (ip, port))
        if self.mode == "dict":
            plist = [{"ip": host.encode(), "port": p, "peer id": b"-XX0000-" + b"0" * 12}
                     for host, p in peers]
        else:
            plist = b"".join(socket.inet_aton(host) + struct.pack(">H", p) for host, p in peers)
        return bencode.encode({"interval": self.interval, "peers": plist})

    # UDP (BEP 15)

    def datagram_received(self, data, addr):
        self.datagrams += 1
        if self.datagrams <= self.drop:
            return
        if len(data) < 16:
            return
        conn, action, txid = struct.unpack(">QII", data[:16])
        if action == 0:
            if conn != 0x41727101980:
                return
            cid = random.getrandbits(63) + 1
            self._conn_ids.add(cid)
            self._transport.sendto(struct.pack(">IIQ", 0, txid, cid), addr)
        elif action == 1 and conn in self._conn_ids and len(data) >= 98:
            info_hash, peer_id = data[16:36], data[36:56]
            left, event, port = struct.unpack(">Q", data[64:72])[0], struct.unpack(">I", data[80:84])[0], \
                struct.unpack(">H", data[96:98])[0]
            event = EVENTS.get(event, "")
            self._record("udp", info_hash, peer_id, addr[0], port, left, event)
            if self.mode == "fail":
                self._transport.sendto(struct.pack(">II", 3, txid) + self.reason.encode(), addr)
                return
            peers = self._peers_for(info_hash, (addr[0], port))
            v6 = ":" in addr[0]  # BEP 15: an IPv6 announce gets 18-byte records
            body = b"".join(_pack(host, p, v6) for host, p in peers)
            self._transport.sendto(struct.pack(">IIIII", 1, txid, self.interval, 0, len(peers)) + body, addr)

    # lifecycle

    async def start(self, host="127.0.0.1", http_port=0, udp_port=0):
        self._server = await asyncio.start_server(self._http, host, http_port)
        self.http_port = self._server.sockets[0].getsockname()[1]
        outer = self

        class Proto(asyncio.DatagramProtocol):
            def datagram_received(self, data, addr):
                outer.datagram_received(data, addr)

        self._transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
            Proto, local_addr=(host, udp_port))
        self.udp_port = self._transport.get_extra_info("sockname")[1]
        return self

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        if self._transport:
            self._transport.close()


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--http-port", type=int, default=8000)
    ap.add_argument("--udp-port", type=int, default=8001)
    ap.add_argument("--mode", default="compact")
    ap.add_argument("--max-seconds", type=float, default=900)
    args = ap.parse_args()
    tracker = await Tracker(mode=args.mode).start("0.0.0.0", args.http_port, args.udp_port)
    print(f"ready http={tracker.http_port} udp={tracker.udp_port}", flush=True)
    await asyncio.sleep(args.max_seconds)


if __name__ == "__main__":
    asyncio.run(main())
