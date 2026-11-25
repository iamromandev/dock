"""dm 0.8.0 file selection: Storage keeps the bytes of unpicked files inside a needed piece in a hidden side
area."""

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "plugin" / "dm"))

from dmcore.torrent import storage  # noqa: E402
from dmcore.torrent.layout import FileSpec  # noqa: E402


def tree(root):
    """Every entry under root as a sorted list of relative paths ("/" ends a folder)."""
    out = []
    for dirpath, dirs, files in os.walk(root):
        rel = Path(dirpath).relative_to(root)
        out += [f"{(rel / d).as_posix()}/" for d in dirs] + [(rel / f).as_posix() for f in files]
    return sorted(out)


class Side(unittest.TestCase):
    """a:3 @0, sub/b:6 @3, deep/er/c:2 @9, z:0 @11; pieces of 4 -> 4, 4, 3."""

    SPECS = (FileSpec(("a",), 3, 0), FileSpec(("sub", "b"), 6, 3), FileSpec(("deep", "er", "c"), 2, 9),
             FileSpec(("z",), 0, 11))

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

    def test_prepare_creates_only_the_picked_files_and_their_folders(self):
        self.storage({0, 2})
        self.assertEqual(tree(self.root), ["a", "deep/", "deep/er/", "deep/er/c"])

    def test_a_picked_empty_file_is_created_an_unpicked_one_is_not(self):
        self.storage({3})
        self.assertEqual(tree(self.root), ["z"])
        self.assertEqual((self.root / "z").stat().st_size, 0)

    def test_nothing_picked_out_means_the_full_layout(self):
        self.storage(None)
        self.assertEqual(tree(self.root), ["a", "deep/", "deep/er/", "deep/er/c", "sub/", "sub/b", "z"])

    def test_a_boundary_piece_lands_in_the_picked_file_and_the_side_area(self):
        st = self.storage({0})
        st.write_piece(0, b"ABCD")  # a(3) + the first byte of sub/b
        self.assertEqual((self.root / "a").read_bytes(), b"ABC")
        side = self.root / ".dm-side" / "sub" / "b"
        self.assertEqual(side.stat().st_size, 6)
        self.assertEqual(side.read_bytes(), b"D\0\0\0\0\0")
        self.assertEqual(tree(self.root), [".dm-side/", ".dm-side/sub/", ".dm-side/sub/b", "a"])
        self.assertFalse((self.root / "sub").exists())

    def test_read_block_returns_the_bytes_of_picked_and_side_files_alike(self):
        st = self.storage({0})
        st.write_piece(0, b"ABCD")
        self.assertEqual(st.read_block(0, 0, 4), b"ABCD")
        self.assertEqual(st.read_block(0, 2, 2), b"CD")

    def test_reading_an_unpicked_file_never_written_fails_without_creating_it(self):
        st = self.storage({0})
        with self.assertRaises(FileNotFoundError):
            st.read_block(1, 0, 4)
        self.assertEqual(tree(self.root), ["a"])

    def test_a_piece_over_two_unpicked_files_and_a_picked_one(self):
        st = self.storage({2})
        st.write_piece(2, b"XYZ")  # sub/b's last 1 byte, deep/er/c's 2 bytes
        self.assertEqual((self.root / "deep" / "er" / "c").read_bytes(), b"YZ")
        self.assertEqual((self.root / ".dm-side" / "sub" / "b").read_bytes(), b"\0\0\0\0\0X")
        self.assertEqual(st.read_block(2, 0, 3), b"XYZ")

    def test_a_side_file_that_vanished_after_being_written_is_not_recreated_as_zeros(self):
        st = self.storage({0})
        with mock.patch.object(storage, "MAX_OPEN_FILES", 1):
            st.write_piece(0, b"ABCD")  # a + the side file of sub/b
            st.read_block(0, 0, 3)  # evicts the descriptor of the side file
        os.unlink(self.root / ".dm-side" / "sub" / "b")
        with self.assertRaises(FileNotFoundError):
            st.write_piece(1, b"EFGH")
        self.assertFalse((self.root / ".dm-side" / "sub" / "b").exists())

    def test_finish_layout_leaves_only_picked_files_and_their_folders(self):
        st = self.storage({1})
        st.write_piece(0, b"ABCD")
        st.write_piece(1, b"EFGH")
        st.write_piece(2, b"IJK")
        self.assertTrue((self.root / ".dm-side" / "a").exists())
        st.finish_layout()
        self.assertEqual(tree(self.root), ["sub/", "sub/b"])
        self.assertEqual((self.root / "sub" / "b").read_bytes(), b"DEFGHI")

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

    def test_finish_layout_without_a_side_area_is_fine(self):
        st = self.storage(None)
        st.finish_layout()
        self.assertEqual(len(tree(self.root)), 7)

    def test_a_symlink_at_the_side_folder_is_not_written_through(self):
        outside = self.dir / "outside"
        os.mkdir(outside)
        os.symlink(outside, self.root / ".dm-side")
        st = self.storage({0})
        with self.assertRaises(OSError):
            st.write_piece(0, b"ABCD")
        self.assertEqual(os.listdir(outside), [])

    def test_a_symlink_at_a_side_subfolder_is_not_written_through(self):
        outside = self.dir / "outside"
        os.mkdir(outside)
        os.mkdir(self.root / ".dm-side")
        os.symlink(outside, self.root / ".dm-side" / "sub")
        st = self.storage({0})
        with self.assertRaises(OSError):
            st.write_piece(0, b"ABCD")
        self.assertEqual(os.listdir(outside), [])

    def test_a_symlink_at_the_side_file_is_not_written_through(self):
        target = self.dir / "target"
        target.write_bytes(b"keep")
        os.makedirs(self.root / ".dm-side" / "sub")
        os.symlink(target, self.root / ".dm-side" / "sub" / "b")
        st = self.storage({0})
        with self.assertRaises(OSError):
            st.write_piece(0, b"ABCD")
        self.assertEqual(target.read_bytes(), b"keep")

    def test_finish_layout_removes_a_symlinked_side_area_without_following_it(self):
        outside = self.dir / "outside"
        os.mkdir(outside)
        (outside / "precious").write_bytes(b"x")
        os.symlink(outside, self.root / ".dm-side")
        st = self.storage({0})
        st.finish_layout()
        self.assertEqual(tree(self.root), ["a"])
        self.assertEqual(os.listdir(outside), ["precious"])

    def test_finish_layout_unlinks_a_symlink_where_a_pruned_folder_should_be(self):
        outside = self.dir / "outside"
        os.mkdir(outside)
        (outside / "precious").write_bytes(b"x")
        st = self.storage({0})
        os.symlink(outside, self.root / "deep")
        os.mkdir(self.root / "sub")
        os.symlink(outside, self.root / "sub" / "b")  # a link at the path of an unpicked file
        st.finish_layout()
        self.assertEqual(tree(self.root), ["a"])
        self.assertEqual(os.listdir(outside), ["precious"])

    def test_finish_layout_unlinks_a_symlink_below_a_pruned_folder(self):
        outside = self.dir / "outside"
        os.mkdir(outside)
        (outside / "precious").write_bytes(b"x")
        st = self.storage({0})
        os.makedirs(self.root / "deep")
        os.symlink(outside, self.root / "deep" / "er")
        st.finish_layout()
        self.assertEqual(tree(self.root), ["a"])
        self.assertEqual(os.listdir(outside), ["precious"])

    def test_finish_layout_keeps_the_folders_of_picked_files_and_drops_the_rest_bottom_up(self):
        st = self.storage({2})
        os.makedirs(self.root / "sub" / "x" / "y")
        st.finish_layout()
        self.assertEqual(tree(self.root), ["deep/", "deep/er/", "deep/er/c"])

    def test_finish_layout_of_a_hostile_torrent_is_fast(self):
        import time

        specs = [FileSpec(("keep", "f"), 1, 0)]
        for i in range(100_000):  # unpicked, 32 deep, folders that were never created
            parts = (f"g{i % 1000}",) + tuple(f"s{k}" for k in range(30)) + (f"f{i}",)
            specs.append(FileSpec(parts, 1, i + 1))
        st = storage.Storage(self.root, specs, 4, len(specs), single=False, wanted={0})
        st.prepare()
        self.addCleanup(st.close)
        os.makedirs(self.root / "g7" / "s0" / "s1")  # a few that were
        started = time.monotonic()
        st.finish_layout()
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(tree(self.root), ["keep/", "keep/f"])

    def reopen(self, wanted, reconcile=True):
        st = storage.Storage(self.root, self.SPECS, 4, 11, single=False, wanted=wanted)
        self.addCleanup(st.close)
        st.open_existing()
        if reconcile:
            st.reconcile()
        return st

    def test_a_selected_partial_opens_with_the_same_selection(self):
        st = self.storage({0})
        st.write_piece(0, b"ABCD")
        st.close()
        again = self.reopen({0})
        self.assertEqual(again.read_block(0, 0, 4), b"ABCD")
        self.assertEqual(tree(self.root), [".dm-side/", ".dm-side/sub/", ".dm-side/sub/b", "a"])

    def test_a_selected_partial_opens_with_a_different_selection_moving_files_both_ways(self):
        st = self.storage({0})
        st.write_piece(0, b"ABCD")
        st.close()
        again = self.reopen({1})
        self.assertEqual(tree(self.root), [".dm-side/", ".dm-side/a", ".dm-side/sub/", "sub/", "sub/b"])
        self.assertEqual(again.read_block(0, 0, 4), b"ABCD")
        self.assertEqual((self.root / "sub" / "b").read_bytes()[:1], b"D")
        self.assertEqual((self.root / ".dm-side" / "a").read_bytes(), b"ABC")

    def test_a_selected_partial_opens_with_no_selection_and_makes_every_file(self):
        st = self.storage({0})
        st.write_piece(0, b"ABCD")
        st.close()
        self.reopen(None)
        # the emptied side folders stay until finish_layout() removes the side area
        self.assertEqual(tree(self.root), [".dm-side/", ".dm-side/sub/", "a", "deep/", "deep/er/",
                                           "deep/er/c", "sub/", "sub/b", "z"])
        self.assertEqual((self.root / "a").read_bytes(), b"ABC")
        self.assertEqual((self.root / "sub" / "b").read_bytes()[:1], b"D")

    def test_a_full_layout_partial_with_a_selection_moves_unpicked_files_into_the_side_area(self):
        st = self.storage(None)
        st.write_piece(0, b"ABCD")
        st.close()
        again = self.reopen({0})
        self.assertEqual(tree(self.root), [".dm-side/", ".dm-side/deep/", ".dm-side/deep/er/",
                                           ".dm-side/deep/er/c", ".dm-side/sub/", ".dm-side/sub/b",
                                           ".dm-side/z", "a", "deep/", "deep/er/", "sub/"])
        self.assertEqual(again.read_block(0, 0, 4), b"ABCD")

    def test_a_partial_whose_unpicked_files_are_gone_opens_and_reads_them_as_absent(self):
        st = self.storage({0})
        st.write_piece(0, b"ABCD")
        st.close()
        shutil.rmtree(self.root / ".dm-side")  # a cancel after finish_layout looks like this
        again = self.reopen({0})
        self.assertEqual(tree(self.root), ["a"])
        with self.assertRaises(FileNotFoundError):
            again.read_block(0, 0, 4)
        self.assertEqual(again.read_block(0, 0, 3), b"ABC")

    def test_a_picked_file_that_is_nowhere_is_created_sparse(self):
        self.storage({0}).close()
        self.reopen({0, 2})
        self.assertEqual((self.root / "deep" / "er" / "c").stat().st_size, 2)

    def test_a_side_file_found_on_disk_that_vanishes_later_is_not_recreated_as_zeros(self):
        st = self.storage({0})
        st.write_piece(0, b"ABCD")
        st.close()
        again = self.reopen({0})
        again.close()
        again = self.reopen({0})
        os.unlink(self.root / ".dm-side" / "sub" / "b")
        with self.assertRaises(FileNotFoundError):
            again.write_piece(1, b"EFGH")

    def test_a_file_in_both_places_is_a_mismatch(self):
        st = self.storage({0})
        st.write_piece(0, b"ABCD")
        st.close()
        (self.root / "sub").mkdir()
        (self.root / "sub" / "b").write_bytes(b"x" * 6)
        with self.assertRaises(storage.LayoutMismatch):
            self.reopen({0})

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

    def test_a_symlink_at_a_real_path_is_a_mismatch(self):
        def build(o):
            os.unlink(self.root / "a")
            os.symlink(o / "keep", self.root / "a")
        self.hostile(build)

    def test_a_symlink_at_the_side_folder_is_a_mismatch(self):
        def build(o):
            shutil.rmtree(self.root / ".dm-side")
            os.symlink(o, self.root / ".dm-side")
        self.hostile(build)

    def test_a_symlink_at_a_side_subfolder_is_a_mismatch(self):
        def build(o):
            shutil.rmtree(self.root / ".dm-side" / "sub")
            os.symlink(o, self.root / ".dm-side" / "sub")
        self.hostile(build)

    def test_an_extra_entry_inside_the_side_area_is_a_mismatch(self):
        self.hostile(lambda o: (self.root / ".dm-side" / "stray").write_bytes(b"x"))

    def test_a_fifo_where_a_file_goes_is_a_mismatch(self):
        def build(o):
            os.unlink(self.root / "a")
            os.mkfifo(self.root / "a")
        self.hostile(build)

    def test_a_hard_linked_file_is_a_mismatch(self):
        def build(o):
            os.unlink(self.root / ".dm-side" / "sub" / "b")
            os.link(o / "keep", self.root / ".dm-side" / "sub" / "b")
        self.hostile(build)

    def test_a_resized_file_is_a_mismatch(self):
        self.hostile(lambda o: (self.root / "a").write_bytes(b"too long for it"))

    def test_a_symlink_to_a_file_in_the_side_area_is_a_mismatch(self):
        def build(o):
            os.unlink(self.root / ".dm-side" / "sub" / "b")
            os.symlink(o / "keep", self.root / ".dm-side" / "sub" / "b")
        self.hostile(build)

    def test_a_full_layout_partial_still_opens_without_a_selection(self):
        self.storage(None).close()
        again = storage.Storage(self.root, self.SPECS, 4, 11, single=False)
        again.open_existing()
        again.close()

    def test_picking_every_file_is_the_same_as_no_selection(self):
        self.storage({0, 1, 2, 3}).close()
        again = storage.Storage(self.root, self.SPECS, 4, 11, single=False, wanted={0, 1, 2, 3})
        again.open_existing()
        again.close()


if __name__ == "__main__":
    unittest.main()
