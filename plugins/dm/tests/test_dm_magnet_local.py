"""dm magnet links in one process: fixture tracker and seeders on 127.0.0.1, no container runtime needed.

AC3, AC4, AC6, AC7, AC8, AC11, AC12, AC15 and a Ctrl-C resume that fetches the metadata again.
"""

import errno
import hashlib
import random
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import torrentkit
import dmcore
from dmcore import Cancelled
from dmcore.target import Target
from dmcore.torrent import bencode, layout, magnet, metadata, metainfo, session
from dmcore.torrent.handle import TorrentHandle
from test_dm_torrent_local import FAST, PL, Harness


class MagnetHarness(Harness):
    def announce_url(self, tr):
        return f"http://127.0.0.1:{tr.http_port}/announce"

    def magnet(self, t, tr=None, peers=(), **kw):
        trackers = [self.announce_url(tr)] if tr is not None else []
        return torrentkit.magnet_for(t, trackers, peers, **kw)

    def in_listed_order(self):
        """Peers are dialed in the order the tracker lists them: no shuffle, so `peer #n` in a log line is the
        n-th seeder of `swarm()`. A log never carries an address, so this is how a test tells the peers apart."""
        return mock.patch.object(
            session, "random", types.SimpleNamespace(shuffle=lambda items: None, sample=random.sample)
        )

    def run_magnet(self, link, opts=FAST, fixed_name=None, handle=None, resume=True):
        self.progress, self.notes = [], []
        return session.run_magnet(
            magnet.parse(link),
            Target(self.cwd, fixed_name),
            lambda pct, text: self.progress.append((pct, text)),
            handle or TorrentHandle(),
            opts,
            None,
            resume=resume,
            note=self.notes.append,
        )

    def start_magnet(self, link, opts=FAST, fixed_name=None, resume=True):
        """Run in a thread; returns (handle, result dict, thread)."""
        handle, result = TorrentHandle(), {}

        def work():
            try:
                result["ok"] = self.run_magnet(link, opts, fixed_name, handle, resume)
            except BaseException as e:
                result["err"] = e

        thread = threading.Thread(target=work)
        thread.start()
        self.addCleanup(thread.join, 10)
        return handle, result, thread

    def dm(self, link, **inputs):
        """dmcore.run for a magnet: (exit code, events)."""
        events = []
        code = dmcore.run(
            {"url": link, "no_dht": True, **inputs},
            str(self.cwd),
            lambda method, **kw: events.append((method, kw)),
            threading.Event(),
        )
        return code, events

    @staticmethod
    def texts(events, kind):
        return [kw.get("text") or kw.get("message") for k, kw in events if k == kind]


class Download(MagnetHarness):
    """AC4, AC6, AC8: the metadata comes from the seeder, then the torrent downloads as usual."""

    # AC8

    # AC6


def many_files(n=700):
    """A torrent whose info dictionary needs two ut_metadata blocks."""
    return [(f"d/f{i:04d}.bin", b"x") for i in range(n)]


class Hostile(MagnetHarness):
    """S3: a corrupt metadata seeder never gets an honest peer banned."""

    def test_a_corrupt_seeder_and_an_honest_one_in_either_order_still_succeed(self):
        for order in (0, 1, 0, 1, 0, 1):
            with self.subTest(round=order):
                self.setUp()
                t = self.torrent(many_files(), name="set", multi=True)
                self.assertGreater(len(bencode.encode(bencode.decode(t.data)[0][b"info"])), metadata.BLOCK)
                tr = self.serve_tracker()
                good, evil = self.seeder(t), self.seeder(t, metadata="corrupt")
                self.swarm(tr, *((good, evil) if order == 0 else (evil, good)))
                logs = []
                self.progress, self.notes = [], []
                with self.in_listed_order():
                    final, _, nfiles, info = session.run_magnet(
                        magnet.parse(self.magnet(t, tr)),
                        Target(self.cwd, None),
                        lambda p, x: None,
                        TorrentHandle(),
                        FAST,
                        logs.append,
                        note=self.notes.append,
                    )
                self.assertEqual(nfiles, 700)
                self.assertEqual(hashlib.sha1(info).digest(), t.info_hash)
                dropped = [m for m in logs if "dropped" in m]
                self.assertFalse([m for m in dropped if m.startswith(f"peer #{order + 1} ")], dropped)


