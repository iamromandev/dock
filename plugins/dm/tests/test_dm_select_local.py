"""dm 0.8.0 file selection in one process: fixture tracker and seeders on 127.0.0.1, no container runtime."""

import os
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import test_dm_torrent_local as local
import torrentkit
import dmcore
from dmcore.target import DmError, Target
from dmcore.torrent import select, session
from dmcore.torrent.handle import TorrentHandle

PL = 16384
A1 = torrentkit.content(20000, "a1")
A2 = torrentkit.content(20000, "a2")
B3 = torrentkit.content(30000, "b3")
FILES = [("a/1.bin", A1), ("a/2.bin", A2), ("b/3.bin", B3)]  # 70000 bytes: piece 2 is a/2.bin + b/3.bin


def tree(root):
    out = []
    for dirpath, dirs, files in os.walk(root):
        rel = Path(dirpath).relative_to(root)
        out += [f"{(rel / d).as_posix()}/" for d in dirs] + [(rel / f).as_posix() for f in files]
    return sorted(out)


class Selected(local.Harness):
    """run_torrent / run_magnet with a selection, against the fixture tracker and seeders."""

    def prepared(self, files=FILES, name="t", delay=0.0, piece_length=PL, **kw):
        tr = self.serve_tracker()
        t = self.torrent(files, name=name, tr=tr, piece_length=piece_length)
        seeder = self.seeder(t, delay=delay, **kw)
        self.swarm(tr, seeder)
        return t, tr, seeder

    def select(self, t, only=None, skip=None, opts=local.FAST, handle=None, stats=None):
        path = t.write_torrent(self.dir / "x.torrent")
        self.progress = []
        return session.run_torrent(
            path,
            Target(self.cwd, None),
            lambda pct, text: self.progress.append((pct, text)),
            handle or TorrentHandle(),
            opts,
            selection=select.parse(only, skip),
            on_stats=stats,
        )

    def wanted_pieces(self, t, only=None, skip=None):
        meta = local.metainfo.load(t.write_torrent(self.dir / "w.torrent"))
        specs = local.layout.plan_files(meta)
        chosen = select.resolve(specs, select.parse(only, skip))
        return select.wanted_pieces(specs, chosen, meta.piece_length)


class Magnet(Selected):
    """A selection on a magnet link: made once the metadata is there, before any piece is asked for."""

    def run_magnet(self, t, seeder, only=None, skip=None):
        from dmcore.torrent import magnet

        link = torrentkit.magnet_for(t, peers=[("127.0.0.1", seeder.port)])
        return session.run_magnet(
            magnet.parse(link),
            Target(self.cwd, None),
            lambda p, x: None,
            TorrentHandle(),
            local.FAST,
            selection=select.parse(only, skip),
        )

    def meta_name(self, t):
        from dmcore import resume

        key = resume.torrent_key(t.info_hash, Target(self.cwd, None))
        return resume.meta_path(self.cwd, key).name

    def test_a_retry_after_a_bad_pick_succeeds_from_the_stored_metadata(self):
        t, _, seeder = self.prepared()
        with self.assertRaises(DmError):
            self.run_magnet(t, seeder, "*.iso")
        self.assertEqual(self.listing(), [self.meta_name(t)])
        final, size, nfiles, info = self.run_magnet(t, seeder, "a/1.bin")
        self.assertEqual(tree(final), ["a/", "a/1.bin"])
        self.assertEqual(self.listing(), ["t"])


