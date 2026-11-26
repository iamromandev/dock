"""src.keys.read_key: bytes from a terminal in, key names out."""

import os
import sys
import time
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

    def test_printable_characters_are_themselves(self):
        self.assertEqual(self.keys(b"a Z~"), ["a", " ", "Z", "~"])

    def test_utf8_characters_are_assembled(self):
        self.assertEqual(self.keys("é日👍".encode()), ["é", "日", "👍"])

    def test_invalid_utf8_is_dropped_not_fatal(self):
        self.assertEqual(self.keys(b"\xffa\xc3"), ["a"])

    def test_enter_tab_backspace(self):
        self.assertEqual(self.keys(b"\r\n\t\x7f\x08"), ["enter", "enter", "tab", "backspace", "backspace"])

    def test_control_keys_have_names(self):
        self.assertEqual(self.keys(b"\x01\x03\x04\x05\x0b\x0c\x15\x17\x1a"),
                         ["ctrl-a", "ctrl-c", "ctrl-d", "ctrl-e", "ctrl-k", "ctrl-l", "ctrl-u", "ctrl-w", "ctrl-z"])

    def test_csi_arrows_home_end_delete(self):
        self.assertEqual(self.keys(b"\x1b[A\x1b[B\x1b[C\x1b[D\x1b[H\x1b[F\x1b[3~"),
                         ["up", "down", "right", "left", "home", "end", "delete"])

    def test_csi_tilde_forms(self):
        self.assertEqual(self.keys(b"\x1b[1~\x1b[4~\x1b[7~\x1b[8~"), ["home", "end", "home", "end"])

    def test_ss3_arrows(self):
        self.assertEqual(self.keys(b"\x1bOA\x1bOB\x1bOC\x1bOD\x1bOH\x1bOF"),
                         ["up", "down", "right", "left", "home", "end"])

    def test_modifiers_in_a_csi_sequence_are_ignored(self):
        self.assertEqual(self.keys(b"\x1b[1;5C"), ["right"])

    def test_unknown_sequences_are_dropped_and_reading_goes_on(self):
        self.assertEqual(self.keys(b"\x1b[99~x\x1b[200~y\x1b[?1;2cz"), ["x", "y", "z"])

    def test_alt_key_is_dropped(self):
        self.assertEqual(self.keys(b"\x1bxa"), ["a"])

    def test_a_lone_escape_is_esc_after_a_short_wait(self):
        r, w = os.pipe()
        self.addCleanup(os.close, r)
        self.addCleanup(os.close, w)
        os.write(w, b"\x1b")
        start = time.monotonic()
        self.assertEqual(read_key(r, timeout=2), "esc")
        self.assertLess(time.monotonic() - start, 1.0)

    def test_timeout_gives_an_empty_string(self):
        r, w = os.pipe()
        self.addCleanup(os.close, r)
        self.addCleanup(os.close, w)
        self.assertEqual(read_key(r, timeout=0.05), "")

    def test_end_of_input_is_ctrl_d(self):
        r, w = os.pipe()
        self.addCleanup(os.close, r)
        os.close(w)
        self.assertEqual(read_key(r, timeout=1), "eof")

    def test_a_sequence_split_across_reads_is_still_one_key(self):
        r, w = os.pipe()
        self.addCleanup(os.close, r)
        self.addCleanup(os.close, w)
        os.write(w, b"\x1b[")
        import threading
        threading.Timer(0.01, os.write, (w, b"A")).start()
        self.assertEqual(read_key(r, timeout=2), "up")


if __name__ == "__main__":
    unittest.main()