class HostileMetadata(MagnetHarness):
    """AC5, NFR1: peers that lie about or abuse the metadata are dropped, an honest peer supplies it."""

    def setUp(self):
        super().setUp()
        files = many_files() + [("big.bin", torrentkit.content(30 * PL, "b"))]
        self.files = files
        self.t = self.torrent(files, name="set", multi=True)
        self.info = bencode.encode(bencode.decode(self.t.data)[0][b"info"])
        self.assertGreater(len(self.info), metadata.BLOCK)

    def run_with(self, *evil, honest=None):
        tr = self.serve_tracker()
        good = self.seeder(
            self.t, delay=0.02, **{"metadata_delay": 0.3, **(honest or {})}
        )  # hostile peers go first
        self.swarm(tr, *evil, good)
        logs = []
        self.progress, self.notes = [], []
        with self.in_listed_order():
            final, _, nfiles, info = session.run_magnet(
                magnet.parse(self.magnet(self.t, tr)),
                Target(self.cwd, None),
                lambda p, x: None,
                TorrentHandle(),
                FAST,
                logs.append,
                note=self.notes.append,
            )
        self.assertEqual(torrentkit.tree(final), dict(self.files))
        self.assertEqual(hashlib.sha1(info).digest(), self.t.info_hash)
        dropped = [m for m in logs if "dropped" in m]
        # never the honest one, listed last; and what is logged is the peer's number and the kind of error only
        self.assertFalse([m for m in dropped if m.startswith(f"peer #{len(evil) + 1} ")], dropped)
        for m in dropped:
            self.assertRegex(m, r"^peer #\d+ dropped: \w+$")
        return dropped

    def dropped_peer(self, dropped, number):
        return any(m.startswith(f"peer #{number} dropped") for m in dropped)

    def hostile(self, mode):
        evil = self.seeder(self.t, metadata=mode)
        dropped = self.run_with(evil)
        self.assertTrue(self.dropped_peer(dropped, 1), (mode, dropped))
        self.assertLessEqual(evil.metadata_requests, 3)

    def test_every_kind_of_hostile_peer_at_once(self):
        # a silent peer is tested on its own: next to a corrupt one it can cost a 10 s timeout per attempt
        evil = [self.seeder(self.t, metadata=m) for m in ("size0", "huge", "short", "extra", "corrupt")]
        self.run_with(*evil)


class OddExtensions(MagnetHarness):
    """S4: a plain .torrent download never drops a peer over its BEP 10 extras."""

    def download_from(self, **seeder_args):
        tr = self.serve_tracker()
        t = self.single(size=4 * PL, tr=tr)
        seeder = self.seeder(t, **seeder_args)
        self.swarm(tr, seeder)
        final, *_ = self.run_torrent(t)
        self.assertEqual(final.read_bytes(), t.blob)
        self.assertEqual(seeder.connections, 1)

    def test_an_extension_handshake_that_is_not_a_dictionary(self):
        self.download_from(ext_body=b"i5e")

    def test_an_oversized_extension_message_is_ignored(self):
        self.download_from(ext_noise=100_000)


class StoredMetadata(MagnetHarness):
    """AC9, AC10 (R14, R15): the metadata is kept beside the partial and reused by the next run."""

    def paths(self, t):
        from dmcore import resume

        meta = metainfo.load(t.write_torrent(self.dir / "k.torrent"))
        key = resume.torrent_key(meta.info_hash, Target(self.cwd, None))
        return resume.paths(self.cwd, layout.item_name(meta), key).part, resume.meta_path(self.cwd, key)

    def cancelled_run(self, t, tr):
        """Start the magnet against a slow seeder and cancel it once some pieces are in."""
        first = self.seeder(t, delay=0.01)
        self.swarm(tr, first)
        link = self.magnet(t, tr)
        handle, result, thread = self.start_magnet(link)
        self.assertTrue(self.wait_for(lambda: any(p and p >= 30 for p, _ in list(self.progress)), 20))
        self.assertTrue(handle.abort())
        thread.join(5)
        self.assertIsInstance(result.get("err"), Cancelled)
        return link

    def torrent_run(self, t, resume=True):
        path = t.write_torrent(self.dir / "x.torrent")
        return session.run_torrent(
            path, Target(self.cwd, None), lambda p, x: None, TorrentHandle(), FAST, resume=resume
        )

    def test_a_tampered_metadata_file_is_deleted_and_fetched_again(self):
        tr = self.serve_tracker()
        t = self.single(size=64 * PL, piece_length=PL)
        link = self.cancelled_run(t, tr)
        _, meta = self.paths(t)
        data = bytearray(meta.read_bytes())
        data[10] ^= 0xFF
        meta.write_bytes(bytes(data))
        second = self.seeder(t)
        self.swarm(tr, second)
        final, *_ = self.run_magnet(link)
        self.assertEqual(final.read_bytes(), t.blob)
        self.assertGreaterEqual(second.metadata_requests, 1)
        self.assertEqual(self.listing(), ["a.bin"])

    def test_no_resume_downloads_everything_again(self):
        tr = self.serve_tracker()
        t = self.single(size=64 * PL, piece_length=PL)
        link = self.cancelled_run(t, tr)
        second = self.seeder(t)
        self.swarm(tr, second)
        final, *_ = self.run_magnet(link, resume=False)
        self.assertEqual(final.read_bytes(), t.blob)
        self.assertGreaterEqual(second.metadata_requests, 1)
        self.assertEqual(set(second.piece_requests), set(range(64)))
        self.assertEqual(self.listing(), ["a.bin"])