class Run(Selected):
    """dmcore.run with a fake notify, the way the plugin calls it: --list, --only, --skip."""

    def dm(self, arg, **inputs):
        events = []
        self.outputs = {}
        code = dmcore.run(
            {"url": arg, "no_dht": True, **inputs},
            str(self.cwd),
            lambda method, **p: events.append((method, p)),
            threading.Event(),
            outputs=self.outputs,
        )
        self.events = events
        return code

    def errors(self):
        return [p["message"] for m, p in self.events if m == "log" and p["level"] == "error"]

    def output(self):
        return "".join(p["text"] for m, p in self.events if m == "output")

    def served(self, files=FILES, name="t"):
        t, tr, seeder = self.prepared(files, name)
        t.write_torrent(self.cwd / "data.torrent")
        return t, tr, seeder

    # AC1

    # AC3

    def test_list_of_a_single_file_torrent(self):
        self.served([("one.bin", torrentkit.content(5000))], "one.bin")
        self.assertEqual(self.dm("data.torrent", list=True), 0, self.errors())
        self.assertEqual(self.output().splitlines()[1].split()[-1], "one.bin")

    # AC4

    # AC6
    def test_bad_picks_fail_with_their_name_before_any_traffic(self):
        _, tr, seeder = self.served()
        cases = (
            ({"only": "*.iso"}, "no file matches '*.iso'"),
            ({"only": "9"}, "file number 9 is out of range (1-3)"),
            ({"only": "3-1"}, "bad pick '3-1'"),
            ({"only": ","}, "bad pick ''"),
            ({"only": "1", "skip": "1"}, "no file selected"),
            ({"skip": "*.iso"}, "no file matches '*.iso'"),
        )
        for inputs, text in cases:
            with self.subTest(inputs=inputs):
                self.assertEqual(self.dm("data.torrent", **inputs), 1)
                self.assertEqual(self.errors(), [f"dm: data.torrent: {text}"])
        self.assertEqual(tr.announces, [])
        self.assertEqual(seeder.connections, 0)
        self.assertEqual(self.listing(), ["data.torrent"])

    # AC7

    def test_flags_with_a_missing_torrent_keep_the_normal_not_found_error(self):
        self.assertEqual(self.dm("missing.torrent"), 1)
        plain = self.errors()
        self.assertNotIn("--only", plain[0])
        for inputs in ({"only": "1"}, {"skip": "1"}, {"list": True}):
            with self.subTest(inputs=inputs):
                self.assertEqual(self.dm("missing.torrent", **inputs), 1)
                self.assertEqual(self.errors(), plain)

    # AC2

    def test_list_with_a_magnet_and_a_pick_filters_like_a_torrent(self):
        t, tr, seeder = self.prepared()
        link = torrentkit.magnet_for(t, peers=[("127.0.0.1", seeder.port)])
        self.assertEqual(self.dm(link, list=True, only="b/*"), 0, self.errors())
        lines = self.output().splitlines()
        self.assertEqual([l.split()[0] for l in lines[1:-1]], ["3"])
        self.assertEqual(lines[-1], "1 file, 30.0 kB")
        self.assertEqual(self.listing(), [])

    def test_list_with_a_magnet_and_a_pick_that_matches_nothing_fails_with_the_label(self):
        t, tr, seeder = self.prepared()
        link = torrentkit.magnet_for(t, peers=[("127.0.0.1", seeder.port)])
        self.assertEqual(self.dm(link, list=True, only="*.iso"), 1)
        self.assertEqual(self.errors(), [f"dm: magnet {t.info_hash.hex()[:8]}…: no file matches '*.iso'"])
        self.assertEqual(dict(seeder.piece_requests), {})
        self.assertEqual(self.output(), "")
        self.assertEqual(self.listing(), [])

    def test_list_with_a_magnet_uses_the_stored_metadata_and_does_not_touch_it(self):
        from dmcore import resume
        from dmcore.torrent import bencode

        t, tr, seeder = self.prepared()
        link = torrentkit.magnet_for(t, peers=[("127.0.0.1", seeder.port)])
        key = resume.torrent_key(t.info_hash, Target(self.cwd, None))
        mpath = resume.meta_path(self.cwd, key)
        resume.write_meta(mpath, t.data[slice(*bencode.decode(t.data)[1])])
        before = Path(mpath).read_bytes()
        self.assertEqual(self.dm(link, list=True), 0, self.errors())
        self.assertEqual(seeder.connections, 0)  # no peer was asked
        self.assertEqual(len(self.output().splitlines()), 5)
        self.assertEqual(Path(mpath).read_bytes(), before)
        self.assertEqual(len(self.listing()), 1)


if __name__ == "__main__":
    unittest.main()
