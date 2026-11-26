"""dm torrent uploads (R8, AC5): the choker, the serving rules, and a real leecher fed while dm downloads."""

import asyncio
import random
import sys
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import test_dm_torrent_local as local
from dmcore.torrent import session, wire
from dmcore.torrent.choker import choose
from dmcore.torrent.peer import MAX_QUEUED, PeerConn

PL = 16384


class Choose(unittest.TestCase):
    """The choker picks the best uploaders to us, plus one optimistic peer outside them."""

    def test_the_four_fastest_uploaders_get_the_regular_slots(self):
        rates = {f"p{i}": i for i in range(10)}
        regular, _ = choose(list(rates), rates, 4, random.Random(1))
        self.assertEqual(regular, {"p9", "p8", "p7", "p6"})

    def test_the_optimistic_slot_goes_to_a_peer_outside_the_regular_ones(self):
        rates = {f"p{i}": i for i in range(10)}
        for seed in range(20):
            regular, optimistic = choose(list(rates), rates, 4, random.Random(seed))
            self.assertIsNotNone(optimistic)
            self.assertNotIn(optimistic, regular)

    def test_the_optimistic_slot_rotates_over_the_other_peers(self):
        rates = {f"p{i}": 0 for i in range(8)}
        rng = random.Random(3)
        seen = {choose(list(rates), rates, 4, rng)[1] for _ in range(60)}
        self.assertGreater(len(seen), 3)

    def test_few_interested_peers_are_all_unchoked_without_an_optimistic_one(self):
        regular, optimistic = choose(["a", "b"], {"a": 1, "b": 2}, 4, random.Random(0))
        self.assertEqual((regular, optimistic), ({"a", "b"}, None))

    def test_nobody_interested_unchokes_nobody(self):
        self.assertEqual(choose([], {}, 4, random.Random(0)), (set(), None))

    def test_equal_rates_do_not_always_favour_the_same_peers(self):
        rates = {f"p{i}": 0 for i in range(8)}
        rng = random.Random(5)
        seen = set()
        for _ in range(40):
            seen |= choose(list(rates), rates, 4, rng)[0]
        self.assertEqual(seen, set(rates))


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

    def test_a_valid_request_is_queued(self):
        c = self.conn()
        self.request(c, 1, 0, PL)
        self.assertEqual(list(c.queue), [(1, 0, PL)])

    def test_requests_while_choked_are_ignored(self):
        c = self.conn()
        c.am_choking = True
        self.request(c, 1, 0, PL)
        self.assertEqual(list(c.queue), [])

    def test_a_piece_we_have_not_verified_closes_the_peer(self):
        c = self.conn()
        with self.assertRaises(ValueError):
            self.request(c, 5, 0, PL)

    def test_an_index_beyond_the_torrent_closes_the_peer(self):
        c = self.conn()
        with self.assertRaises(ValueError):
            self.request(c, 8, 0, PL)

    def test_a_block_over_16_kib_closes_the_peer(self):
        c = self.conn(piece_length=4 * PL)
        with self.assertRaises(ValueError):
            self.request(c, 0, 0, PL + 1)

    def test_an_empty_block_closes_the_peer(self):
        c = self.conn()
        with self.assertRaises(ValueError):
            self.request(c, 0, 0, 0)

    def test_a_block_running_past_the_piece_closes_the_peer(self):
        c = self.conn()
        with self.assertRaises(ValueError):
            self.request(c, 0, 1, PL)

    def test_a_flood_of_requests_closes_the_peer(self):
        c = self.conn(piece_length=MAX_QUEUED * 2 * wire.MAX_BLOCK)
        with self.assertRaises(ValueError):
            for i in range(MAX_QUEUED + 1):
                self.request(c, 0, i * wire.MAX_BLOCK, wire.MAX_BLOCK)
        self.assertLessEqual(len(c.queue), MAX_QUEUED)

    def test_cancel_removes_a_queued_request(self):
        c = self.conn()
        self.request(c, 0, 0, PL)
        self.request(c, 1, 0, PL)
        c.on_message(wire.CANCEL, wire.cancel(0, 0, PL)[5:], 0)
        self.assertEqual(list(c.queue), [(1, 0, PL)])

    def test_cancel_of_something_not_queued_is_harmless(self):
        c = self.conn()
        c.on_message(wire.CANCEL, wire.cancel(0, 0, PL)[5:], 0)
        self.assertEqual(list(c.queue), [])

    def test_choking_a_peer_drops_what_it_asked_for(self):
        c = self.conn()
        self.request(c, 0, 0, PL)
        c.set_choke(True)
        self.assertEqual(list(c.queue), [])
        self.assertTrue(c.am_choking)


