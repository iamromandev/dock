"""Unit tests for the torrent support in the dm plugin (plugins/dm/dmcore/torrent)."""

import errno
import hashlib
import os
import resource
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "plugins" / "dm"))

from dmcore import fetch, names
from dmcore.target import DmError
from dmcore.torrent import bencode, layout, metainfo, place, storage


class Bencode(unittest.TestCase):
    def test_decodes_basic_types(self):
        value, span = bencode.decode(b"d1:ai-3e1:bl1:x3:abcee")
        self.assertEqual(value, {b"a": -3, b"b": [b"x", b"abc"]})
        self.assertIsNone(span)

    def test_rejects_malformed_input(self):
        cases = {
            "leading zero": b"i03e",
            "negative zero": b"i-0e",
            "empty int": b"ie",
            "bare minus": b"i-e",
            "leading zero length": b"01:a",
            "truncated string": b"5:abc",
            "truncated int": b"i12",
            "truncated list": b"l1:a",
            "truncated dict": b"d1:ai1e",
            "empty input": b"",
            "trailing data": b"i1ex",
            "int key": b"di1ei2ee",
            "unsorted keys": b"d1:bi1e1:ai2ee",
            "duplicate keys": b"d1:ai1e1:ai2ee",
            "unknown token": b"x",
            "dict missing value": b"d1:ae",
        }
        for label, data in cases.items():
            with self.subTest(label):
                with self.assertRaises(bencode.BencodeError):
                    bencode.decode(data)

    def test_depth_64_is_fine_and_65_fails(self):
        ok = b"l" * 64 + b"e" * 64
        bencode.decode(ok)
        with self.assertRaises(bencode.BencodeError):
            bencode.decode(b"l" * 65 + b"e" * 65)

    def test_info_span_with_other_keys_around(self):
        info = b"d4:name1:xe"
        data = b"d1:a1:z4:info" + info + b"1:zi1ee"
        value, span = bencode.decode(data)
        self.assertEqual(data[span[0]:span[1]], info)
        self.assertEqual(value[b"info"], {b"name": b"x"})

    def test_info_span_only_for_top_level(self):
        _, span = bencode.decode(b"d1:ad4:infoi1eee")
        self.assertIsNone(span)

    def test_round_trip(self):
        value = {b"a": [1, b"x", {b"k": -5}], b"b": b""}
        self.assertEqual(bencode.decode(bencode.encode(value))[0], value)

    def test_encode_sorts_keys_and_accepts_str(self):
        self.assertEqual(bencode.encode({"b": 1, b"a": "é"}), b"d1:a2:\xc3\xa91:bi1ee")


PL = 16384


def make_torrent(info=None, top=None, **info_over):
    """A valid single-file torrent (2 pieces) built with encode; overrides replace info keys."""
    base = {"name": "a.bin", "piece length": PL, "length": PL + 5, "pieces": b"\x01" * 40}
    base.update(info if info is not None else {})
    base.update(info_over)
    base = {k: v for k, v in base.items() if v is not None}
    outer = {"info": base}
    outer.update(top or {})
    return bencode.encode(outer)


