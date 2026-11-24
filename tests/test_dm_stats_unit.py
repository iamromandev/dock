"""dm 0.5.0 stats model (phase C, step 16): per-file progress, rates, ETA, ratio and the peers list."""

import random
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "plugin" / "dm"))

from dmcore.torrent import bencode, layout, metadata, metainfo, session, stats  # noqa: E402
from dmcore.torrent.handle import TorrentHandle  # noqa: E402
from dmcore.torrent.peer import PeerConn  # noqa: E402

PL = 16384


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def three_files(have=(), clock=None):
    """Files of 40000, 10000 and 30000 bytes in pieces of 16384: five pieces, the last one short."""
    files = tuple(metainfo.FileEntry(("d", name), n) for name, n in (("a", 40000), ("b", 10000), ("c", 30000)))
    meta = metainfo.Meta(info_hash=b"\1" * 20, name="d", piece_length=PL, hashes=(b"\0" * 20,) * 5,
                         total=80000, files=files, multi=True, trackers=())
    return session.Session(meta, None, lambda *a: None, TorrentHandle(), session.Options(), have=have,
                           clock=clock or Clock())


class Layout(unittest.TestCase):
    def test_a_piece_is_split_over_the_files_it_spans(self):
        specs = layout.plan_files(three_files().meta)
        self.assertEqual(layout.piece_files(specs, PL, 80000, 0), [(0, 16384)])
        self.assertEqual(layout.piece_files(specs, PL, 80000, 2), [(0, 7232), (1, 9152)])
        self.assertEqual(layout.piece_files(specs, PL, 80000, 4), [(2, 14464)])

    def test_the_pieces_of_a_file_add_up_to_its_length(self):
        specs = layout.plan_files(three_files().meta)
        done = [0, 0, 0]
        for index in range(5):
            for i, n in layout.piece_files(specs, PL, 80000, index):
                done[i] += n
        self.assertEqual(done, [40000, 10000, 30000])


class ClientName(unittest.TestCase):
    def test_the_extension_handshake_name_wins_and_is_cut_and_cleaned(self):
        self.assertEqual(stats.client_name(b"-qB4650-" + b"x" * 12, "qBittorrent/4.6.5"), "qBittorrent/4.6.5")
        long = stats.client_name(None, "a" * 50)
        self.assertEqual(long, "a" * 20)
        self.assertEqual(stats.client_name(None, "bad\x1b[31m\nname"), "bad?[31m?name")

    def test_bidi_and_other_format_characters_and_line_separators_are_replaced(self):
        for char in ("\u202e", "\u202a", "\u2066", "\u2069", "\u200f", "\ufeff", "\u2028", "\u2029", "\u00ad"):
            with self.subTest(hex(ord(char))):
                self.assertEqual(stats.client_name(None, f"a{char}b"), "a?b")

    def test_azureus_style_prefixes_are_named_with_their_version(self):
        cases = {b"-qB4650-": "qBittorrent 4.6.5", b"-TR3000-": "Transmission 3.0.0",
                 b"-UT3550-": "uTorrent 3.5.5", b"-LT0D80-": "libtorrent 0.13.8",
                 b"-lt0D80-": "libtorrent 0.13.8", b"-DE2110-": "Deluge 2.1.1", b"-AZ5750-": "Azureus 5.7.5",
                 b"-DM0500-": "dm 0.5.0"}
        for prefix, name in cases.items():
            with self.subTest(prefix):
                self.assertEqual(stats.client_name(prefix + b"0" * 12, None), name)

    def test_anything_else_is_a_question_mark(self):
        for peer_id in (None, b"", b"x" * 20, b"-ZZ1234-" + b"0" * 12, b"-qB46"):
            with self.subTest(peer_id):
                self.assertEqual(stats.client_name(peer_id, None), "?")
        self.assertEqual(stats.client_name(b"x" * 20, ""), "?")

    def test_the_extension_handshake_carries_the_peers_client_name(self):
        body = bencode.encode({"m": {"ut_metadata": 2}, "v": "Some Client 1.0"})
        self.assertEqual(metadata.parse_ext_handshake(body).client, "Some Client 1.0")
        self.assertIsNone(metadata.parse_ext_handshake(bencode.encode({"m": {}})).client)
        odd = bencode.encode({"m": {}, "v": 7})
        self.assertIsNone(metadata.parse_ext_handshake(odd).client)


