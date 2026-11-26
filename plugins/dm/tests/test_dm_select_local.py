"""dm 0.8.0 file selection in one process: fixture tracker and seeders on 127.0.0.1, no container runtime."""

import os
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "plugins" / "dm"))
sys.path.insert(0, str(ROOT / "tests"))

import test_dm_torrent_local as local
import torrentkit
import dmcore
from dmcore.fmt import human_size
from dmcore.target import DmError, Target
from dmcore.torrent import select, session, storage as storage_mod
from dmcore.torrent.handle import TorrentHandle
from dmcore.torrent.peer import PeerConn

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
        return session.run_torrent(path, Target(self.cwd, None),
                                   lambda pct, text: self.progress.append((pct, text)),
                                   handle or TorrentHandle(), opts, selection=select.parse(only, skip),
                                   on_stats=stats)

    def wanted_pieces(self, t, only=None, skip=None):
        meta = local.metainfo.load(t.write_torrent(self.dir / "w.torrent"))
        specs = local.layout.plan_files(meta)
        chosen = select.resolve(specs, select.parse(only, skip))
        return select.wanted_pieces(specs, chosen, meta.piece_length)


class Download(Selected):
    # AC4
    def test_only_the_picked_files_and_their_folders_are_placed(self):
        t, *_ = self.prepared()
        final, size, nfiles = self.select(t, "a/*")
        self.assertEqual((final, size, nfiles), (self.cwd / "t", 40000, 2))
        self.assertEqual(tree(final), ["a/", "a/1.bin", "a/2.bin"])
        self.assertEqual((final / "a" / "1.bin").read_bytes(), A1)
        self.assertEqual((final / "a" / "2.bin").read_bytes(), A2)
        self.assertEqual(self.listing(), ["t"])

    # AC5
    def test_only_a_range_minus_a_skip(self):
        t, *_ = self.prepared()
        final, size, nfiles = self.select(t, "1-3", "2")
        self.assertEqual((size, nfiles), (50000, 2))
        self.assertEqual(tree(final), ["a/", "a/1.bin", "b/", "b/3.bin"])
        self.assertEqual((final / "b" / "3.bin").read_bytes(), B3)

    def test_skip_alone_downloads_the_rest(self):
        t, *_ = self.prepared()
        final, size, nfiles = self.select(t, None, "b/*")
        self.assertEqual((size, nfiles), (40000, 2))
        self.assertEqual(tree(final), ["a/", "a/1.bin", "a/2.bin"])

    def test_a_single_file_torrent_picked_by_number(self):
        t, *_ = self.prepared([("one.bin", torrentkit.content(30000))], name="one.bin")
        final, size, nfiles = self.select(t, "1")
        self.assertEqual((final, size, nfiles), (self.cwd / "one.bin", 30000, 1))
        self.assertEqual(final.read_bytes(), t.blob)

    def test_a_picked_empty_file_is_placed_and_an_unpicked_one_is_not(self):
        files = [("a.bin", torrentkit.content(20000, "x")), ("e/empty", b""), ("z/empty2", b""),
                 ("c.bin", torrentkit.content(100, "c"))]
        t, *_ = self.prepared(files)
        final, size, nfiles = self.select(t, "e/empty,c.bin")
        self.assertEqual(tree(final), ["c.bin", "e/", "e/empty"])
        self.assertEqual((size, nfiles), (100, 2))

    def test_only_empty_files_picked_finishes_without_any_piece(self):
        files = [("a.bin", torrentkit.content(20000, "x")), ("e/empty", b"")]
        t, _, seeder = self.prepared(files)
        final, size, nfiles = self.select(t, "e/empty")
        self.assertEqual(tree(final), ["e/", "e/empty"])
        self.assertEqual((size, nfiles), (0, 1))
        self.assertEqual(seeder.requests, 0)

    # AC8
    def test_only_the_pieces_of_the_picked_files_are_requested(self):
        t, tr, seeder = self.prepared()
        self.select(t, "a/1.bin")
        self.assertEqual(set(seeder.piece_requests), set(self.wanted_pieces(t, "a/1.bin")))
        self.assertEqual(set(seeder.piece_requests), {0, 1})

    def test_the_download_succeeds_with_a_peer_that_has_only_the_picked_pieces(self):
        t, tr, seeder = self.prepared()
        content = seeder.content[t.info_hash]
        # b/3.bin: all but its first bytes (piece 2 holds them) are gone
        path, size, offset = content.files[2]
        with open(path, "r+b") as f:
            f.seek(PL * 3 - offset)  # from piece 3 on
            f.write(b"\xff" * (size - (PL * 3 - offset)))
        final, *_ = self.select(t, "a/*")
        self.assertEqual(tree(final), ["a/", "a/1.bin", "a/2.bin"])
        self.assertEqual((final / "a" / "2.bin").read_bytes(), A2)

    # AC9
    def test_the_unpicked_file_never_appears_and_nothing_hidden_is_left(self):
        files = [("a/1.bin", torrentkit.content(200_000, "a1")),
                 ("a/2.bin", torrentkit.content(200_000, "a2")),
                 ("b/3.bin", torrentkit.content(300_000, "b3"))]
        t, *_ = self.prepared(files, delay=0.01)
        seen, stop = [], threading.Event()

        def watch():
            while not stop.is_set():
                for dirpath, dirs, names in os.walk(self.cwd):
                    if ".dm-side" in Path(dirpath).parts:
                        continue
                    seen.extend(os.path.join(dirpath, n) for n in dirs + names if n in ("3.bin", "b"))

        watcher = threading.Thread(target=watch)
        watcher.start()
        try:
            final, *_ = self.select(t, "a/*")
        finally:
            stop.set()
            watcher.join()
        self.assertEqual(seen, [])
        self.assertEqual(tree(final), ["a/", "a/1.bin", "a/2.bin"])
        self.assertEqual(self.listing(), ["t"])

    def test_the_side_area_exists_during_the_run_and_is_gone_after(self):
        t, *_ = self.prepared(delay=0.05)
        found = []

        def watch():
            while not stop.is_set():
                found.extend(p for p in self.cwd.glob(".*dm-part/.dm-side"))

        stop = threading.Event()
        watcher = threading.Thread(target=watch)
        watcher.start()
        try:
            final, *_ = self.select(t, "a/*")
        finally:
            stop.set()
            watcher.join()
        self.assertTrue(found)
        self.assertEqual(self.listing(), ["t"])
        self.assertEqual(tree(final), ["a/", "a/1.bin", "a/2.bin"])

    # R13
    def test_progress_counts_only_the_picked_files(self):
        t, *_ = self.prepared()
        rows = []
        self.select(t, "a/1.bin", stats=lambda st, final=False: rows.extend(st()["files"]))
        texts = [text for _, text in self.progress if text and " of " in text]
        self.assertTrue(texts)
        self.assertTrue(all(text.split(" · ")[0].endswith(f"of {human_size(20000)}")
                            for text in texts), texts)
        self.assertEqual(self.progress[-1][0], 100)
        self.assertTrue(all(path == "a/1.bin" for _, path, _, _ in rows))

    def test_announces_report_the_picked_bytes_still_to_verify_as_left(self):
        import re
        t, tr, _ = self.prepared()
        self.select(t, "a/1.bin")
        left = [(a["event"], int(re.search(r"(?:^|&)left=(\d+)", a["query"]).group(1))) for a in tr.announces]
        self.assertEqual(left, [("started", 20000), ("completed", 0), ("stopped", 0)])

    def test_no_selection_keeps_the_whole_torrent_total(self):
        t, *_ = self.prepared()
        self.select(t)
        texts = [text for _, text in self.progress if text and " of " in text]
        self.assertTrue(all(text.split(" · ")[0].endswith(f"of {human_size(70000)}")
                            for text in texts), texts)

    # R6
    def test_a_bad_pick_fails_before_any_traffic_and_creates_nothing(self):
        t, tr, seeder = self.prepared()
        for only in ("*.iso", "9"):
            with self.assertRaises(DmError):
                self.select(t, only)
        self.assertEqual(tr.announces, [])
        self.assertEqual(seeder.connections, 0)
        self.assertEqual(self.listing(), [])


