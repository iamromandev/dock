"""Validation and sanitising of the v2 UI messages a plugin sends. No runtime needed."""

import sys
import unicodedata
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import uimsg
from src.process import PluginError

FFFD = "�"


def bad_chars(s: str) -> list[str]:
    return [
        c
        for c in s
        if (ord(c) < 0x20 and c not in "\n\t")
        or ord(c) == 0x7F
        or 0x80 <= ord(c) <= 0x9F
        or (unicodedata.category(c) in ("Cf", "Zl", "Zp") and c not in uimsg.JOINERS)
    ]


class Rejects(unittest.TestCase):
    """Every case is (validator, params): Invalid, with code -32602 and a reason."""

    def assertInvalid(self, fn, params, part=""):
        with self.assertRaises(uimsg.Invalid) as cm:
            fn(params)
        self.assertEqual(cm.exception.code, -32602)
        self.assertIsInstance(cm.exception, PluginError)
        self.assertIn(part, str(cm.exception))

    def test_form_shape(self):
        ok = {"name": "a", "label": "A", "type": "text"}
        cases = [
            ({"fields": "x"}, "fields"),
            ({"fields": ["x"]}, "fields"),
            ({"title": 3, "fields": [ok]}, "title"),
            ({"fields": [{**ok, "name": ""}]}, "name"),
            ({"fields": [{**ok, "name": 3}]}, "name"),
            ({"fields": [{**ok, "label": 3}]}, "label"),
            ({"fields": [{k: v for k, v in ok.items() if k != "label"}]}, "label"),
            ({"fields": [{**ok, "type": "number"}]}, "type"),
            ({"fields": [{**ok, "type": None}]}, "type"),
            ({"fields": [{**ok, "default": 3}]}, "default"),
            ({"fields": [{**ok, "secret": "y"}]}, "secret"),
            ({"fields": [ok, ok]}, "duplicate"),
            ({"fields": [{**ok, "type": "confirm", "default": "y"}]}, "default"),
            ({"fields": [{**ok, "type": "choice"}]}, "choices"),
            ({"fields": [{**ok, "type": "choice", "choices": []}]}, "choices"),
            ({"fields": [{**ok, "type": "choice", "choices": "ab"}]}, "choices"),
            ({"fields": [{**ok, "type": "choice", "choices": [1]}]}, "choices"),
            ({"fields": [{**ok, "type": "choice", "choices": ["a"], "default": "b"}]}, "default"),
        ]
        for p, part in cases:
            with self.subTest(p=p):
                self.assertInvalid(uimsg.form, p, part)
        self.assertInvalid(uimsg.form, {}, "fields")


if __name__ == "__main__":
    unittest.main()
