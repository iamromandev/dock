"""dm 0.5.0 stats model (phase C, step 16): per-file progress, rates, ETA, ratio and the peers list."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dmcore.torrent import metainfo, session
from dmcore.torrent.handle import TorrentHandle
from dmcore.torrent.peer import PeerConn

PL = 16384


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def three_files(have=(), clock=None):
    """Files of 40000, 10000 and 30000 bytes in pieces of 16384: five pieces, the last one short."""
    files = tuple(
        metainfo.FileEntry(("d", name), n) for name, n in (("a", 40000), ("b", 10000), ("c", 30000))
    )
    meta = metainfo.Meta(
        info_hash=b"\1" * 20,
        name="d",
        piece_length=PL,
        hashes=(b"\0" * 20,) * 5,
        total=80000,
        files=files,
        multi=True,
        trackers=(),
    )
    return session.Session(
        meta, None, lambda *a: None, TorrentHandle(), session.Options(), have=have, clock=clock or Clock()
    )


class Stats(unittest.TestCase):
    def test_rates_peers_eta_and_ratio(self):
        clock = Clock()
        s = three_files(have=(0,), clock=clock)
        self.assertEqual(s.stats()["eta_s"], None)
        self.assertIsNone(s.stats()["ratio"])  # nothing downloaded this run: no ratio to show
        a, b = PeerConn(s, ("10.0.0.1", 6881)), PeerConn(s, ("10.0.0.2", 51413))
        for conn, peer_id in ((a, b"-qB4650-" + b"0" * 12), (b, b"-xx0000-" + b"0" * 12)):
            conn.handshaken = True
            conn.peer_id = peer_id
            s.conns[conn.addr] = conn
        b.client = "Fancy 1.0"
        s.pending.append(("10.0.0.3", 1))
        for _ in range(30):
            clock.now += 1
            s.got_block(a, 60_000)
            s.got_block(b, 20_000)
            s.sent_block(a, 5_000)
            s.stats()
        st = s.stats()
        self.assertAlmostEqual(st["down_rate"], 80_000, delta=4_000)
        self.assertAlmostEqual(st["up_rate"], 5_000, delta=300)
        self.assertEqual((st["peers_connected"], st["peers_known"]), (2, 3))
        self.assertEqual(
            [(p[0], p[1]) for p in st["peers"]],
            [(("10.0.0.1", 6881), "qBittorrent 4.6.5"), (("10.0.0.2", 51413), "Fancy 1.0")],
        )
        self.assertGreater(st["peers"][0][2], st["peers"][1][2])
        self.assertAlmostEqual(st["eta_s"], (80000 - 16384) / st["down_rate"], delta=1)
        self.assertAlmostEqual(st["ratio"], 150_000 / 2_400_000, places=3)


if __name__ == "__main__":
    unittest.main()