class Cancelling(Selected):
    def test_a_cancel_before_placing_leaves_the_side_area_alone(self):
        t, _, seeder = self.prepared()
        handle = TorrentHandle()
        real_drive = session._drive

        def drive(meta, st, progress, h, opts, log, place, *args, **kw):
            def cancelled_then_place():
                self.assertTrue(h.abort())  # the cancel lands just before the last step
                place()
            return real_drive(meta, st, progress, h, opts, log, cancelled_then_place, *args, **kw)

        with mock.patch.object(session, "_drive", drive), \
                mock.patch.object(storage_mod.Storage, "finish_layout", autospec=True) as finish:
            with self.assertRaises(dmcore.Cancelled):
                self.select(t, "a/1.bin", handle=handle)
        finish.assert_not_called()  # the partial is kept whole: its side data is still there for a resume
        self.assertEqual(len(self.listing()), 1)


class Magnet(Selected):
    """A selection on a magnet link: made once the metadata is there, before any piece is asked for."""

    def run_magnet(self, t, seeder, only=None, skip=None):
        from dmcore.torrent import magnet
        link = torrentkit.magnet_for(t, peers=[("127.0.0.1", seeder.port)])
        return session.run_magnet(magnet.parse(link), Target(self.cwd, None), lambda p, x: None,
                                  TorrentHandle(), local.FAST, selection=select.parse(only, skip))

    def test_a_magnet_downloads_only_the_picked_files(self):
        t, _, seeder = self.prepared()
        final, size, nfiles, info = self.run_magnet(t, seeder, "a/1.bin,b/*")
        self.assertEqual((final, size, nfiles), (self.cwd / "t", 50000, 2))
        self.assertEqual(tree(final), ["a/", "a/1.bin", "b/", "b/3.bin"])
        self.assertEqual((final / "b" / "3.bin").read_bytes(), B3)
        self.assertEqual(set(seeder.piece_requests), set(self.wanted_pieces(t, "a/1.bin,b/*")))
        self.assertEqual(self.listing(), ["t"])

    def test_a_bad_pick_fails_after_the_metadata_before_any_piece(self):
        t, _, seeder = self.prepared()
        with self.assertRaises(DmError) as cm:
            self.run_magnet(t, seeder, "*.iso")
        self.assertEqual(str(cm.exception), "no file matches '*.iso'")
        self.assertEqual(seeder.requests, 0)
        self.assertTrue(cm.exception.keep)
        self.assertEqual(self.listing(), [self.meta_name(t)])  # the metadata only: no partial was created

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
        code = dmcore.run({"url": arg, "no_dht": True, **inputs}, str(self.cwd),
                          lambda method, **p: events.append((method, p)), threading.Event())
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
    def test_list_prints_every_file_and_a_total_and_creates_nothing(self):
        _, tr, seeder = self.served()
        self.assertEqual(self.dm("data.torrent", list=True), 0, self.errors())
        lines = self.output().splitlines()
        self.assertEqual(len(lines), 5)
        self.assertEqual(lines[0].split(), ["#", "size", "path"])
        self.assertEqual([l.split()[0] for l in lines[1:4]], ["1", "2", "3"])
        self.assertEqual([l.split()[-1] for l in lines[1:4]], ["a/1.bin", "a/2.bin", "b/3.bin"])
        self.assertEqual(lines[4], "3 files, 70.0 kB")
        self.assertEqual(self.listing(), ["data.torrent"])
        self.assertEqual(tr.announces, [])
        self.assertEqual(seeder.connections, 0)
        self.assertFalse([1 for m, _ in self.events if m == "bar"])

    # AC3
    def test_list_with_a_selection_lists_only_the_picked_files_with_their_numbers(self):
        self.served()
        self.assertEqual(self.dm("data.torrent", list=True, only="a/*", skip="2"), 0, self.errors())
        lines = self.output().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertEqual((lines[1].split()[0], lines[1].split()[-1]), ("1", "a/1.bin"))
        self.assertEqual(lines[2], "1 file, 20.0 kB")
        self.assertEqual(self.listing(), ["data.torrent"])

    def test_list_of_a_single_file_torrent(self):
        self.served([("one.bin", torrentkit.content(5000))], "one.bin")
        self.assertEqual(self.dm("data.torrent", list=True), 0, self.errors())
        self.assertEqual(self.output().splitlines()[1].split()[-1], "one.bin")

    def test_list_with_a_bad_pick_names_the_torrent_and_the_pick(self):
        self.served()
        self.assertEqual(self.dm("data.torrent", list=True, only="*.iso"), 1)
        self.assertEqual(self.errors(), ["dm: data.torrent: no file matches '*.iso'"])
        self.assertEqual(self.output(), "")

    def test_list_of_an_unreadable_torrent_is_an_error(self):
        (self.cwd / "bad.torrent").write_bytes(b"not a torrent")
        self.assertEqual(self.dm("bad.torrent", list=True), 1)
        self.assertTrue(self.errors()[0].startswith("dm: bad.torrent: "), self.errors())

    # AC4
    def test_only_through_run_prints_the_saved_line_of_the_picked_files(self):
        self.served()
        self.assertEqual(self.dm("data.torrent", only="a/*"), 0, self.errors())
        self.assertEqual(self.output(), "saved t (40.0 kB, 2 files)\n")
        self.assertEqual(tree(self.cwd / "t"), ["a/", "a/1.bin", "a/2.bin"])

    def test_skip_through_run(self):
        self.served()
        self.assertEqual(self.dm("data.torrent", skip="*.bin,"), 1)
        self.assertEqual(self.errors(), ["dm: data.torrent: bad pick ''"])
        self.assertEqual(self.dm("data.torrent", skip="b/*"), 0, self.errors())
        self.assertEqual(self.output(), "saved t (40.0 kB, 2 files)\n")

    def test_without_flags_nothing_changes(self):
        self.served()
        self.assertEqual(self.dm("data.torrent", list=False), 0, self.errors())
        self.assertEqual(self.output(), "saved t (70.0 kB, 3 files)\n")
        self.assertEqual(tree(self.cwd / "t"), ["a/", "a/1.bin", "a/2.bin", "b/", "b/3.bin"])

    # AC6
    def test_bad_picks_fail_with_their_name_before_any_traffic(self):
        _, tr, seeder = self.served()
        cases = (({"only": "*.iso"}, "no file matches '*.iso'"),
                 ({"only": "9"}, "file number 9 is out of range (1-3)"),
                 ({"only": "3-1"}, "bad pick '3-1'"), ({"only": ","}, "bad pick ''"),
                 ({"only": "1", "skip": "1"}, "no file selected"),
                 ({"skip": "*.iso"}, "no file matches '*.iso'"))
        for inputs, text in cases:
            with self.subTest(inputs=inputs):
                self.assertEqual(self.dm("data.torrent", **inputs), 1)
                self.assertEqual(self.errors(), [f"dm: data.torrent: {text}"])
        self.assertEqual(tr.announces, [])
        self.assertEqual(seeder.connections, 0)
        self.assertEqual(self.listing(), ["data.torrent"])

    def test_a_huge_number_in_a_pick_is_labelled_and_names_the_pick(self):
        self.served()
        huge = "9" * 5000
        self.assertEqual(self.dm("data.torrent", only=huge), 1)
        self.assertEqual(self.errors(), [f"dm: data.torrent: bad pick '{huge}'"])

    # AC7
    def test_flags_with_an_http_url_are_an_error_before_any_request(self):
        for inputs in ({"only": "1"}, {"skip": "1"}, {"list": True}):
            with self.subTest(inputs=inputs):
                with mock.patch.object(dmcore.fetch, "download", side_effect=AssertionError("requested")), \
                        mock.patch.object(dmcore.fetch, "check_url", side_effect=AssertionError("requested")):
                    self.assertEqual(self.dm("http://127.0.0.1:1/x.bin", **inputs), 1)
                self.assertEqual(self.errors(),
                                 ["dm: --only, --skip and --list are for torrents and magnets"])

    def test_flags_with_a_missing_torrent_keep_the_normal_not_found_error(self):
        self.assertEqual(self.dm("missing.torrent"), 1)
        plain = self.errors()
        self.assertNotIn("--only", plain[0])
        for inputs in ({"only": "1"}, {"skip": "1"}, {"list": True}):
            with self.subTest(inputs=inputs):
                self.assertEqual(self.dm("missing.torrent", **inputs), 1)
                self.assertEqual(self.errors(), plain)

    # AC2
    def test_list_with_a_magnet_lists_after_the_metadata_and_requests_no_piece(self):
        t, tr, seeder = self.prepared()
        link = torrentkit.magnet_for(t, peers=[("127.0.0.1", seeder.port)])
        self.assertEqual(self.dm(link, list=True), 0, self.errors())
        lines = self.output().splitlines()
        self.assertEqual([l.split()[-1] for l in lines[1:4]], ["a/1.bin", "a/2.bin", "b/3.bin"])
        self.assertEqual(lines[4], "3 files, 70.0 kB")
        self.assertEqual(dict(seeder.piece_requests), {})
        self.assertEqual(self.listing(), [])  # no partial, no .dm-meta, no lock left behind

    def test_list_with_a_magnet_and_a_pick_filters_like_a_torrent(self):
        t, tr, seeder = self.prepared()
        link = torrentkit.magnet_for(t, peers=[("127.0.0.1", seeder.port)])
        self.assertEqual(self.dm(link, list=True, only="b/*"), 0, self.errors())
        lines = self.output().splitlines()
        self.assertEqual([l.split()[0] for l in lines[1:-1]], ["3"])
        self.assertEqual(lines[-1], "1 file, 30.0 kB")
        self.assertEqual(self.listing(), [])

    def test_list_with_a_magnet_shows_the_metadata_progress_and_leaves_no_bar(self):
        t, tr, seeder = self.prepared(metadata="silent")
        seeder2 = self.seeder(t)
        self.swarm(tr, seeder, seeder2)
        link = torrentkit.magnet_for(t, peers=[("127.0.0.1", seeder.port), ("127.0.0.1", seeder2.port)])
        self.assertEqual(self.dm(link, list=True), 0, self.errors())
        shown = [(m, p) for m, p in self.events if m not in ("output", "log")]
        self.assertTrue(any("fetching metadata" in str(p) or "finding peers" in str(p) for _, p in shown),
                        shown)
        self.assertEqual(shown[-1][0], "clear")  # the bar is gone once the files are listed

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

    def test_a_bad_pick_on_a_magnet_keeps_the_magnet_label(self):
        t, *_ = self.prepared()
        link = torrentkit.magnet_for(t, peers=[("127.0.0.1", 9)])
        self.assertEqual(self.dm(link, only="0"), 1)
        label = f"magnet {t.info_hash.hex()[:8]}…"
        self.assertEqual(self.errors(), [f"dm: {label}: bad pick '0'"])