class ServeMetadata(MagnetHarness):
    """AC13 (R18): peers that ask dm for the metadata are refused before it is verified and served after."""

    def probe(self, t, mode="all"):
        probe = self.start(self.peerfix.MetadataProbe(t.write_torrent(self.dir / "p.torrent"), mode))
        return probe

    def run_with(self, t, tr, probe):
        """Download t as a magnet from a slow seeder whose metadata comes late, with the probe alongside."""
        seeder = self.seeder(t, delay=0.02, metadata_delay=0.5)
        self.swarm(tr, probe, seeder)
        info = bencode.encode(bencode.decode(t.data)[0][b"info"])
        final, *_ = self.run_magnet(self.magnet(t, tr))
        self.assertEqual(final.read_bytes(), t.blob)
        return info

    def test_a_block_past_the_end_drops_the_peer_and_the_download_still_finishes(self):
        tr = self.serve_tracker()
        t = self.single(size=64 * PL, piece_length=PL)
        probe = self.probe(t, "bad_index")
        self.run_with(t, tr, probe)
        self.assertEqual(probe.early, [metadata.REJECT])
        self.assertEqual(probe.blocks, {})
        self.assertTrue(probe.eof)

    def test_a_download_that_started_from_stored_metadata_advertises_the_size_at_once(self):
        tr = self.serve_tracker()
        t = self.single(size=64 * PL, piece_length=PL)
        link = StoredMetadata.cancelled_run(self, t, tr)
        probe = self.probe(t)
        self.swarm(tr, probe, self.seeder(t, delay=0.02))
        final, *_ = self.run_magnet(link)
        self.assertEqual(final.read_bytes(), t.blob)
        self.assertEqual(probe.early, [])
        self.assertEqual(probe.advertised, len(bencode.encode(bencode.decode(t.data)[0][b"info"])))


class Saved(MagnetHarness):
    """AC3, AC4, AC11, AC12 through dmcore.run: output lines, the saved .torrent, its failure."""

    def seeded(self, **kw):
        tr = self.serve_tracker()
        t = self.single(size=8 * PL, **kw)
        self.swarm(tr, self.seeder(t))
        return tr, t

    # AC3
    def test_a_magnet_without_trackers_or_peers_and_no_dht_fails_at_once_naming_only_the_hash(self):
        t = self.single(size=4 * PL)
        link = f"magnet:?xt=urn:btih:{t.info_hash.hex()}&dn=Secret+Name"
        with mock.patch.object(session, "run_magnet", side_effect=AssertionError("network used")):
            code, events = self.dm(link, no_dht=True)
        self.assertEqual(code, 1)
        errors = self.texts(events, "log")
        self.assertEqual(
            errors,
            [
                f"dm: magnet {t.info_hash.hex()[:8]}…: magnet link has no trackers or "
                "peers (and the DHT is off)"
            ],
        )
        self.assertEqual(self.listing(), [])

    # AC4

    # AC11
    def test_the_saved_torrent_has_the_same_hash_and_the_magnets_trackers(self):
        tr, t = self.seeded()
        second = "udp://127.0.0.1:9/announce"
        link = torrentkit.magnet_for(t, [self.announce_url(tr), second])
        code, _ = self.dm(link)
        self.assertEqual(code, 0)
        saved = metainfo.load(self.cwd / "a.bin.torrent")
        self.assertEqual(saved.info_hash, t.info_hash)
        self.assertEqual(saved.trackers, ((self.announce_url(tr), second),))
        top = bencode.decode((self.cwd / "a.bin.torrent").read_bytes())[0]
        self.assertEqual(top[b"announce"], self.announce_url(tr).encode())

    def test_a_multi_file_download_saves_its_torrent_beside_the_folder(self):
        tr = self.serve_tracker()
        t = self.torrent([("a.bin", torrentkit.content(PL)), ("b.bin", b"hi")], name="set")
        self.swarm(tr, self.seeder(t))
        code, events = self.dm(self.magnet(t, tr))
        self.assertEqual(code, 0)
        self.assertEqual(self.listing(), ["set", "set.torrent"])
        self.assertIn("saved set.torrent\n", self.texts(events, "output"))

    # AC12
    def test_a_torrent_that_cannot_be_written_warns_and_still_succeeds(self):
        tr, t = self.seeded()
        from dmcore.torrent import place

        with mock.patch.object(place, "place_file", side_effect=OSError(errno.EACCES, "Permission denied")):
            code, events = self.dm(self.magnet(t, tr))
        self.assertEqual(code, 0)
        self.assertEqual(self.listing(), ["a.bin"])
        self.assertEqual((self.cwd / "a.bin").read_bytes(), t.blob)
        out = "".join(self.texts(events, "output"))
        self.assertEqual(out, "saved a.bin (131.1 kB, 1 file)\n")
        warns = [kw["message"] for k, kw in events if k == "log" and kw.get("level") == "warn"]
        self.assertEqual(warns, ["could not save a.bin.torrent: Permission denied"])


if __name__ == "__main__":
    unittest.main()
