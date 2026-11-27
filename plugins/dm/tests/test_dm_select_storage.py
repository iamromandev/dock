"""dm 0.8.0 file selection: Storage keeps the bytes of unpicked files inside a needed piece in a hidden side
area."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dmcore.torrent import storage
from dmcore.torrent.layout import FileSpec


def tree(root):
    """Every entry under root as a sorted list of relative paths ("/" ends a folder)."""
    out = []
    for dirpath, dirs, files in os.walk(root):
        rel = Path(dirpath).relative_to(root)
        out += [f"{(rel / d).as_posix()}/" for d in dirs] + [(rel / f).as_posix() for f in files]
    return sorted(out)


class Side(unittest.TestCase):
    """a:3 @0, sub/b:6 @3, deep/er/c:2 @9, z:0 @11; pieces of 4 -> 4, 4, 3."""

    SPECS = (
        FileSpec(("a",), 3, 0),
        FileSpec(("sub", "b"), 6, 3),
        FileSpec(("deep", "er", "c"), 2, 9),
        FileSpec(("z",), 0, 11),
    )

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.root = self.dir / "root"
        os.mkdir(self.root)

    def storage(self, wanted):
        st = storage.Storage(self.root, self.SPECS, 4, 11, single=False, wanted=wanted)
        st.prepare()
        self.addCleanup(st.close)
        return st

    def test_finish_layout_removes_empty_folders_that_hold_no_picked_file(self):
        st = self.storage({0})
        os.makedirs(self.root / "deep" / "er")  # as a wider earlier selection would leave them
        os.makedirs(self.root / "sub")
        (self.root / "sub" / "keep").write_bytes(b"x")
        st.finish_layout()
        self.assertEqual(tree(self.root), ["a", "sub/", "sub/keep"])  # only empty folders go
        os.unlink(self.root / "sub" / "keep")
        st.finish_layout()
        self.assertEqual(tree(self.root), ["a"])

    def test_finish_layout_removes_a_symlinked_side_area_without_following_it(self):
        outside = self.dir / "outside"
        os.mkdir(outside)
        (outside / "precious").write_bytes(b"x")
        os.symlink(outside, self.root / ".dm-side")
        st = self.storage({0})
        st.finish_layout()
        self.assertEqual(tree(self.root), ["a"])
        self.assertEqual(os.listdir(outside), ["precious"])

    def reopen(self, wanted, reconcile=True):
        st = storage.Storage(self.root, self.SPECS, 4, 11, single=False, wanted=wanted)
        self.addCleanup(st.close)
        st.open_existing()
        if reconcile:
            st.reconcile()
        return st

    def hostile(self, build, wanted=frozenset({0})):
        st = self.storage(wanted)
        st.write_piece(0, b"ABCD")
        st.close()
        outside = self.dir / "outside"
        outside.mkdir()
        (outside / "keep").write_bytes(b"secret")
        build(outside)
        before = tree(self.dir)
        with self.assertRaises(storage.LayoutMismatch):
            self.reopen(wanted)
        self.assertEqual(tree(self.dir), before)
        self.assertEqual((outside / "keep").read_bytes(), b"secret")


if __name__ == "__main__":
    unittest.main()
