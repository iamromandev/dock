"""dm 0.8.0 file selection: parsing picks, resolving them against a torrent's files, listing, pieces, the
picker."""

import random
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "plugins" / "dm"))

from dmcore.target import DmError
from dmcore.torrent import layout, picker, select
from dmcore.torrent.layout import FileSpec

PL = 16384


def specs_of(*files):
    """FileSpecs for (path, length) pairs, path as "a/b"."""
    out, offset = [], 0
    for path, n in files:
        out.append(FileSpec(tuple(path.split("/")), n, offset))
        offset += n
    return tuple(out)


SHOW = specs_of(("S01E01.mkv", 1200), ("S01E02.mkv", 1100), ("S01E01.srt", 12), ("extras/poster.png", 34),
                ("notes.txt", 5))


def picked(only=None, skip=None, specs=SHOW):
    return sorted(select.resolve(specs, select.parse(only, skip)))


def cols(line):
    return [c.strip() for c in line.strip().split("  ") if c.strip()]


def message(fn, *args):
    with unittest.TestCase().assertRaises(DmError) as cm:
        fn(*args)
    return str(cm.exception)


class Parse(unittest.TestCase):
    def test_no_flag_is_no_picks(self):
        self.assertIsNone(select.parse(None, None))

    def test_items_are_split_on_commas_and_stripped(self):
        picks = select.parse(" 1 , 3-4 ,*.srt", None)
        self.assertEqual([type(p).__name__ for p in picks.only], ["Number", "Number", "Glob"])
        self.assertEqual((picks.only[1].lo, picks.only[1].hi), (3, 4))
        self.assertEqual(picks.skip, ())

    def test_bad_picks_are_named(self):
        for text, bad in ((",", ""), ("1,,2", ""), ("3-1", "3-1"), ("0", "0"), ("0-2", "0-2"), ("2-", "2-"),
                          ("-3", "-3"), (" , ", "")):
            with self.subTest(text=text):
                self.assertEqual(message(select.parse, text, None), f"bad pick '{bad}'")
        self.assertEqual(message(select.parse, None, "4-2"), "bad pick '4-2'")

    def test_a_huge_number_is_a_bad_pick_not_a_crash(self):
        for text in ("9" * 5000, "1-" + "9" * 5000, "9" * 5000 + "-9999999999"):
            with self.subTest(text=text[:12]):
                self.assertEqual(message(select.parse, text, None), f"bad pick '{text}'")

    def test_a_dash_between_names_is_a_glob(self):
        picks = select.parse("a-b", None)
        self.assertEqual(type(picks.only[0]).__name__, "Glob")


