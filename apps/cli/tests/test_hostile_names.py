"""An index written by an older dock may hold command names that load now refuses: they are still shown safely."""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import main
from src.shell import Shell
from src.store import Store

EVIL = "go\x1b]0;pwned\x07\x9b2J\ud800"


def has_control(s: str) -> bool:
    return any((ord(c) < 0x20 and c != "\n") or 0x7F <= ord(c) <= 0x9F or 0xD800 <= ord(c) <= 0xDFFF for c in s)


class HostileIndex(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Store(Path(tmp.name))
        self.store.index_path.write_text(json.dumps({
            "dev.dock.evil": {"version": "1", "path": "x", "description": "", "keywords": {},
                              "commands": {EVIL: "summary\x1b[2J"}}}))

    def test_completion_candidates_are_sanitised(self):
        found = Shell(self.store, None).candidates("g", "g")
        self.assertEqual(len(found), 1)
        self.assertFalse(has_control(found[0]), repr(found[0]))

    def test_help_lists_names_and_summaries_sanitised(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main.cmd_help(self.store, in_shell=True)
        self.assertFalse(has_control(out.getvalue()), repr(out.getvalue()))
        self.assertIn("go�", out.getvalue())


if __name__ == "__main__":
    unittest.main()