class FakeWriter:
    def __init__(self):
        self.sent = []

    def write(self, data):
        self.sent.append(data)

    async def drain(self):
        pass

    def close(self):
        pass


class ChokeEpoch(unittest.IsolatedAsyncioTestCase):
    async def test_a_block_read_before_a_choke_is_not_sent_after_the_unchoke(self):
        started, release = threading.Event(), threading.Event()

        class Storage:
            def read_block(self, index, begin, length):
                started.set()
                release.wait(5)
                return b"x" * length

        fake = FakeSession()
        fake.storage = Storage()
        fake.reader_pool = ThreadPoolExecutor(1)
        self.addCleanup(fake.reader_pool.shutdown)
        c = PeerConn(fake, ("127.0.0.1", 1))
        c.writer = FakeWriter()
        c.handshaken = True
        c.am_choking = False
        c.on_message(wire.REQUEST, wire.request(0, 0, PL)[5:], 0)
        server = asyncio.ensure_future(c.serve())
        self.addCleanup(server.cancel)
        while not started.is_set():
            await asyncio.sleep(0.01)
        c.set_choke(True)
        c.set_choke(False)
        sent = len(c.writer.sent)  # the choke and the unchoke messages
        release.set()
        await asyncio.sleep(0.2)
        self.assertEqual(len(c.writer.sent), sent)


    async def test_blocks_served_are_counted_and_logged_when_the_connection_ends(self):
        class Storage:
            def read_block(self, index, begin, length):
                return b"x" * length

        fake = FakeSession()
        fake.storage = Storage()
        fake.reader_pool = ThreadPoolExecutor(1)
        self.addCleanup(fake.reader_pool.shutdown)
        c = PeerConn(fake, ("127.0.0.1", 1), 7)
        c.writer = FakeWriter()
        c.handshaken = True
        c.am_choking = False
        for index in (0, 1):
            c.on_message(wire.REQUEST, wire.request(index, 0, PL)[5:], 0)
        server = asyncio.ensure_future(c.serve())
        self.addCleanup(server.cancel)
        while c.served < 2:
            await asyncio.sleep(0.01)
        c.report_served()
        self.assertEqual(fake.log_lines, ["peer #7 served 2 blocks"])


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

    def test_best_uploaders_and_one_optimistic_peer_are_unchoked(self):
        self.s.rechoke(random.Random(1))
        open_ = self.open()
        self.assertTrue({7, 6, 5, 4} <= open_)
        self.assertEqual(len(open_), 5)
        self.assertTrue(open_ <= set(range(8)))  # never the uninterested ones

    def test_the_optimistic_peer_changes_between_rechokes(self):
        rng = random.Random(2)
        seen = set()
        for _ in range(30):
            for c in self.conns:
                c.received += 1000 * (c.addr[1] - 999)  # keep the ranking steady
            self.s.rechoke(rng)
            seen |= self.open() - {7, 6, 5, 4}
        self.assertGreater(len(seen), 1)

    def test_a_peer_that_loses_its_slot_is_choked_again(self):
        self.s.rechoke(random.Random(1))
        was = self.open()
        for c in self.conns:
            c.peer_interested = False
        self.s.rechoke(random.Random(1))
        self.assertTrue(was)
        self.assertEqual(self.open(), set())

    def test_a_newly_interested_peer_gets_a_free_slot_at_once(self):
        c = self.conns[0]
        c.on_message(wire.INTERESTED, b"", 0)
        self.assertFalse(c.am_choking)

    def test_no_more_than_five_peers_are_unchoked_by_interest_alone(self):
        for c in self.conns[:8]:
            c.on_message(wire.INTERESTED, b"", 0)
        self.assertEqual(len(self.open()), 5)


    def test_peers_that_lost_interest_still_hold_their_slots_until_the_next_rechoke(self):
        for c in self.conns[:5]:
            c.on_message(wire.INTERESTED, b"", 0)
        for c in self.conns[:5]:
            c.on_message(wire.NOT_INTERESTED, b"", 0)
        for c in self.conns[5:8]:
            c.on_message(wire.INTERESTED, b"", 0)
        self.assertLessEqual(len(self.open()), 5)