class ListMagnet(Selected):
    """session.list_magnet: --list of a magnet never deletes, reports progress and can be cancelled."""

    def listed(self, link, handle=None, progress=None):
        from dmcore.torrent import magnet
        return session.list_magnet(magnet.parse(link), Target(self.cwd, None), handle, local.FAST,
                                   progress=progress)

    def meta_path(self, t):
        from dmcore import resume
        return Path(resume.meta_path(self.cwd, resume.torrent_key(t.info_hash, Target(self.cwd, None))))

    def test_a_corrupt_stored_metadata_survives_list_and_the_metadata_comes_from_peers(self):
        t, tr, seeder = self.prepared()
        link = torrentkit.magnet_for(t, peers=[("127.0.0.1", seeder.port)])
        mpath = self.meta_path(t)
        mpath.write_bytes(b"not the info dictionary")
        meta = self.listed(link)
        self.assertEqual(len(meta.files), 3)
        self.assertEqual(mpath.read_bytes(), b"not the info dictionary")
        self.assertEqual(self.listing(), [mpath.name])

    def test_a_symlinked_stored_metadata_survives_list_and_its_target_is_untouched(self):
        t, tr, seeder = self.prepared()
        link = torrentkit.magnet_for(t, peers=[("127.0.0.1", seeder.port)])
        outside = self.dir / "outside"
        outside.write_bytes(b"secret")
        mpath = self.meta_path(t)
        mpath.symlink_to(outside)
        self.assertEqual(len(self.listed(link).files), 3)
        self.assertTrue(mpath.is_symlink())
        self.assertEqual(os.readlink(mpath), str(outside))
        self.assertEqual(outside.read_bytes(), b"secret")
        self.assertEqual(self.listing(), [mpath.name])

    def test_list_reports_the_metadata_progress(self):
        t, tr, seeder = self.prepared(delay=0.0)
        link = torrentkit.magnet_for(t, peers=[("127.0.0.1", seeder.port)])
        seen = []
        self.listed(link, progress=lambda pct, text: seen.append((pct, text)))
        self.assertTrue(seen)
        self.assertTrue(all(pct is None and ("fetching metadata" in text or "finding peers" in text)
                            for pct, text in seen), seen)

    def test_cancelling_list_with_a_peer_that_never_answers_ends_it_and_leaves_nothing(self):
        t, tr, seeder = self.prepared(metadata="silent")
        link = torrentkit.magnet_for(t, peers=[("127.0.0.1", seeder.port)])
        handle, result, seen = TorrentHandle(), {}, []

        def work():
            try:
                result["ok"] = self.listed(link, handle, lambda pct, text: seen.append(text))
            except BaseException as e:
                result["err"] = e

        thread = threading.Thread(target=work)
        thread.start()
        self.assertTrue(self.wait_for(lambda: any("fetching metadata" in x for x in list(seen)), 10))
        self.assertTrue(handle.abort())
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(result.get("err"), dmcore.Cancelled, result)
        self.assertEqual(self.listing(), [])


