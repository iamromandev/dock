"""dm torrent uploads (R8, AC5): the choker, the serving rules, and a real leecher fed while dm downloads."""

import asyncio
import random
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import test_dm_torrent_local as local
from dmcore.torrent import session, wire
from dmcore.torrent.peer import PeerConn

PL = 16384


class FakeSession:
    """What PeerConn needs from a session, for the serving rules without a network."""

    def __init__(self, npieces=8, piece_length=PL, have=(0, 1)):
        class Meta:
            hashes = [b"x" * 20] * npieces
            files = ("one",)  # a single file: the session keeps no per-file progress
            private = False

        self.meta = Meta()
        self.meta.piece_length = piece_length
        self.meta.total = npieces * piece_length
        self.opts = session.Options()
        self.finished = set(have)
        self.picker = self
        self.log_lines = []
        self.clock = time.monotonic

    def got_block(self, conn, n):
        pass

    def sent_block(self, conn, n):
        pass

    def piece_size(self, index):
        return self.meta.piece_length

    def log(self, message):
        self.log_lines.append(message)


class Serving(unittest.TestCase):
    """Request validation and the queue, on one PeerConn."""

    def conn(self, **kw):
        c = PeerConn(FakeSession(**kw), ("127.0.0.1", 1))
        c.handshaken = True
        c.am_choking = False
        return c

    def request(self, c, index, begin, length):
        c.on_message(wire.REQUEST, wire.request(index, begin, length)[5:], 0)

    def test_cancel_of_something_not_queued_is_harmless(self):
        c = self.conn()
        c.on_message(wire.CANCEL, wire.cancel(0, 0, PL)[5:], 0)
        self.assertEqual(list(c.queue), [])


class FakeWriter:
    def __init__(self):
        self.sent = []

    def write(self, data):
        self.sent.append(data)

    async def drain(self):
        pass

    def close(self):
        pass


class Rechoke(unittest.TestCase):
    """The session's choker over many connections."""

    def setUp(self):
        fake = FakeSession(npieces=4, have=())
        self.s = session.Session(fake.meta, None, None, None, session.Options())
        self.addCleanup(self.s.pool.shutdown)
        self.addCleanup(self.s.reader_pool.shutdown)
        self.conns = []
        for i in range(10):
            c = PeerConn(self.s, ("10.0.0.1", 1000 + i))
            c.handshaken = True
            c.peer_interested = i < 8  # two uninterested peers
            c.received = i * 1000
            self.s.conns[c.addr] = c
            self.conns.append(c)

    def open(self):
        return {i for i, c in enumerate(self.conns) if not c.am_choking}

    def test_a_peer_that_loses_its_slot_is_choked_again(self):
        self.s.rechoke(random.Random(1))
        was = self.open()
        for c in self.conns:
            c.peer_interested = False
        self.s.rechoke(random.Random(1))
        self.assertTrue(was)
        self.assertEqual(self.open(), set())


class Upload(local.Harness):
    """AC5, in-process: peers that listen get verified pieces from dm while it downloads."""

    def leecher(self, t):
        lc = self.start(self.peerfix.Leecher(t.write_torrent(self.dir / "leech.torrent")))
        return lc

    def test_a_peer_that_never_reads_is_dropped_and_the_download_goes_on(self):
        tr = self.serve_tracker()
        t = self.single(size=2 * 1024 * 1024, piece_length=64 * 1024, tr=tr)
        slow = self.seeder(t, delay=0.03)  # the download takes about 4 s
        probe = Probe(t, mute=True)
        self.start(probe)
        self.swarm(tr, slow, probe)
        lines = []
        opts = session.Options(dht=False, stall_s=3, connect_timeout=2, idle_timeout=1)
        final, _, _ = self.run_torrent(t, opts=opts, log=lines.append)
        self.assertEqual(final.read_bytes(), t.blob)
        self.assertTrue(probe.muted)
        self.assertTrue(any("TimeoutError" in line for line in lines), lines)


class Probe:
    """A scripted listening peer. It records what dm sends and, depending on the mode, asks for things.

    wanted: asks for the first block of every piece dm announces. greedy: asks for a piece dm never announced,
    as soon as it is unchoked. interested=False: never says so.
    """

    def __init__(self, torrent, wanted=False, greedy=False, mute=False):
        self.t = torrent
        self.wanted = wanted
        self.mute = mute  # asks for every block of what dm has several times over, then never reads again
        self.greedy = greedy
        self.interested = True
        self.muted = False
        self.port = None
        self.connections = 0
        self.unchokes = 0
        self.pieces = []
        self.blocks = {}
        self.closed_after = None  # seconds from the connection to its end
        self.peer_id = b"-PR0001-" + b"p" * 12
        self._server = None
        self._writers = set()

    async def start(self, host="127.0.0.1"):
        self._server = await asyncio.start_server(self._serve, host, 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def stop(self):
        self._server.close()
        for w in list(self._writers):
            w.close()

    async def _serve(self, reader, writer):
        self.connections += 1
        self._writers.add(writer)
        began = asyncio.get_running_loop().time()
        n = -(-len(self.t.blob) // self.t.piece_length)
        has = set()
        try:
            writer.write(wire.handshake(self.t.info_hash, self.peer_id))
            await wire.read_handshake(reader, self.t.info_hash)
            if self.interested:
                writer.write(wire.msg(wire.INTERESTED))
            await writer.drain()
            asked = False
            while True:
                got = await asyncio.wait_for(wire.read_message(reader, n), 20)
                if got is None:
                    continue
                mid, payload = got
                if mid == wire.UNCHOKE:
                    self.unchokes += 1
                    if self.greedy and not asked:
                        asked = True
                        missing = next(i for i in range(n) if i not in has)
                        writer.write(wire.request(missing, 0, 16384))
                elif mid == wire.HAVE:
                    index = wire.parse_have(payload)
                    has.add(index)
                    if self.mute and self.unchokes and len(has) >= 2:
                        for i in sorted(has):
                            for _ in range(8):
                                for begin in range(0, self.t.piece_length, 16384):
                                    writer.write(wire.request(i, begin, 16384))
                        self.muted = True
                        while not writer.is_closing():
                            await asyncio.sleep(0.1)
                    if self.wanted and self.unchokes:
                        writer.write(wire.request(index, 0, 16384))
                elif mid == wire.BITFIELD:
                    has.update(wire.parse_bitfield(payload, n))
                elif mid == wire.PIECE:
                    index, begin, data = wire.parse_piece(payload)
                    self.pieces.append(index)
                    self.blocks[(index, begin)] = data
                await writer.drain()
        except TimeoutError, asyncio.IncompleteReadError, ConnectionError, ValueError, OSError:
            self.closed_after = asyncio.get_running_loop().time() - began
        finally:
            self._writers.discard(writer)
            writer.close()


if __name__ == "__main__":
    unittest.main()
