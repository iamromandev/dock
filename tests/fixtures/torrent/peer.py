"""Test peers for the dm torrent tests (stdlib only; they use dmcore's own wire module).

Seeder: serves complete torrents from disk. Fault modes: corrupt=<piece or set of pieces> (flips a byte of
them when served), unrequested (sends a block nobody asked for right after the unchoke), never_unchoke,
delay=<seconds per block>. `piece_requests` counts the block requests per piece.
Leecher: has nothing. It connects out to a seeder (`connect`) or accepts a connection (`serve`), asks for
pieces, checks them against the torrent's hashes and records the ones it got in `.pieces`.

In-process: build one, `await start(host)`, read `.port`. In a container: `python3 peer.py seeder --torrent T
--data DIR [--corrupt N] [--unrequested] [--never-unchoke] [--delay S] [--port 6881]` (dmcore on PYTHONPATH).
"""

import argparse
import asyncio
import collections
import hashlib
import os
import random
import sys

try:
    from dmcore.torrent import metainfo, tracker, wire
except ImportError:  # run straight from the repository
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.path.join(here, "..", "..", "..", "plugin", "dm"))
    from dmcore.torrent import metainfo, tracker, wire

BLOCK = wire.MAX_BLOCK


def peer_id(tag=b"SD"):
    return b"-" + tag + b"0001-" + bytes(random.getrandbits(8) for _ in range(12))


class _Content:
    """A torrent's payload on disk: root/<name> is the file, or the folder holding the files."""

    def __init__(self, meta, root):
        self.meta = meta
        base = os.path.join(root, meta.name)
        self.files, offset = [], 0
        for entry in meta.files:
            path = os.path.join(base, *entry.path) if meta.multi else base
            self.files.append((path, entry.length, offset))
            offset += entry.length

    def read(self, index, begin, length):
        start = index * self.meta.piece_length + begin
        out = []
        for path, size, offset in self.files:
            if start < offset + size and length > 0:
                n = min(length, offset + size - start)
                with open(path, "rb") as f:
                    f.seek(start - offset)
                    out.append(f.read(n))
                start += n
                length -= n
        return b"".join(out)


def _hs_info_hash(head):
    if head[:20] != bytes([19]) + wire.PROTOCOL:
        raise ValueError("not BitTorrent")
    return head[28:48]