class Resolve(unittest.TestCase):
    def test_no_picks_is_every_file(self):
        self.assertEqual(sorted(select.resolve(SHOW, None)), [0, 1, 2, 3, 4])

    def test_numbers_and_ranges_are_one_based_and_inclusive(self):
        self.assertEqual(picked("1"), [0])
        self.assertEqual(picked("2-4"), [1, 2, 3])
        self.assertEqual(picked("5,1"), [0, 4])

    def test_globs_match_the_whole_path_inside_the_item_across_slashes(self):
        self.assertEqual(picked("*E01*"), [0, 2])
        self.assertEqual(picked("extras/*"), [3])
        self.assertEqual(picked("*"), [0, 1, 2, 3, 4])
        self.assertEqual(picked("S01E0?.mkv"), [0, 1])
        self.assertEqual(picked("S01E0[12].srt"), [2])
        self.assertEqual(message(select.resolve, SHOW, select.parse("Show/*", None)),
                         "no file matches 'Show/*'")

    def test_globs_are_case_sensitive(self):
        self.assertEqual(message(select.resolve, SHOW, select.parse("*.MKV", None)),
                         "no file matches '*.MKV'")

    def test_a_literal_comma_goes_through_a_bracket(self):
        specs = specs_of(("a,b.txt", 1), ("c.txt", 1))
        self.assertEqual(picked("a[,]b.txt", specs=specs), [0])

    def test_picks_are_a_union(self):
        self.assertEqual(picked("1,*E01*,1-2"), [0, 1, 2])

    def test_skip_removes_from_only_or_from_all(self):
        self.assertEqual(picked("1-3", "2"), [0, 2])
        self.assertEqual(picked(None, "*.txt"), [0, 1, 2, 3])
        self.assertEqual(picked(None, "*.mkv,5"), [2, 3])

    def test_a_single_file_torrent_is_named_by_its_cleaned_name(self):
        one = specs_of(("movie.mkv", 10))
        self.assertEqual(sorted(select.resolve(one, select.parse("1", None))), [0])
        self.assertEqual(sorted(select.resolve(one, select.parse("movie.*", None))), [0])
        self.assertEqual(message(select.resolve, one, select.parse("2", None)),
                         "file number 2 is out of range (1-1)")

    def test_out_of_range_numbers_are_named(self):
        self.assertEqual(message(select.resolve, SHOW, select.parse("9", None)),
                         "file number 9 is out of range (1-5)")
        self.assertEqual(message(select.resolve, SHOW, select.parse("4-9", None)),
                         "file number 9 is out of range (1-5)")
        self.assertEqual(message(select.resolve, SHOW, select.parse(None, "7")),
                         "file number 7 is out of range (1-5)")

    def test_a_pick_that_matches_nothing_is_named_in_only_and_in_skip(self):
        self.assertEqual(message(select.resolve, SHOW, select.parse("1,*.iso", None)),
                         "no file matches '*.iso'")
        self.assertEqual(message(select.resolve, SHOW, select.parse("1", "*.iso")),
                         "no file matches '*.iso'")

    def test_an_empty_selection_is_an_error(self):
        self.assertEqual(message(select.resolve, SHOW, select.parse("1", "1")), "no file selected")
        self.assertEqual(message(select.resolve, SHOW, select.parse(None, "*")), "no file selected")


class Listing(unittest.TestCase):
    def test_paths_are_inside_the_item(self):
        self.assertEqual(select.paths(SHOW)[3], "extras/poster.png")
        self.assertEqual(select.paths(specs_of(("a.bin", 1))), ["a.bin"])

    def test_lines_have_a_header_one_row_per_file_and_a_total(self):
        lines = select.list_lines(SHOW)
        self.assertEqual(cols(lines[0]), ["#", "size", "path"])
        self.assertEqual(len(lines), 1 + 5 + 1)
        self.assertEqual(cols(lines[1]), ["1", "1.2 kB", "S01E01.mkv"])
        self.assertEqual(cols(lines[4]), ["4", "34 B", "extras/poster.png"])
        self.assertEqual(lines[-1], "5 files, 2.4 kB")

    def test_filtered_lines_keep_the_numbers_of_the_whole_torrent(self):
        lines = select.list_lines(SHOW, frozenset({2, 4}))
        self.assertEqual([cols(l)[0] for l in lines[1:-1]], ["3", "5"])
        self.assertEqual(lines[-1], "2 files, 17 B")

    def test_one_file_total_is_singular(self):
        self.assertEqual(select.list_lines(specs_of(("a", 5)))[-1], "1 file, 5 B")

    def test_columns_line_up_for_many_files(self):
        specs = specs_of(*[(f"f{i}", 1) for i in range(12)])
        lines = select.list_lines(specs)
        self.assertEqual({l.rindex("  ") for l in lines[:-1]}, {lines[0].rindex("  ")})


