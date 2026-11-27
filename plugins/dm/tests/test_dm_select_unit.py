"""dm 0.8.0 file selection: parsing picks, resolving them against a torrent's files, listing, pieces, the
picker."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dmcore.target import DmError
from dmcore.torrent import select
from dmcore.torrent.layout import FileSpec

PL = 16384


def specs_of(*files):
    """FileSpecs for (path, length) pairs, path as "a/b"."""
    out, offset = [], 0
    for path, n in files:
        out.append(FileSpec(tuple(path.split("/")), n, offset))
        offset += n
    return tuple(out)


SHOW = specs_of(
    ("S01E01.mkv", 1200),
    ("S01E02.mkv", 1100),
    ("S01E01.srt", 12),
    ("extras/poster.png", 34),
    ("notes.txt", 5),
)


def picked(only=None, skip=None, specs=SHOW):
    return sorted(select.resolve(specs, select.parse(only, skip)))


def cols(line):
    return [c.strip() for c in line.strip().split("  ") if c.strip()]


def message(fn, *args):
    with unittest.TestCase().assertRaises(DmError) as cm:
        fn(*args)
    return str(cm.exception)


class Resolve(unittest.TestCase):
    def test_globs_match_the_whole_path_inside_the_item_across_slashes(self):
        self.assertEqual(picked("*E01*"), [0, 2])
        self.assertEqual(picked("extras/*"), [3])
        self.assertEqual(picked("*"), [0, 1, 2, 3, 4])
        self.assertEqual(picked("S01E0?.mkv"), [0, 1])
        self.assertEqual(picked("S01E0[12].srt"), [2])
        self.assertEqual(
            message(select.resolve, SHOW, select.parse("Show/*", None)), "no file matches 'Show/*'"
        )


if __name__ == "__main__":
    unittest.main()
