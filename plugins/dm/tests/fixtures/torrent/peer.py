"""Test peers for the dm torrent tests (stdlib only; they use dmcore's own wire module).

Seeder: serves complete torrents from disk. Fault modes: corrupt=<piece or set of pieces> (flips a byte of
them when served), unrequested (sends a block nobody asked for right after the unchoke), never_unchoke,
delay=<seconds per block>. `piece_requests` counts the block requests per piece.
Metadata (BEP 9/10): `metadata="honest"` (default) advertises ut_metadata and serves the info dictionary;
"silent" advertises it and never answers; "none" speaks the extension protocol without ut_metadata;
"corrupt" serves every block with its first byte flipped. Hostile ones: "size0" announces metadata_size 0,
"huge" 64 MiB, "short" answers with blocks one byte short, "extra" follows each block with unsolicited ones,
"junk" announces just under the 10 MiB maximum and serves right-length blocks that hash to nothing.
"sybil" announces `sybil_size` and answers one request with a right-length junk block, then hangs up.
`ext_delay=<s>` waits before our extension handshake. `metadata_delay=<s>` waits before each answer.
`ext_body=<bytes>` replaces our extension handshake (to send an odd one); `ext_noise=<n>` follows it with
an extension message of n bytes.
`metadata_requests` counts the ut_metadata requests, `metadata_pieces` them per block index.
MetadataProbe: asks dm for its metadata (see its docstring).
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
    from dmcore.torrent import bencode, metadata, metainfo, tracker, wire
except ImportError:  # run straight from the repository
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.path.join(here, "..", "..", ".."))
    from dmcore.torrent import bencode, metadata, metainfo, tracker, wire

BLOCK = wire.MAX_BLOCK
HUGE = 64 * 1024 * 1024  # a metadata_size no honest torrent has
JUNK = 10 * 1024 * 1024 - 2 * BLOCK  # just under the biggest metadata dm accepts, leaving room for an honest two blocks
_ANSWERING = ("honest", "corrupt", "short", "extra", "junk", "sybil")  # modes that answer metadata requests


def peer_id(tag=b"SD"):
    return b"-" + tag + b"0001-" + bytes(random.getrandbits(8) for _ in range(12))


class _Content:
    """A torrent's payload on disk: root/<name> is the file, or the folder holding the files."""

    def __init__(self, meta, root, info=b""):
        self.meta = meta
        self.info = info  # the bencoded info dictionary, as ut_metadata serves it
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
    def __init__(self, corrupt=None, unrequested=False, never_unchoke=False, delay=0.0, metadata="honest",
                 ext_body=None, ext_noise=0, metadata_delay=0.0, sybil_size=BLOCK * 2, ext_delay=0.0,
                 pex_peers=None, pex_mode="normal", pex_id=1, pex_advertise=True):
        # pex_peers: [(host, port)] sent to dm in one ut_pex after the extension handshake (None: we do not
        # speak ut_pex). pex_mode: "normal", "twice" (the same message again at once), "flood" (1000 addresses
        # made up from pex_peers' host), "garbage" (not bencode), "early" (before the handshake, then a valid one after it),
        # "extra" (added plus added6 and dropped). pex_id: the id we claim for ut_pex; pex_advertise False: we
        # send ut_pex messages without listing ut_pex in our handshake.
        self.pex_peers = pex_peers
        self.pex_mode = pex_mode
        self.pex_id = pex_id
        self.pex_advertise = pex_advertise
        self.pex_sent = 0  # ut_pex messages we sent dm
        self.dm_ids = []  # the "m" dictionaries of dm's extension handshakes
        self.pex_received = 0  # ut_pex messages dm sent us: always 0
        self.metadata_delay = metadata_delay
        self.sybil_size = sybil_size
        self.ext_delay = ext_delay
        self.metadata = metadata
        self.ext_body = ext_body
        self.ext_noise = ext_noise
        self.metadata_requests = 0
        self.metadata_pieces = collections.Counter()
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
        with open(torrent_path, "rb") as f:
            raw = f.read()
        start, end = bencode.decode(raw)[1]
        self.content[meta.info_hash] = _Content(meta, data_root, raw[start:end])
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

    def pex_message(self):
        """The ut_pex message for dm (it advertised ut_pex as id 4)."""
        self.pex_sent += 1
        if self.pex_mode == "garbage":
            return wire.extended(4, b"\xffnot bencode")
        peers = list(self.pex_peers)
        if self.pex_mode == "flood":
            host = peers[0][0]
            peers = [(host, 10000 + i) for i in range(1000)]
        added = b"".join(bytes(int(x) for x in h.split(".")) + p.to_bytes(2, "big") for h, p in peers)
        body = {"added": added, "added.f": bytes(len(peers))}
        if self.pex_mode == "extra":
            body["added6"] = bytes([1] * 16) + (4000).to_bytes(2, "big")
            body["dropped"] = bytes([9, 9, 9, 9]) + (5000).to_bytes(2, "big")
        return wire.extended(4, bencode.encode(body))

    def announced_size(self, content):
        """The metadata_size we claim: the real one, unless the mode lies about it."""
        if self.metadata == "size0":
            return 0
        if self.metadata == "huge":
            return HUGE
        if self.metadata == "junk":
            return JUNK
        if self.metadata == "sybil":
            return self.sybil_size
        return len(content.info)

    def block(self, content, piece, size):
        """The bytes we answer a request for `piece` with (empty: no such block), per mode."""
        if self.metadata in ("junk", "sybil"):  # a full-size set of the right block lengths and no meaning
            return b"j" * max(0, min(BLOCK, size - piece * BLOCK))
        block = content.info[piece * BLOCK:(piece + 1) * BLOCK]
        if self.metadata == "corrupt" and block:
            block = bytes([block[0] ^ 0xFF]) + block[1:]
        if self.metadata == "short" and block:  # one byte missing: not the length the size implies
            block = block[:-1]
        return block

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
            ext = wire.supports_extensions(head[20:28])
            their_ut = None
            if ext:
                if self.ext_delay:
                    await asyncio.sleep(self.ext_delay)
                ids = {"ut_metadata": 2} if self.metadata != "none" else {}
                if self.pex_peers is not None:
                    if self.pex_advertise:
                        ids["ut_pex"] = self.pex_id
                    if self.pex_mode == "early":
                        writer.write(self.pex_message())
                size = self.announced_size(content)
                writer.write(wire.extended(0, self.ext_body if self.ext_body is not None else bencode.encode(
                    {"m": ids, "metadata_size": size, "v": "test seeder"})))
                if self.ext_noise:
                    writer.write(wire.extended(99, b"\x00" * self.ext_noise))
                if self.pex_peers is not None:
                    writer.write(self.pex_message())
                    if self.pex_mode == "twice":
                        writer.write(self.pex_message())
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
                if mid == wire.EXTENDED:
                    if payload[0] == 0:
                        their_ut = metadata.parse_ext_handshake(payload[1:]).ut_metadata
                        self.dm_ids.append(bencode.decode(payload[1:])[0].get(b"m"))
                    elif payload[0] in (self.pex_id, 4) and self.pex_peers is not None:  # a ut_pex id: dm must never use it
                        self.pex_received += 1
                    elif payload[0] == 2 and self.metadata != "none":
                        kind, piece, _, _ = metadata.parse_metadata_msg(payload[1:])
                        self.metadata_requests += 1
                        self.metadata_pieces[piece] += 1
                        size = self.announced_size(content)
                        block = self.block(content, piece, size)
                        if self.metadata_delay:
                            await asyncio.sleep(self.metadata_delay)
                        if self.metadata in _ANSWERING and their_ut:
                            writer.write(metadata.data_msg(their_ut, piece, size, block)
                                         if block else metadata.reject_msg(their_ut, piece))
                            if self.metadata == "extra" and block:  # blocks nobody asked for
                                writer.write(metadata.data_msg(their_ut, piece, size, block))
                                writer.write(metadata.data_msg(their_ut, 99, size, block))
                            await writer.drain()
                        if self.metadata == "sybil":
                            return  # one block stored, and the peer is gone
                elif mid == wire.INTERESTED and not unchoked and not self.never_unchoke:
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
        except (TimeoutError, ConnectionError, asyncio.IncompleteReadError, ValueError, OSError):
            pass
        finally:
            self.open_connections -= 1
            self.closed += 1
            self.writers.discard(writer)
            writer.close()