class Seeder:
    def __init__(self, corrupt=None, unrequested=False, never_unchoke=False, delay=0.0):
        self.corrupt = corrupt
        self.unrequested = unrequested
        self.never_unchoke = never_unchoke
        self.delay = delay
        self.peer_id = peer_id(b"SD")
        self.content = {}  # info_hash -> _Content
        self.port = None
        self.connections = 0  # accepted
        self.open_connections = 0
        self.requests = 0
        self.piece_requests = collections.Counter()  # piece index -> block requests served
        self.closed = 0
        self.writers = set()
        self._server = None

    def add(self, torrent_path, data_root):
        meta = metainfo.load(torrent_path)
        self.content[meta.info_hash] = _Content(meta, data_root)
        return meta

    async def start(self, host="127.0.0.1", port=0):
        self._server = await asyncio.start_server(self._serve, host, port)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def announce(self):
        """Tell each torrent's first tracker we are here (listening on self.port, nothing left to get)."""
        for ih, c in self.content.items():
            for tier in c.meta.trackers:
                try:
                    req = tracker.Announce(ih, self.peer_id, self.port, 0, 0, 0, "started")
                    await tracker.announce(tier[0], req)
                    break
                except tracker.TrackerError as e:
                    print(f"announce failed: {e}", flush=True)

    async def stop(self):
        if self._server:
            self._server.close()
        for w in list(self.writers):
            w.close()
        if self._server:
            await self._server.wait_closed()

    def _corrupt(self, index):
        if self.corrupt is None:
            return False
        return index == self.corrupt if isinstance(self.corrupt, int) else index in self.corrupt

    async def _serve(self, reader, writer):
        self.connections += 1
        self.open_connections += 1
        self.writers.add(writer)
        try:
            head = await asyncio.wait_for(reader.readexactly(68), 10)
            content = self.content.get(_hs_info_hash(head))
            if content is None:
                return
            meta = content.meta
            writer.write(wire.handshake(meta.info_hash, self.peer_id))
            n = len(meta.hashes)
            bits = bytearray(-(-n // 8))
            for i in range(n):
                bits[i // 8] |= 0x80 >> (i % 8)
            writer.write(wire.bitfield(bits))
            await writer.drain()
            unchoked = False
            while True:
                got = await wire.read_message(reader, n)
                if got is None:
                    continue
                mid, payload = got
                if mid == wire.INTERESTED and not unchoked and not self.never_unchoke:
                    unchoked = True
                    writer.write(wire.msg(wire.UNCHOKE))
                    if self.unrequested:  # a block at an offset dm never asks for
                        writer.write(wire.piece(0, 5, b"\x00" * 16))
                    await writer.drain()
                elif mid == wire.REQUEST and unchoked:
                    index, begin, length = wire.parse_request(payload)
                    self.requests += 1
                    self.piece_requests[index] += 1
                    if self.delay:
                        await asyncio.sleep(self.delay)
                    data = content.read(index, begin, length)
                    if self._corrupt(index) and data:
                        data = bytes([data[0] ^ 0xFF]) + data[1:]
                    writer.write(wire.piece(index, begin, data))
                    await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.TimeoutError, ValueError, OSError):
            pass
        finally:
            self.open_connections -= 1
            self.closed += 1
            self.writers.discard(writer)
            writer.close()


class Leecher:
    """Wants every piece of one torrent; records verified pieces in .pieces (index -> bytes)."""

    def __init__(self, torrent_path):
        self.meta = metainfo.load(torrent_path)
        self.peer_id = peer_id(b"LC")
        self.pieces = {}
        self.port = None
        self.connections = 0
        self.writers = set()
        self._server = None
        self._tasks = set()

    async def start(self, host="127.0.0.1", port=0):
        self._server = await asyncio.start_server(lambda r, w: self._run(r, w, False), host, port)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def announce(self):
        for tier in self.meta.trackers:
            try:
                await tracker.announce(tier[0], tracker.Announce(
                    self.meta.info_hash, self.peer_id, self.port, 0, 0, self.meta.total, "started"))
                break
            except tracker.TrackerError:
                continue

    async def connect(self, host, port):
        reader, writer = await asyncio.open_connection(host, port)
        task = asyncio.ensure_future(self._run(reader, writer, True))
        self._tasks.add(task)
        return task

    async def stop(self):
        if self._server:
            self._server.close()
        for w in list(self.writers):
            w.close()
        for t in self._tasks:
            t.cancel()

    async def wait_pieces(self, count, timeout=20):
        end = asyncio.get_running_loop().time() + timeout
        while len(self.pieces) < count:
            if asyncio.get_running_loop().time() > end:
                return False
            await asyncio.sleep(0.02)
        return True

    async def _run(self, reader, writer, outbound):
        meta = self.meta
        self.connections += 1
        self.writers.add(writer)
        n = len(meta.hashes)
        try:
            if outbound:
                writer.write(wire.handshake(meta.info_hash, self.peer_id))
            await wire.read_handshake(reader, meta.info_hash)
            if not outbound:
                writer.write(wire.handshake(meta.info_hash, self.peer_id))
            writer.write(wire.msg(wire.INTERESTED))
            await writer.drain()
            has, unchoked, asked, buf = set(), False, False, {}

            def piece_len(i):
                return min(meta.piece_length, meta.total - i * meta.piece_length)

            async def ask():
                nonlocal asked
                want = sorted(i for i in has if i not in self.pieces and i not in buf)
                if asked or not unchoked or not want:
                    return
                i = want[0]
                buf[i] = {}
                asked = True
                for begin in range(0, piece_len(i), BLOCK):
                    writer.write(wire.request(i, begin, min(BLOCK, piece_len(i) - begin)))
                await writer.drain()

            while True:
                got = await wire.read_message(reader, n)
                if got is None:
                    continue
                mid, payload = got
                if mid == wire.BITFIELD:
                    has.update(wire.parse_bitfield(payload, n))
                elif mid == wire.HAVE:
                    has.add(wire.parse_have(payload))
                elif mid == wire.UNCHOKE:
                    unchoked = True
                elif mid == wire.CHOKE:
                    unchoked = False
                elif mid == wire.PIECE:
                    index, begin, data = wire.parse_piece(payload)
                    if index in buf:
                        buf[index][begin] = data
                        if sum(len(d) for d in buf[index].values()) >= piece_len(index):
                            blob = b"".join(buf[index][b] for b in sorted(buf[index]))
                            del buf[index]
                            asked = False
                            if hashlib.sha1(blob).digest() == meta.hashes[index]:
                                self.pieces[index] = blob
                await ask()
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.TimeoutError, ValueError, OSError,
                asyncio.CancelledError):
            pass
        finally:
            self.writers.discard(writer)
            writer.close()


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("role", choices=["seeder"])
    ap.add_argument("--torrent", action="append", required=True)
    ap.add_argument("--data", required=True)
    ap.add_argument("--port", type=int, default=6881)
    ap.add_argument("--corrupt", type=int)
    ap.add_argument("--unrequested", action="store_true")
    ap.add_argument("--never-unchoke", action="store_true")
    ap.add_argument("--delay", type=float, default=0.0)
    ap.add_argument("--max-seconds", type=float, default=900)
    args = ap.parse_args()
    seeder = Seeder(args.corrupt, args.unrequested, args.never_unchoke, args.delay)
    for t in args.torrent:
        seeder.add(t, args.data)
    await seeder.start("0.0.0.0", args.port)
    await seeder.announce()
    print(f"ready port={seeder.port}", flush=True)
    await asyncio.sleep(args.max_seconds)


if __name__ == "__main__":
    asyncio.run(main())