class Upload(local.Harness):
    """AC5, in-process: peers that listen get verified pieces from dm while it downloads."""

    def leecher(self, t):
        lc = self.start(self.peerfix.Leecher(t.write_torrent(self.dir / "leech.torrent")))
        return lc

    def test_a_leecher_gets_verified_pieces_while_dm_downloads(self):
        tr = self.serve_tracker()
        t = self.single(size=2 * 1024 * 1024, piece_length=64 * 1024, tr=tr)
        slow = self.seeder(t, delay=0.01)
        lc = self.leecher(t)
        self.swarm(tr, slow, lc)
        final, _, _ = self.run_torrent(t)
        self.assertEqual(final.read_bytes(), t.blob)
        self.assertGreaterEqual(len(lc.pieces), 1)
        plen = 64 * 1024
        for index, data in lc.pieces.items():
            self.assertEqual(data, t.blob[index * plen:(index + 1) * plen])

    def test_a_peer_that_never_says_interested_gets_nothing(self):
        tr = self.serve_tracker()
        t = self.single(size=1024 * 1024, piece_length=64 * 1024, tr=tr)
        slow = self.seeder(t, delay=0.01)
        probe = Probe(t)
        self.start(probe)
        probe.interested = False
        self.swarm(tr, slow, probe)
        self.run_torrent(t)
        self.assertGreaterEqual(probe.connections, 1)
        self.assertEqual(probe.pieces, [])
        self.assertEqual(probe.unchokes, 0)

    def test_the_connections_are_closed_when_the_download_ends(self):
        tr = self.serve_tracker()
        t = self.single(size=1024 * 1024, piece_length=64 * 1024, tr=tr)
        slow = self.seeder(t, delay=0.005)
        lc = self.leecher(t)
        self.swarm(tr, slow, lc)
        self.run_torrent(t)
        self.assertTrue(self.wait_for(lambda: not lc.writers and slow.open_connections == 0))

    def test_a_peer_asking_for_a_piece_dm_lacks_is_dropped_and_the_download_goes_on(self):
        tr = self.serve_tracker()
        t = self.single(size=2 * 1024 * 1024, piece_length=64 * 1024, tr=tr)
        slow = self.seeder(t, delay=0.03)  # the download takes at least 3 s
        probe = Probe(t, greedy=True)
        self.start(probe)
        self.swarm(tr, slow, probe)
        final, _, _ = self.run_torrent(t)
        self.assertEqual(final.read_bytes(), t.blob)
        self.assertEqual(probe.pieces, [])
        self.assertIsNotNone(probe.closed_after)
        self.assertLess(probe.closed_after, 1.5)  # dropped long before the download ended

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

    def test_cancel_while_a_leecher_is_being_served_is_prompt_and_closes_its_connection(self):
        tr = self.serve_tracker()
        t = self.single(size=2 * 1024 * 1024, piece_length=64 * 1024, tr=tr)
        slow = self.seeder(t, delay=0.03)
        probe = Probe(t, wanted=True)
        self.start(probe)
        self.swarm(tr, slow, probe)
        handle, result = local.TorrentHandle(), {}

        def work():
            try:
                result["ok"] = self.run_torrent(t, handle=handle)
            except BaseException as e:
                result["err"] = e
            result["end"] = time.monotonic()

        thread = threading.Thread(target=work)
        thread.start()
        self.assertTrue(self.wait_for(lambda: len(probe.blocks) >= 2, 10))
        start = time.monotonic()
        self.assertTrue(handle.abort())
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertLess(result["end"] - start, 2)
        self.assertIsInstance(result["err"], local.Cancelled)
        self.assertEqual(self.listing(), self.kept(t))
        self.assertTrue(self.wait_for(lambda: probe.closed_after is not None and not probe._writers))
        self.assertTrue(self.wait_for(lambda: slow.open_connections == 0))

    def test_a_valid_request_is_answered_with_the_right_bytes(self):
        tr = self.serve_tracker()
        t = self.single(size=2 * 1024 * 1024, piece_length=64 * 1024, tr=tr)
        slow = self.seeder(t, delay=0.01)
        probe = Probe(t, wanted=True)
        self.start(probe)
        self.swarm(tr, slow, probe)
        self.run_torrent(t)
        self.assertTrue(probe.blocks)
        for (index, begin), data in probe.blocks.items():
            start = index * 64 * 1024 + begin
            self.assertEqual(data, t.blob[start:start + len(data)])


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
        except (TimeoutError, asyncio.IncompleteReadError, ConnectionError, ValueError, OSError):
            self.closed_after = asyncio.get_running_loop().time() - began
        finally:
            self._writers.discard(writer)
            writer.close()


if __name__ == "__main__":
    unittest.main()