class Pieces(unittest.TestCase):
    def test_pieces_touching_picked_files_including_shared_boundaries(self):
        specs = specs_of(("a", 20000), ("b", 10000), ("c", 40000))  # 70000 bytes, pieces of 16384
        self.assertEqual(sorted(select.wanted_pieces(specs, frozenset({0}), PL)), [0, 1])
        self.assertEqual(sorted(select.wanted_pieces(specs, frozenset({1}), PL)), [1])
        self.assertEqual(sorted(select.wanted_pieces(specs, frozenset({2}), PL)), [1, 2, 3, 4])
        self.assertEqual(sorted(select.wanted_pieces(specs, frozenset({0, 2}), PL)), [0, 1, 2, 3, 4])

    def test_zero_length_files_have_no_pieces(self):
        specs = specs_of(("a", 20000), ("z", 0), ("b", 100))
        self.assertEqual(sorted(select.wanted_pieces(specs, frozenset({1}), PL)), [])

    def test_agrees_with_piece_files(self):
        rng = random.Random(7)
        specs = specs_of(*[(f"f{i}", rng.choice([0, 1, 100, 5000, 20000])) for i in range(40)])
        total = sum(f.length for f in specs)
        chosen = frozenset(rng.sample(range(40), 9))
        want = {p for p in range(-(-total // PL))
                if any(i in chosen for i, _ in layout.piece_files(specs, PL, total, p))}
        self.assertEqual(select.wanted_pieces(specs, chosen, PL), want)


class WantedPicker(unittest.TestCase):
    PL = 4 * picker.BLOCK

    def make(self, wanted, npieces=6):
        return picker.Picker(npieces, self.PL, npieces * self.PL, rng=random.Random(1), wanted=wanted)

    def test_only_wanted_pieces_are_requested(self):
        p = self.make({2, 3})
        p.peer_bitfield("a", range(6))
        asked = {r[0] for r in p.next_requests("a", 1000)}
        self.assertEqual(asked, {2, 3})
        self.assertEqual(p.missing, set())
        self.assertEqual(p.want["a"], 2)

    def test_done_once_every_wanted_piece_finished(self):
        p = self.make({2, 3})
        p.peer_bitfield("a", range(6))
        self.assertFalse(p.done())
        reqs = p.next_requests("a", 1000)
        p.mark_verified([2])
        self.assertFalse(p.done())
        for index, begin, length in [r for r in reqs if r[0] == 3]:
            p.block_received("a", index, begin, bytes(length))
        p.piece_done(3)
        self.assertTrue(p.done())
        self.assertEqual(p.finished, {2, 3})

    def test_a_peer_with_only_unwanted_pieces_is_not_interesting(self):
        p = self.make({2, 3})
        p.peer_bitfield("b", [0, 1, 4, 5])
        self.assertFalse(p.interesting("b"))
        self.assertEqual(p.next_requests("b", 100), [])
        p.peer_has("b", 3)
        self.assertTrue(p.interesting("b"))

    def test_a_verified_unwanted_piece_still_counts_in_the_bitfield_not_in_want(self):
        p = self.make({2})
        p.peer_bitfield("a", [0, 2])
        p.mark_verified([0])
        self.assertEqual(p.finished, {0})
        self.assertEqual(p.want["a"], 1)
        self.assertFalse(p.done())
        p.mark_verified([2])
        self.assertTrue(p.done())
        self.assertEqual(p.want["a"], 0)

    def test_nothing_wanted_is_done_at_once(self):
        self.assertTrue(self.make(frozenset()).done())

    def test_no_wanted_argument_is_every_piece(self):
        p = self.make(None, npieces=3)
        p.peer_bitfield("a", range(3))
        self.assertEqual({r[0] for r in p.next_requests("a", 1000)}, {0, 1, 2})


class Benchmark(unittest.TestCase):
    def test_a_selection_over_100k_files_with_5_globs_takes_under_a_second(self):  # AC16
        specs = specs_of(*[(f"d{i % 300}/sub{i % 7}/file{i}.{('mkv', 'srt', 'txt', 'png')[i % 4]}", 100)
                           for i in range(100_000)])
        start = time.perf_counter()
        picks = select.parse("*.mkv,d1/*,*sub3*", "*.txt,d2/*")
        chosen = select.resolve(specs, picks)
        select.wanted_pieces(specs, chosen, PL)
        self.assertLess(time.perf_counter() - start, 1.0)
        self.assertTrue(chosen)


if __name__ == "__main__":
    unittest.main()
