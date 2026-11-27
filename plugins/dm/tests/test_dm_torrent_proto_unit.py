"""Unit tests for the wire format and the piece picker of the torrent support in dm."""

import asyncio
import random
import struct
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import torrentkit
from dmcore.torrent import picker, tracker, wire

IH = b"\x11" * 20
PID = b"-DM0200-" + b"x" * 12


def feed(data, eof=True):
    """A StreamReader holding data; build it inside a running loop."""
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    if eof:
        reader.feed_eof()
    return reader


def run_with(data, fn, eof=True):
    async def go():
        return await fn(feed(data, eof))

    return asyncio.run(go())


def read(data, npieces=100, eof=True):
    return run_with(data, lambda r: wire.read_message(r, npieces), eof)


def framed(mid, payload=b""):
    return struct.pack(">I", len(payload) + 1) + bytes([mid]) + payload


class Wire(unittest.TestCase):
    def test_read_handshake_rejects_wrong_info_hash_protocol_and_short_data(self):
        bad_proto = bytearray(wire.handshake(IH, PID))
        bad_proto[1] = ord("X")
        for data in (wire.handshake(b"\x22" * 20, PID), bytes(bad_proto), wire.handshake(IH, PID)[:30]):
            with self.subTest(data=data[:8]):
                with self.assertRaises(ValueError):
                    run_with(data, lambda r: wire.read_handshake(r, IH))


class Picker(unittest.TestCase):
    PL = 4 * picker.BLOCK  # 4 blocks per piece

    def make(self, npieces=4, total=None, **kw):
        total = total if total is not None else npieces * self.PL
        return picker.Picker(npieces, self.PL, total, rng=random.Random(1), **kw)

    def drain(self, p, pid, n=1000):
        return p.next_requests(pid, n)

    def test_unrequested_block_is_refused(self):
        p = self.make()
        p.peer_has("a", 0)
        p.next_requests("a", 1)  # block 0 of piece 0 only
        for idx, begin, n in ((0, picker.BLOCK, picker.BLOCK), (1, 0, picker.BLOCK)):
            with self.assertRaises(picker.BadBlock):
                p.block_received("a", idx, begin, b"x" * n)
        with self.assertRaises(picker.BadBlock):
            p.block_received("zzz", 0, 0, b"x" * picker.BLOCK)


class Tracker(unittest.TestCase):
    """AC3: announces against the fixture tracker (HTTP and UDP) running on a background loop."""

    @classmethod
    def setUpClass(cls):
        cls.bg = torrentkit.Background()
        cls.addClassCleanup(cls.bg.close)
        cls.fix = torrentkit.load_fixture("tracker")

    def serve(self, host="127.0.0.1", **kw):
        t = self.fix.Tracker(**kw)
        try:
            self.bg.call(t.start(host))
        except OSError:
            self.skipTest(f"cannot bind {host}")
        self.addCleanup(self.bg.call, t.stop())
        return t

    def req(self, event="started", left=1000):
        return tracker.Announce(IH, PID, 6881, 0, 0, left, event)

    def announce(self, url, **kw):
        return asyncio.run(tracker.announce(url, self.req(**kw), timeout=5))

    def drip_server(self, seconds=6):
        async def handler(reader, writer):
            try:
                await reader.readuntil(b"\r\n\r\n")
                writer.write(b"HTTP/1.0 200 OK\r\nContent-Length: 100000\r\n\r\n")
                end = asyncio.get_running_loop().time() + seconds
                while asyncio.get_running_loop().time() < end:
                    writer.write(b"d")
                    await writer.drain()
                    await asyncio.sleep(0.05)
            except ConnectionError, asyncio.IncompleteReadError:
                pass
            finally:
                writer.close()

        server = self.bg.call(asyncio.start_server(handler, "127.0.0.1", 0))
        self.addCleanup(server.close)
        return f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/announce"

    def tracker_threads(self):
        import threading

        return [t for t in threading.enumerate() if t.name == "dm-tracker"]

    def wait_no_threads(self, seconds=3):
        import time

        end = time.monotonic() + seconds
        while self.tracker_threads() and time.monotonic() < end:
            time.sleep(0.02)
        return not self.tracker_threads()

    def test_udp_gives_up_after_the_retries(self):
        t = self.serve(drop=1000)
        with mock.patch.object(tracker, "UDP_TIMEOUT", 0.02), mock.patch.object(tracker, "UDP_RETRIES", 2):
            with self.assertRaises(tracker.TrackerError) as cm:
                self.announce(f"udp://127.0.0.1:{t.udp_port}/announce")
        self.assertEqual(str(cm.exception), "timed out")
        self.assertEqual(t.datagrams, 3)  # the first try and two retries


if __name__ == "__main__":
    unittest.main()
