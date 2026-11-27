"""src.keys.read_key: bytes from a terminal in, key names out."""

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.keys import read_key


class ReadKey(unittest.TestCase):
    def keys(self, data: bytes, n: int | None = None) -> list[str]:
        r, w = os.pipe()
        self.addCleanup(os.close, r)
        os.write(w, data)
        os.close(w)
        out = []
        while n is None or len(out) < n:
            key = read_key(r, timeout=0.5)
            if key in ("", "eof"):
                break
            out.append(key)
        return out

    def test_invalid_utf8_is_dropped_not_fatal(self):
        self.assertEqual(self.keys(b"\xffa\xc3"), ["a"])

    def test_unknown_sequences_are_dropped_and_reading_goes_on(self):
        self.assertEqual(self.keys(b"\x1b[99~x\x1b[200~y\x1b[?1;2cz"), ["x", "y", "z"])


if __name__ == "__main__":
    unittest.main()