class Serving(Selected):
    """AC10 (R14): dm tells peers only about verified pieces and sends nothing it does not have."""

    def make(self, have):
        t, *_ = self.prepared()
        meta = local.metainfo.load(t.write_torrent(self.dir / "s.torrent"))
        specs = local.layout.plan_files(meta)
        root = self.dir / "part"
        os.mkdir(root)
        st = storage_mod.Storage(root, specs, meta.piece_length, meta.total, single=False, wanted={0})
        st.prepare()
        self.addCleanup(st.close)
        s = session.Session(meta, st, lambda *a: None, TorrentHandle(), session.Options(dht=False), have=have)
        self.addCleanup(s.pool.shutdown, wait=False)
        self.addCleanup(s.reader_pool.shutdown, wait=False)
        return s

    def test_the_bitfield_lists_only_the_verified_pieces(self):
        s = self.make(have=(0,))
        self.assertEqual(s.bitfield(), bytes([0b10000000]))
        self.assertEqual(s.picker.finished, {0})

    def test_a_request_for_a_piece_not_verified_closes_the_peer(self):
        s = self.make(have=(0,))
        c = PeerConn(s, ("127.0.0.1", 1))
        c.handshaken = True
        c.am_choking = False
        from dmcore.torrent import wire
        for index in (1, 2, 4):  # wanted but not yet verified, unwanted, unwanted
            with self.assertRaises(ValueError):
                c.on_message(wire.REQUEST, wire.request(index, 0, PL)[5:], 0)

    def test_the_picker_wants_only_the_pieces_of_the_picked_files(self):
        s = self.make(have=())
        self.assertEqual(s.picker.missing, {0, 1})


if __name__ == "__main__":
    unittest.main()