class Metainfo(unittest.TestCase):
    def invalid(self, data, reason=None):
        with self.assertRaises(metainfo.TorrentError) as cm:
            metainfo.parse(data)
        self.assertTrue(str(cm.exception).startswith("invalid torrent: "), str(cm.exception))
        if reason:
            self.assertIn(reason, str(cm.exception))

    def test_single_file(self):
        m = metainfo.parse(make_torrent())
        self.assertEqual((m.name, m.piece_length, m.total, m.multi), ("a.bin", PL, PL + 5, False))
        self.assertEqual(m.files, (metainfo.FileEntry(("a.bin",), PL + 5),))
        self.assertEqual(m.hashes, (b"\x01" * 20, b"\x01" * 20))

    def test_multi_file(self):
        files = [{"length": PL, "path": [b"d", b"x"]}, {"length": 5, "path": [b"y"]}]
        m = metainfo.parse(make_torrent(length=None, files=files))
        self.assertTrue(m.multi)
        self.assertEqual(m.total, PL + 5)
        self.assertEqual(m.files, (metainfo.FileEntry(("d", "x"), PL), metainfo.FileEntry(("y",), 5)))

    def test_info_hash_is_sha1_of_original_bytes(self):
        # hand-built info dict with keys in order but an unusual (still valid) layout
        info = (b"d6:lengthi%de4:name5:a.bin12:piece lengthi%de6:pieces40:" % (PL + 5, PL)
                + b"\x01" * 40 + b"e")
        data = b"d8:announce3:foo4:info" + info + b"e"
        self.assertEqual(metainfo.parse(data).info_hash, hashlib.sha1(info).digest())

    def test_not_a_dict_or_garbage(self):
        self.invalid(b"i1e")
        self.invalid(b"garbage")
        self.invalid(bencode.encode({"x": 1}), "info")
        self.invalid(bencode.encode({"info": [1]}), "info")

    def test_requires_name_piece_length_pieces(self):
        for key in ("name", "piece length", "pieces"):
            with self.subTest(key):
                self.invalid(make_torrent(**{key: None}))

    def test_requires_length_or_files(self):
        self.invalid(make_torrent(length=None))
        self.invalid(make_torrent(length=None, files=[]))

    def test_wrong_types(self):
        self.invalid(make_torrent(name=5))
        self.invalid(make_torrent(length=b"x"))
        self.invalid(make_torrent(length=-1))
        self.invalid(make_torrent(length=None, files=[{"length": 1}]))
        self.invalid(make_torrent(length=None, files=[{"length": 1, "path": []}]))
        self.invalid(make_torrent(length=None, files=[{"length": 1, "path": [5]}]))
        self.invalid(make_torrent(length=None, files=[{"length": -1, "path": [b"a"]}]))
        self.invalid(make_torrent(length=None, files=[5]))

    def test_piece_length_power_of_two_in_range(self):
        for bad in (PL + 1, 3 * PL, PL // 2, 0, 2 ** 25):
            with self.subTest(bad):
                self.invalid(make_torrent(**{"piece length": bad}), "piece length")
        for good in (PL, 2 ** 24):
            with self.subTest(good):
                total = good + 1
                metainfo.parse(make_torrent(**{"piece length": good, "length": total,
                                               "pieces": b"\x00" * 40}))

    def test_pieces_length_must_be_multiple_of_20(self):
        self.invalid(make_torrent(pieces=b"\x01" * 41), "pieces")

    def test_piece_count_must_match_total(self):
        self.invalid(make_torrent(pieces=b"\x01" * 20), "pieces")
        self.invalid(make_torrent(pieces=b"\x01" * 60), "pieces")

    def test_max_pieces(self):
        n = metainfo.MAX_PIECES + 1
        data = make_torrent(length=n * PL, pieces=b"\x00" * (20 * n))
        self.invalid(data, "pieces")

    def test_max_pieces_boundary_is_accepted(self):
        n = metainfo.MAX_PIECES
        metainfo.parse(make_torrent(length=n * PL, pieces=b"\x00" * (20 * n)))

    def test_total_zero(self):
        self.invalid(make_torrent(length=0, pieces=b""))
        self.invalid(make_torrent(length=None, files=[{"length": 0, "path": [b"a"]}], pieces=b""))

    def test_v2_only_is_rejected_with_its_own_message(self):
        data = make_torrent(pieces=None, **{"meta version": 2})
        with self.assertRaises(metainfo.TorrentError) as cm:
            metainfo.parse(data)
        self.assertEqual(str(cm.exception), "v2-only torrents are not supported")

    def test_hybrid_is_accepted_as_v1(self):
        m = metainfo.parse(make_torrent(**{"meta version": 2}))
        self.assertEqual(m.total, PL + 5)

    def test_missing_pieces_without_v2_is_invalid(self):
        self.invalid(make_torrent(pieces=None))

    def test_strict_bencode_errors_become_invalid_torrent(self):
        self.invalid(make_torrent() + b"x")
        self.invalid(make_torrent()[:-3])

    def test_trackers_from_announce_list_filtered(self):
        top = {"announce": "http://ignored/a",
               "announce-list": [["http://a/x", "udp://b:1", "ftp://c/", "ws://d"], [], ["wss://e"],
                                 ["https://f/a"], [b"udp://g:2"], [5]]}
        m = metainfo.parse(make_torrent(top=top))
        self.assertEqual(m.trackers, (("http://a/x", "udp://b:1"), ("https://f/a",), ("udp://g:2",)))

    def test_trackers_fall_back_to_announce(self):
        m = metainfo.parse(make_torrent(top={"announce": "udp://t:1"}))
        self.assertEqual(m.trackers, (("udp://t:1",),))
        m = metainfo.parse(make_torrent(top={"announce": "magnet:x"}))
        self.assertEqual(m.trackers, ())
        self.assertEqual(metainfo.parse(make_torrent()).trackers, ())

    def test_announce_list_empty_falls_back_to_announce(self):
        m = metainfo.parse(make_torrent(top={"announce": "http://t/", "announce-list": []}))
        self.assertEqual(m.trackers, (("http://t/",),))

    def test_load_reads_a_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.torrent"
            p.write_bytes(make_torrent())
            self.assertEqual(metainfo.load(p).name, "a.bin")

    def test_load_rejects_a_file_over_10_mb_by_size(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "big.torrent"
            with open(p, "wb") as f:
                f.truncate(metainfo.MAX_TORRENT_BYTES + 1)
            with self.assertRaises(metainfo.TorrentError) as cm:
                metainfo.load(p)
            self.assertEqual(str(cm.exception), "invalid torrent: file too large")

    def test_load_missing_file_is_a_dm_error(self):
        with self.assertRaises(DmError):
            metainfo.load(Path(tempfile.gettempdir()) / "definitely-missing.torrent")


def meta_of(name="top", parts=None, lengths=None, single=False):
    """A Meta built directly (no bencode), for layout and storage tests."""
    if single:
        files = (metainfo.FileEntry((name,), 10),)
    else:
        lengths = lengths or [1] * len(parts)
        files = tuple(metainfo.FileEntry(tuple(p), n) for p, n in zip(parts, lengths))
    total = sum(f.length for f in files)
    return metainfo.Meta(info_hash=b"\0" * 20, name=name, piece_length=PL, hashes=(b"\0" * 20,),
                         total=total, files=files, multi=not single, trackers=())


class Layout(unittest.TestCase):
    def unsafe(self, meta, message="invalid torrent: unsafe path"):
        with self.assertRaises(metainfo.TorrentError) as cm:
            layout.plan_files(meta)
        self.assertEqual(str(cm.exception), message)

    def test_traversal_and_unsafe_parts_are_refused(self):
        cases = {
            "dotdot": [".."],
            "dotdot inside": ["a", "..", "..", "x"],
            "absolute": ["/etc/passwd"],
            "backslash": ["a\\..\\b"],
            "nul": ["a\x00b"],
            "empty": [""],
            "dot": ["."],
            "dot inside": ["a", ".", "b"],
        }
        for label, parts in cases.items():
            with self.subTest(label):
                self.unsafe(meta_of(parts=[parts]))

    def test_unsafe_name_is_refused(self):
        for name in ("../x", ".", "..", "", "a/b", "a\\b", "a\x00b"):
            with self.subTest(name):
                self.unsafe(meta_of(name=name, parts=[["f"]]))
                self.unsafe(meta_of(name=name, single=True))

    def test_conflicting_paths(self):
        message = "invalid torrent: conflicting paths"
        self.unsafe(meta_of(parts=[["a"], ["a"]]), message)
        self.unsafe(meta_of(parts=[["a"], ["a", "b"]]), message)
        self.unsafe(meta_of(parts=[["a", "b"], ["a"]]), message)
        self.unsafe(meta_of(parts=[["a", "b"], ["a", "b", "c"]]), message)

    def test_paths_that_clean_to_the_same_name_conflict(self):
        self.unsafe(meta_of(parts=[[".a"], ["a"]]), "invalid torrent: conflicting paths")

    def test_depth_limit(self):
        layout.plan_files(meta_of(parts=[["d"] * 32]))
        self.unsafe(meta_of(parts=[["d"] * 33]), "invalid torrent: path too deep")

    def test_normal_and_unicode_names(self):
        specs = layout.plan_files(meta_of(parts=[["Álbum", "日本語.flac"], ["x y", "z.txt"]],
                                          lengths=[3, 4]))
        self.assertEqual(specs, (layout.FileSpec(("Álbum", "日本語.flac"), 3, 0),
                                 layout.FileSpec(("x y", "z.txt"), 4, 3)))

    def test_leading_dot_is_cleaned_like_safe_name(self):
        specs = layout.plan_files(meta_of(parts=[[".config"]]))
        self.assertEqual(specs[0].parts, ("config",))

    def test_control_chars_are_cleaned(self):
        specs = layout.plan_files(meta_of(parts=[["a\x07b\u202ec"]]))
        self.assertEqual(specs[0].parts, ("abc",))

    def test_single_file_uses_the_cleaned_name(self):
        specs = layout.plan_files(meta_of(name=".hidden.txt", single=True))
        self.assertEqual(specs, (layout.FileSpec(("hidden.txt",), 10, 0),))
        self.assertEqual(layout.item_name(meta_of(name=".hidden.txt", single=True)), "hidden.txt")

    def test_offsets_are_cumulative_in_order_with_zero_length_files(self):
        specs = layout.plan_files(meta_of(parts=[["a"], ["b"], ["c"]], lengths=[5, 0, 7]))
        self.assertEqual([s.offset for s in specs], [0, 5, 5])


def spec(parts, length, offset):
    return layout.FileSpec(tuple(parts), length, offset)


class Storage(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def multi(self, piece_length=4):
        # a:3 bytes, empty:0, sub/b:6 (nested), c:2 -> total 11, pieces 4,4,3
        specs = (spec(["a"], 3, 0), spec(["empty"], 0, 3), spec(["sub", "deep", "b"], 6, 3),
                 spec(["c"], 2, 9))
        root = self.dir / "root"
        os.mkdir(root)
        st = storage.Storage(root, specs, piece_length, 11, single=False)
        st.prepare()
        self.addCleanup(st.close)
        return st, root

    def test_piece_sizes_with_a_short_last_piece(self):
        st, _ = self.multi()
        self.assertEqual([st.piece_size(i) for i in range(3)], [4, 4, 3])

    def test_piece_size_out_of_range(self):
        st, _ = self.multi()
        for i in (-1, 3):
            with self.assertRaises(ValueError):
                st.piece_size(i)

    def test_prepare_creates_sparse_files_folders_and_empty_files(self):
        _, root = self.multi()
        self.assertEqual((root / "a").stat().st_size, 3)
        self.assertEqual((root / "empty").stat().st_size, 0)
        self.assertEqual((root / "sub" / "deep" / "b").stat().st_size, 6)
        self.assertEqual((root / "c").read_bytes(), b"\0\0")

    def test_piece_spanning_three_files_with_an_empty_one_between(self):
        st, root = self.multi(piece_length=8)  # pieces: 8, 3
        st.write_piece(0, b"ABCDEFGH")  # a(3) + empty + b(5 of 6)
        st.write_piece(1, b"xyz")
        self.assertEqual((root / "a").read_bytes(), b"ABC")
        self.assertEqual((root / "sub" / "deep" / "b").read_bytes(), b"DEFGHx")
        self.assertEqual((root / "c").read_bytes(), b"yz")

    def test_write_then_read_back_across_boundaries(self):
        st, _ = self.multi()
        st.write_piece(0, b"0123")
        st.write_piece(1, b"4567")
        st.write_piece(2, b"89A")
        self.assertEqual(st.read_block(0, 0, 4), b"0123")
        self.assertEqual(st.read_block(1, 1, 3), b"567")
        self.assertEqual(st.read_block(2, 0, 3), b"89A")

    def test_write_piece_requires_the_exact_size(self):
        st, _ = self.multi()
        for data in (b"123", b"12345"):
            with self.assertRaises(ValueError):
                st.write_piece(0, data)
        with self.assertRaises(ValueError):
            st.write_piece(2, b"1234")
        with self.assertRaises(ValueError):
            st.write_piece(3, b"1")

    def test_read_block_bounds(self):
        st, _ = self.multi()
        bad = [(0, -1, 1), (0, 0, -1), (0, 3, 2), (0, 5, 0), (2, 0, 4), (3, 0, 1), (-1, 0, 1)]
        for index, begin, length in bad:
            with self.subTest((index, begin, length)):
                with self.assertRaises(ValueError):
                    st.read_block(index, begin, length)
        self.assertEqual(st.read_block(2, 3, 0), b"")

    def test_single_file_mode(self):
        path = self.dir / ".x.dm-part"
        st = storage.Storage(path, (spec(["x"], 10, 0),), 4, 10, single=True)
        st.prepare()
        self.addCleanup(st.close)
        self.assertEqual(path.stat().st_size, 10)
        st.write_piece(2, b"ZZ")
        st.write_piece(0, b"abcd")
        self.assertEqual(path.read_bytes(), b"abcd\0\0\0\0ZZ")
        self.assertEqual(st.read_block(2, 0, 2), b"ZZ")

    def test_prepare_refuses_an_existing_file(self):
        root = self.dir / "root"
        os.mkdir(root)
        (root / "a").write_bytes(b"mine")
        st = storage.Storage(root, (spec(["a"], 3, 0), spec(["b"], 1, 3)), 4, 4, single=False)
        with self.assertRaises(DmError):
            st.prepare()
        self.assertEqual((root / "a").read_bytes(), b"mine")
        st.close()

    def test_prepare_refuses_an_existing_single_file(self):
        path = self.dir / "f"
        path.write_bytes(b"mine")
        st = storage.Storage(path, (spec(["f"], 4, 0),), 4, 4, single=True)
        with self.assertRaises(DmError):
            st.prepare()
        self.assertEqual(path.read_bytes(), b"mine")

    def test_prepare_does_not_follow_a_symlink(self):
        root = self.dir / "root"
        os.mkdir(root)
        target = self.dir / "victim"
        target.write_bytes(b"safe")
        os.symlink(target, root / "a")
        st = storage.Storage(root, (spec(["a"], 4, 0),), 4, 4, single=False)
        with self.assertRaises(DmError):
            st.prepare()
        self.assertEqual(target.read_bytes(), b"safe")

    def test_write_after_the_folder_is_deleted_fails_and_creates_nothing(self):
        st, root = self.multi()
        shutil.rmtree(root)
        with self.assertRaises(OSError):
            st.write_piece(0, b"0123")
        self.assertFalse(root.exists())
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), [])

    def test_close_is_idempotent_and_blocks_further_io(self):
        st, _ = self.multi()
        st.close()
        st.close()
        with self.assertRaises(OSError):
            st.write_piece(0, b"0123")
        with self.assertRaises(OSError):
            st.read_block(0, 0, 1)


class OpenExisting(unittest.TestCase):
    """AC9, NFR2: an existing partial is only reused when it is exactly the layout prepare() makes."""

    SPECS = (spec(["a"], 3, 0), spec(["empty"], 0, 3), spec(["sub", "deep", "b"], 6, 3), spec(["c"], 2, 9))

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.root = self.dir / "root"

    def make(self):
        os.mkdir(self.root)
        st = storage.Storage(self.root, self.SPECS, 4, 11, single=False)
        st.prepare()
        st.write_piece(0, b"0123")
        st.close()

    def reopen(self, specs=None, single=False, root=None):
        st = storage.Storage(root or self.root, specs or self.SPECS, 4, 11, single=single)
        self.addCleanup(st.close)
        st.open_existing()
        return st

    def test_reopens_a_prepared_folder_and_reads_what_was_written(self):
        self.make()
        st = self.reopen()
        self.assertEqual(st.read_block(0, 0, 4), b"0123")
        st.write_piece(1, b"4567")
        self.assertEqual(st.read_block(1, 0, 4), b"4567")

    def test_reopens_a_single_file(self):
        f = self.dir / "one"
        st = storage.Storage(f, (spec(["one"], 8, 0),), 4, 8, single=True)
        st.prepare()
        st.write_piece(0, b"abcd")
        st.close()
        again = self.reopen((spec(["one"], 8, 0),), single=True, root=f)
        self.assertEqual(again.read_block(0, 0, 4), b"abcd")

    def assertMismatch(self, **kw):
        with self.assertRaises(storage.LayoutMismatch):
            self.reopen(**kw)

    def test_missing_root(self):
        self.assertMismatch()

    def test_a_missing_file_is_not_written_yet_and_comes_back_empty(self):
        # an earlier run may have picked fewer files: what is absent is made again and re-checked
        self.make()
        os.unlink(self.root / "c")
        st = self.reopen()
        st.reconcile()
        self.assertEqual((self.root / "c").stat().st_size, 2)

    def test_a_missing_folder_is_not_written_yet_and_comes_back_empty(self):
        self.make()
        shutil.rmtree(self.root / "sub")
        st = self.reopen()
        st.reconcile()
        self.assertTrue((self.root / "sub").is_dir())

    def test_only_missing_or_wrong_kind_errors_mean_a_mismatch(self):
        import errno

        self.make()
        for code in (errno.ENOENT, errno.ELOOP, errno.ENOTDIR, errno.EISDIR, errno.ENXIO):
            with self.subTest(errno.errorcode[code]):
                with mock.patch.object(storage.os, "open", side_effect=OSError(code, "x")):
                    self.assertMismatch()

    def test_other_os_errors_propagate_so_the_data_is_kept(self):
        import errno

        self.make()
        for code in (errno.EMFILE, errno.EIO, errno.EACCES):
            with self.subTest(errno.errorcode[code]):
                with mock.patch.object(storage.os, "open", side_effect=OSError(code, "x")):
                    with self.assertRaises(OSError) as cm:
                        self.reopen()
                self.assertNotIsInstance(cm.exception, storage.LayoutMismatch)
                self.assertEqual(cm.exception.errno, code)

    def test_a_hard_linked_file_is_a_mismatch(self):
        self.make()
        os.link(self.root / "c", self.dir / "other-name")
        self.assertMismatch()

    def test_a_folder_swapped_for_a_symlink_after_opening_is_never_followed(self):
        self.make()
        st = self.reopen()
        outside = self.dir / "outside"
        outside.mkdir()
        shutil.rmtree(self.root / "sub")
        os.symlink(outside, self.root / "sub")
        with self.assertRaises(OSError):
            st.write_piece(1, b"4567")
        self.assertEqual(os.listdir(outside), [])

    def test_extra_file_at_the_top_and_in_a_subfolder(self):
        for where in (self.root / "extra", self.root / "sub" / "extra"):
            with self.subTest(where=where.name):
                self.make()
                where.write_bytes(b"x")
                self.assertMismatch()
                shutil.rmtree(self.root)

    def test_extra_empty_folder(self):
        self.make()
        os.mkdir(self.root / "more")
        self.assertMismatch()

    def test_wrong_size_file(self):
        self.make()
        with open(self.root / "a", "ab") as f:
            f.write(b"x")
        self.assertMismatch()

    def test_symlinked_file_is_refused_and_its_target_untouched(self):
        self.make()
        outside = self.dir / "outside"
        outside.write_bytes(b"123")
        os.unlink(self.root / "a")
        os.symlink(outside, self.root / "a")
        self.assertMismatch()
        self.assertEqual(outside.read_bytes(), b"123")

    def test_symlinked_folder_is_refused(self):
        self.make()
        elsewhere = self.dir / "elsewhere"
        shutil.move(self.root / "sub", elsewhere)
        os.symlink(elsewhere, self.root / "sub")
        self.assertMismatch()

    def test_symlinked_root_is_refused(self):
        self.make()
        moved = self.dir / "moved"
        os.rename(self.root, moved)
        os.symlink(moved, self.root)
        self.assertMismatch()

    def test_directory_or_fifo_in_place_of_a_file(self):
        self.make()
        os.unlink(self.root / "c")
        os.mkdir(self.root / "c")
        self.assertMismatch()
        os.rmdir(self.root / "c")
        os.mkfifo(self.root / "c")
        self.assertMismatch()

    def test_single_file_that_is_a_symlink_or_wrong_size(self):
        f = self.dir / "one"
        outside = self.dir / "outside"
        outside.write_bytes(b"12345678")
        os.symlink(outside, f)
        self.assertMismatch(specs=(spec(["one"], 8, 0),), single=True, root=f)
        os.unlink(f)
        f.write_bytes(b"123")
        self.assertMismatch(specs=(spec(["one"], 8, 0),), single=True, root=f)
        self.assertEqual(outside.read_bytes(), b"12345678")


class Place(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.folder = Path(self._tmp.name)

    def tmpfile(self, data=b"new"):
        p = self.folder / ".t.dm-part"
        p.write_bytes(data)
        return p

    def tmpdir(self):
        d = self.folder / ".t.dm-part-dir"
        os.mkdir(d)
        (d / "f").write_bytes(b"new")
        return d

    def test_fetch_keeps_the_old_name_as_an_alias(self):
        self.assertIs(fetch._place, place.place_file)

    def test_place_file_uses_the_first_free_name(self):
        (self.folder / "a.txt").write_bytes(b"old")
        final = place.place_file(self.tmpfile(), self.folder, "a.txt")
        self.assertEqual(final, self.folder / "a (1).txt")
        self.assertEqual(final.read_bytes(), b"new")
        self.assertEqual((self.folder / "a.txt").read_bytes(), b"old")
        self.assertFalse((self.folder / ".t.dm-part").exists())

    def test_place_dir_free_name(self):
        final = place.place_dir(self.tmpdir(), self.folder, "album")
        self.assertEqual(final, self.folder / "album")
        self.assertEqual((final / "f").read_bytes(), b"new")
        self.assertFalse((self.folder / ".t.dm-part-dir").exists())

    def test_place_dir_skips_an_existing_folder(self):
        os.mkdir(self.folder / "album")
        (self.folder / "album" / "mine").write_bytes(b"old")
        final = place.place_dir(self.tmpdir(), self.folder, "album")
        self.assertEqual(final, self.folder / "album (1)")
        self.assertEqual((self.folder / "album" / "mine").read_bytes(), b"old")
        self.assertEqual(os.listdir(self.folder / "album"), ["mine"])

    def test_place_dir_skips_an_existing_file(self):
        (self.folder / "album").write_bytes(b"old")
        final = place.place_dir(self.tmpdir(), self.folder, "album")
        self.assertEqual(final, self.folder / "album (1)")
        self.assertEqual((self.folder / "album").read_bytes(), b"old")

    def test_place_dir_skips_a_symlink(self):
        target = self.folder / "elsewhere"
        os.mkdir(target)
        os.symlink(target, self.folder / "album")
        os.symlink(self.folder / "missing", self.folder / "album (1)")  # dangling
        final = place.place_dir(self.tmpdir(), self.folder, "album")
        self.assertEqual(final, self.folder / "album (2)")
        self.assertEqual(os.listdir(target), [])

    def test_place_dir_leaves_a_placeholder_someone_filled(self):
        real = os.rename
        calls = []

        def racing(src, dst, *a, **kw):
            if not calls:
                calls.append(dst)
                (Path(dst) / "theirs").write_bytes(b"theirs")
            return real(src, dst, *a, **kw)

        with mock.patch.object(place.os, "rename", racing):
            final = place.place_dir(self.tmpdir(), self.folder, "album")
        self.assertEqual(final, self.folder / "album (1)")
        self.assertEqual((self.folder / "album" / "theirs").read_bytes(), b"theirs")
        self.assertEqual(os.listdir(self.folder / "album"), ["theirs"])
        self.assertEqual((final / "f").read_bytes(), b"new")

    def test_place_dir_none_free(self):
        with mock.patch.object(place, "candidates", lambda base, is_dir=False: iter([base, base + " (1)"])):
            os.mkdir(self.folder / "album")
            os.mkdir(self.folder / "album (1)")
            with self.assertRaises(DmError) as cm:
                place.place_dir(self.tmpdir(), self.folder, "album")
        self.assertEqual(str(cm.exception), "no free name for 'album'")
        self.assertTrue((self.folder / ".t.dm-part-dir" / "f").exists())

    def test_place_dir_other_errors_remove_our_placeholder_and_raise(self):
        with mock.patch.object(place.os, "rename", side_effect=OSError(errno.EIO, "io")):
            with self.assertRaises(OSError):
                place.place_dir(self.tmpdir(), self.folder, "album")
        self.assertFalse((self.folder / "album").exists())
        self.assertTrue((self.folder / ".t.dm-part-dir" / "f").exists())


class Hardening(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def prepared(self, specs, total, piece_length=4):
        root = self.dir / "root"
        os.mkdir(root)
        st = storage.Storage(root, specs, piece_length, total, single=False)
        st.prepare()
        self.addCleanup(st.close)
        return st, root

    # 1
    def test_huge_integer_is_a_bencode_error(self):
        with self.assertRaises(bencode.BencodeError):
            bencode.decode(b"i" + b"1" * 5000 + b"e")
        with self.assertRaises(bencode.BencodeError):
            bencode.decode(b"i-" + b"1" * 21 + b"e")
        self.assertEqual(bencode.decode(b"i" + b"1" * 20 + b"e")[0], int("1" * 20))

    def test_huge_integer_in_a_torrent_is_a_torrent_error(self):
        with self.assertRaises(metainfo.TorrentError):
            metainfo.parse(b"d4:infod6:lengthi" + b"1" * 5000 + b"eee")

    # 2
    def test_too_many_files(self):
        files = [{"length": 1, "path": [b"f%d" % i]} for i in range(4)]
        with mock.patch.object(metainfo, "MAX_FILES", 3):
            with self.assertRaises(metainfo.TorrentError) as cm:
                metainfo.parse(make_torrent(length=None, files=files))
        self.assertIn("too many files", str(cm.exception))

    def test_many_files_beyond_the_fd_cache_work(self):
        n = storage.MAX_OPEN_FILES * 3
        specs = tuple(spec(["d%d" % (i % 5), "f%d" % i], 2, i * 2) for i in range(n))
        st, root = self.prepared(specs, n * 2, piece_length=2 * n)
        data = bytes(range(256)) * (2 * n // 256 + 1)
        data = data[:2 * n]
        st.write_piece(0, data)
        self.assertEqual(st.read_block(0, 0, 2 * n), data)
        self.assertEqual((root / "d3" / "f3").read_bytes(), data[6:8])
        self.assertLessEqual(len(st._cache), storage.MAX_OPEN_FILES)

    def test_prepare_works_with_a_low_fd_limit(self):
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        self.addCleanup(resource.setrlimit, resource.RLIMIT_NOFILE, (soft, hard))
        resource.setrlimit(resource.RLIMIT_NOFILE, (min(soft, 128), hard))
        specs = tuple(spec(["f%d" % i], 1, i) for i in range(300))
        st, root = self.prepared(specs, 300, piece_length=300)
        st.write_piece(0, b"x" * 300)
        self.assertEqual(len(os.listdir(root)), 300)

    def test_deleted_file_is_detected_on_a_cached_fd(self):
        st, root = self.prepared((spec(["a"], 4, 0),), 4)
        st.write_piece(0, b"abcd")
        os.unlink(root / "a")
        with self.assertRaises(FileNotFoundError):
            st.write_piece(0, b"abcd")

    # 3
    def test_directory_candidates_do_not_split_a_fake_extension(self):
        got = list(names.candidates("Release v1.2", is_dir=True))[:3]
        self.assertEqual(got, ["Release v1.2", "Release v1.2 (1)", "Release v1.2 (2)"])
        self.assertEqual(list(names.candidates("a.txt"))[1], "a (1).txt")
        long = "x" * 255
        for c in list(names.candidates(long, is_dir=True))[:12]:
            self.assertLessEqual(len(c.encode()), 255)

    def test_place_dir_numbers_a_dotted_name(self):
        os.mkdir(self.dir / "Release v1.2")
        d = self.dir / ".t"
        os.mkdir(d)
        final = place.place_dir(d, self.dir, "Release v1.2")
        self.assertEqual(final, self.dir / "Release v1.2 (1)")

    # 4
    def test_node_budget(self):
        data = b"l" + b"i1e" * 10 + b"e"
        self.assertEqual(len(bencode.decode(data, max_nodes=11)[0]), 10)
        with self.assertRaises(bencode.BencodeError):
            bencode.decode(data, max_nodes=10)
        with self.assertRaises(bencode.BencodeError):
            bencode.decode(b"d" + b"".join(b"1:%ci1e" % c for c in b"abcdef") + b"e", max_nodes=8)

    # 5
    def test_close_from_another_thread_is_safe(self):
        st, _ = self.prepared((spec(["a"], 4, 0),), 4)
        st.write_piece(0, b"abcd")
        t = threading.Thread(target=st.close)
        t.start()
        t.join()
        with self.assertRaises((OSError, ValueError)):
            st.write_piece(0, b"abcd")
        with self.assertRaises((OSError, ValueError)):
            st.read_block(0, 0, 4)

    def test_close_waits_for_io_in_progress(self):
        st, _ = self.prepared((spec(["a"], 4, 0),), 4)
        entered, release = threading.Event(), threading.Event()
        real = os.pwrite

        def slow(*a):
            entered.set()
            release.wait(5)
            return real(*a)

        errors = []

        def writer():
            try:
                st.write_piece(0, b"abcd")
            except Exception as e:
                errors.append(e)

        with mock.patch.object(storage.os, "pwrite", slow):
            w = threading.Thread(target=writer)
            w.start()
            entered.wait(5)
            c = threading.Thread(target=st.close)
            c.start()
            c.join(0.2)
            self.assertTrue(c.is_alive())  # close is blocked while the write runs
            release.set()
            w.join()
            c.join()
        self.assertEqual(errors, [])

    # 6
    def test_swapped_intermediate_folder_cannot_redirect_writes(self):
        st, root = self.prepared((spec(["sub", "b"], 4, 0),), 4)
        outside = self.dir / "outside"
        os.mkdir(outside)
        os.rename(root / "sub", self.dir / "moved")
        os.symlink(outside, root / "sub")
        with self.assertRaises(OSError):
            st.write_piece(0, b"abcd")
        self.assertEqual(os.listdir(outside), [])

    def test_prepare_refuses_a_symlinked_root(self):
        real = self.dir / "real"
        os.mkdir(real)
        link = self.dir / "link"
        os.symlink(real, link)
        st = storage.Storage(link, (spec(["a"], 1, 0),), 4, 1, single=False)
        with self.assertRaises(DmError):
            st.prepare()
        self.assertEqual(os.listdir(real), [])

    # 7
    def test_case_and_unicode_clashes_are_conflicts(self):
        cases = {
            "case": [["A.txt"], ["a.txt"]],
            "nfc/nfd": [["\u00e9"], ["e\u0301"]],
            "dir vs file case": [["A"], ["a", "x"]],
            "dir case": [["D", "x"], ["d", "x"]],
        }
        for label, parts in cases.items():
            with self.subTest(label):
                with self.assertRaises(metainfo.TorrentError) as cm:
                    layout.plan_files(meta_of(parts=parts))
                self.assertEqual(str(cm.exception), "invalid torrent: conflicting paths")

    # 8
    def test_place_dir_interrupt_removes_the_placeholder(self):
        d = self.dir / ".t"
        os.mkdir(d)
        with mock.patch.object(place.os, "rename", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                place.place_dir(d, self.dir, "album")
        self.assertFalse((self.dir / "album").exists())
        self.assertTrue(d.exists())

    # 9
    def test_prepare_errors_do_not_leak_paths(self):
        root = self.dir / "secretroot"
        os.mkdir(root)
        (root / "evil-name").write_bytes(b"x")
        st = storage.Storage(root, (spec(["evil-name"], 1, 0),), 4, 1, single=False)
        with self.assertRaises(DmError) as cm:
            st.prepare()
        msg = str(cm.exception)
        self.assertTrue(msg.startswith("cannot create files in secretroot: "), msg)
        self.assertNotIn(str(self.dir), msg)
        self.assertNotIn("evil-name", msg)

    # 10
    def test_leading_dots_are_stripped_on_purpose(self):
        specs = layout.plan_files(meta_of(parts=[[".hidden"], ["..."]]))
        self.assertEqual([s.parts for s in specs], [("hidden",), ("download",)])
