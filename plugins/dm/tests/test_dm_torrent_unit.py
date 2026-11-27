"""Unit tests for the torrent support in the dm plugin (plugins/dm/dmcore/torrent)."""

import errno
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dmcore.target import DmError
from dmcore.torrent import bencode, layout, metainfo, place, storage


class Bencode(unittest.TestCase):
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

    def test_wrong_types(self):
        self.invalid(make_torrent(name=5))
        self.invalid(make_torrent(length=b"x"))
        self.invalid(make_torrent(length=-1))
        self.invalid(make_torrent(length=None, files=[{"length": 1}]))
        self.invalid(make_torrent(length=None, files=[{"length": 1, "path": []}]))
        self.invalid(make_torrent(length=None, files=[{"length": 1, "path": [5]}]))
        self.invalid(make_torrent(length=None, files=[{"length": -1, "path": [b"a"]}]))
        self.invalid(make_torrent(length=None, files=[5]))


def meta_of(name="top", parts=None, lengths=None, single=False):
    """A Meta built directly (no bencode), for layout and storage tests."""
    if single:
        files = (metainfo.FileEntry((name,), 10),)
    else:
        lengths = lengths or [1] * len(parts)
        files = tuple(metainfo.FileEntry(tuple(p), n) for p, n in zip(parts, lengths))
    total = sum(f.length for f in files)
    return metainfo.Meta(
        info_hash=b"\0" * 20,
        name=name,
        piece_length=PL,
        hashes=(b"\0" * 20,),
        total=total,
        files=files,
        multi=not single,
        trackers=(),
    )


def spec(parts, length, offset):
    return layout.FileSpec(tuple(parts), length, offset)


class Storage(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def multi(self, piece_length=4):
        # a:3 bytes, empty:0, sub/b:6 (nested), c:2 -> total 11, pieces 4,4,3
        specs = (
            spec(["a"], 3, 0),
            spec(["empty"], 0, 3),
            spec(["sub", "deep", "b"], 6, 3),
            spec(["c"], 2, 9),
        )
        root = self.dir / "root"
        os.mkdir(root)
        st = storage.Storage(root, specs, piece_length, 11, single=False)
        st.prepare()
        self.addCleanup(st.close)
        return st, root

    def test_prepare_refuses_an_existing_single_file(self):
        path = self.dir / "f"
        path.write_bytes(b"mine")
        st = storage.Storage(path, (spec(["f"], 4, 0),), 4, 4, single=True)
        with self.assertRaises(DmError):
            st.prepare()
        self.assertEqual(path.read_bytes(), b"mine")


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

    def assertMismatch(self, **kw):
        with self.assertRaises(storage.LayoutMismatch):
            self.reopen(**kw)

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

    # 2

    # 3

    # 4

    # 5

    # 6

    # 7

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

    # 10