class Rates(unittest.TestCase):
    def test_a_steady_flow_settles_on_its_rate_and_decays_when_it_stops(self):
        clock = Clock()
        rate = stats.Rate(clock)
        for _ in range(40):
            clock.now += 1
            rate.add(1000)
            rate.value()
        self.assertAlmostEqual(rate.value(), 1000, delta=60)
        clock.now += 10  # two windows of silence
        self.assertLess(rate.value(), 200)
        clock.now += 60
        self.assertLess(rate.value(), 1)

    def test_a_new_flow_does_not_jump_to_its_first_instant(self):
        clock = Clock()
        rate = stats.Rate(clock)
        clock.now += 1
        rate.add(100_000)
        self.assertLess(rate.value(), 100_000 * 0.5)


class Stats(unittest.TestCase):
    def test_bytes_per_file_come_from_the_verified_pieces(self):
        s = three_files(have=(0, 2))
        self.assertEqual(sorted(s.stats()["files"]),
                         [(0, "d/a", 23616, 40000), (1, "d/b", 9152, 10000), (2, "d/c", 0, 30000)])
        s.picker.mark_verified([3, 1])  # [49152, 65536): the end of b and 15536 bytes of c; [16384, 32768): in a
        self.assertEqual(sorted(s.stats()["files"]),  # a and b finished: reported once, then not again
                         [(0, "d/a", 40000, 40000), (1, "d/b", 10000, 10000), (2, "d/c", 15536, 30000)])
        self.assertEqual(s.stats()["files"], [(2, "d/c", 15536, 30000)])

    def test_a_torrent_of_fifty_thousand_files_keeps_stats_cheap(self):
        """stats() runs about 5 times a second on the event loop. Planning the files on every call cost about
        15 ms at 50,000 files (and 420 ms at the 100,000 limit); now a call is well under 1 ms. The bound of
        5 ms is 5 times the measured cost on a slow machine and about a third of the old one, taken as the
        median of 21 calls so one scheduling hiccup cannot fail it."""
        n, plen = 50_000, 16384
        files = tuple(metainfo.FileEntry((f"d{i % 50}", f"some-long-file-name-{i}.bin"), 3 * plen) for i in range(n))
        npieces = 3 * n
        meta = metainfo.Meta(info_hash=b"\1" * 20, name="d", piece_length=plen, hashes=(b"\0" * 20,) * npieces,
                             total=3 * plen * n, files=files, multi=True, trackers=())
        s = session.Session(meta, None, lambda *a: None, TorrentHandle(), session.Options(), clock=Clock())
        order = random.Random(1).sample(range(npieces), npieces)
        s.picker.mark_verified(order[: npieces // 2])
        times = []
        for k in range(21):
            s.picker.mark_verified(order[npieces // 2 + k * 50: npieces // 2 + (k + 1) * 50])
            t = time.perf_counter()
            st = s.stats()
            times.append(time.perf_counter() - t)
        times.sort()
        self.assertLess(times[10], 0.005, f"median stats() call took {times[10] * 1000:.1f} ms")
        self.assertLessEqual(len([f for f in st["files"] if f[2] < f[3]]), 8)

    def test_a_single_file_torrent_does_no_file_work(self):
        files = (metainfo.FileEntry(("x.bin",), 80000),)
        meta = metainfo.Meta(info_hash=b"\1" * 20, name="x.bin", piece_length=PL, hashes=(b"\0" * 20,) * 5,
                             total=80000, files=files, multi=False, trackers=())
        s = session.Session(meta, None, lambda *a: None, TorrentHandle(), session.Options(), have=(0, 1),
                            clock=Clock())
        self.assertEqual(s.stats()["files"], [])

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
        self.assertEqual([(p[0], p[1]) for p in st["peers"]],
                         [(("10.0.0.1", 6881), "qBittorrent 4.6.5"), (("10.0.0.2", 51413), "Fancy 1.0")])
        self.assertGreater(st["peers"][0][2], st["peers"][1][2])
        self.assertAlmostEqual(st["eta_s"], (80000 - 16384) / st["down_rate"], delta=1)
        self.assertAlmostEqual(st["ratio"], 150_000 / 2_400_000, places=3)

    def test_a_peer_that_is_not_connected_yet_is_not_listed(self):
        s = three_files()
        conn = PeerConn(s, ("10.0.0.9", 1))
        s.conns[conn.addr] = conn
        self.assertEqual(s.stats()["peers"], [])
        self.assertEqual(s.stats()["peers_connected"], 0)

    def test_the_metadata_phase_has_no_files(self):
        s = session.Session(None, None, lambda *a: None, TorrentHandle(), session.Options(),
                            info_hash=b"\2" * 20, clock=Clock())
        st = s.stats()
        self.assertEqual((st["files"], st["eta_s"]), ([], None))

    def test_our_peer_id_and_extension_name_say_0_5_0(self):
        self.assertTrue(three_files().peer_id.startswith(b"-DM0700-"))
        self.assertEqual(bencode.decode(metadata.ext_handshake())[0][b"v"], b"dm 0.7.0")


if __name__ == "__main__":
    unittest.main()