class MetadataProbe:
    """A peer that asks dm for the torrent's metadata (BEP 9). It accepts the connection dm opens, sends an
    extension handshake and, on dm's first one (no `metadata_size` yet), asks for block 0: `early` holds the
    reply kinds. When dm sends one with the size it asks for every block (`mode="all"`), for a block past the
    end (`"bad_index"`), or for block 0 twice over (`"twice"`); `blocks` holds what arrived (piece -> bytes),
    `advertised` the last size dm announced, `eof` is set when dm closed the connection."""

    def __init__(self, torrent_path, mode="all"):
        self.meta = metainfo.load(torrent_path)
        with open(torrent_path, "rb") as f:
            raw = f.read()
        start, end = bencode.decode(raw)[1]
        self.info = raw[start:end]
        self.mode = mode
        self.peer_id = peer_id(b"MP")
        self.port = None
        self.connections = 0
        self.early = []
        self.blocks = {}
        self.later = []
        self.advertised = None
        self.eof = False
        self.writers = set()
        self._server = None

    async def start(self, host="127.0.0.1", port=0):
        self._server = await asyncio.start_server(self._serve, host, port)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def stop(self):
        if self._server:
            self._server.close()
        for w in list(self.writers):
            w.close()

    async def _serve(self, reader, writer):
        self.connections += 1
        self.writers.add(writer)
        count = -(-len(self.info) // BLOCK)
        their_ut, asked = None, False
        try:
            head = await asyncio.wait_for(reader.readexactly(68), 10)
            if _hs_info_hash(head) != self.meta.info_hash:
                return
            writer.write(wire.handshake(self.meta.info_hash, self.peer_id))
            writer.write(wire.extended(0, bencode.encode({"m": {"ut_metadata": 2}, "v": "probe"})))
            await writer.drain()
            while True:
                got = await wire.read_message(reader, None)
                if got is None or got[0] != wire.EXTENDED:
                    continue
                ext_id, body = got[1][0], got[1][1:]
                if ext_id == 0:
                    info = metadata.parse_ext_handshake(body)
                    their_ut = info.ut_metadata
                    if info.metadata_size is None:
                        writer.write(metadata.request_msg(their_ut, 0))
                    else:
                        self.advertised = info.metadata_size
                        if asked:
                            continue
                        asked = True
                        if self.mode == "bad_index":
                            pieces = [count + 5]
                        elif self.mode == "twice":
                            pieces = [0, 0]
                        else:
                            pieces = range(count)
                        for piece in pieces:
                            writer.write(metadata.request_msg(their_ut, piece))
                    await writer.drain()
                elif ext_id == 2:
                    kind, piece, total, data = metadata.parse_metadata_msg(body)
                    if self.advertised is None:
                        self.early.append(kind)
                    else:
                        self.later.append((kind, piece, total))
                        if kind == metadata.DATA:
                            self.blocks[piece] = data
        except asyncio.IncompleteReadError:
            self.eof = True
        except (TimeoutError, ConnectionError, ValueError, OSError):
            pass
        finally:
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
        except (TimeoutError, ConnectionError, asyncio.IncompleteReadError, ValueError, OSError, asyncio.CancelledError):
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
