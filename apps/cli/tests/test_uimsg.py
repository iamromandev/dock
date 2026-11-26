"""Validation and sanitising of the v2 UI messages a plugin sends. No runtime needed."""

import random
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
    return [c for c in s if (ord(c) < 0x20 and c not in "\n\t") or ord(c) == 0x7F or 0x80 <= ord(c) <= 0x9F
            or (unicodedata.category(c) in ("Cf", "Zl", "Zp") and c not in uimsg.JOINERS)]


class Sanitize(unittest.TestCase):
    def test_every_control_character_is_replaced(self):
        for cp in [*range(0x20), 0x7F, *range(0x80, 0xA0)]:
            with self.subTest(cp=hex(cp)):
                self.assertEqual(uimsg.sanitize(f"a{chr(cp)}b"), f"a{FFFD}b")

    def test_newline_and_tab_only_when_allowed(self):
        self.assertEqual(uimsg.sanitize("a\nb\tc"), f"a{FFFD}b{FFFD}c")
        self.assertEqual(uimsg.sanitize("a\nb\tc", newlines=True), f"a\nb{FFFD}c")
        self.assertEqual(uimsg.sanitize("a\nb\tc", newlines=True, tabs=True), "a\nb    c")
        self.assertEqual(uimsg.sanitize("a\tb", tabs=True), "a    b")

    def test_escape_sequences_lose_their_escape(self):
        out = uimsg.sanitize("\x1b[31mred\x1b]0;title\x07\x9b2J")
        self.assertEqual(bad_chars(out), [])
        self.assertNotIn("\x1b", out)

    def test_lone_surrogates_are_replaced_so_the_text_can_be_encoded(self):
        for cp in (0xD800, 0xDBFF, 0xDC00, 0xDFFF):
            with self.subTest(cp=hex(cp)):
                out = uimsg.sanitize(f"a{chr(cp)}b")
                self.assertEqual(out, f"a{FFFD}b")
                out.encode("utf-8")
        uimsg.sanitize("\ud800\n\x1b", newlines=True, tabs=True).encode("utf-8")

    def test_format_characters_and_line_separators_are_replaced(self):
        """Bidi overrides and isolates reorder what the terminal shows; U+2028 and U+2029 end a line on some."""
        for cp in [*range(0x202A, 0x202F), *range(0x2066, 0x206A), 0x200E, 0x200F, 0x061C, 0xFEFF, 0x200B, 0xAD,
                   0x2060, 0xE0041, 0x2028, 0x2029]:
            with self.subTest(cp=hex(cp)):
                self.assertEqual(uimsg.sanitize(f"a{chr(cp)}b", newlines=True), f"a{FFFD}b")

    def test_every_format_character_the_unicode_database_knows_is_replaced_except_the_joiners(self):
        found = [cp for cp in range(0x110000) if unicodedata.category(chr(cp)) in ("Cf", "Zl", "Zp")]
        self.assertGreater(len(found), 100)
        for cp in found:
            kept = chr(cp) in uimsg.JOINERS
            self.assertEqual(uimsg.sanitize(f"a{chr(cp)}b") == f"a{chr(cp)}b", kept, hex(cp))

    def test_the_joiners_stay_so_emoji_sequences_and_persian_words_survive(self):
        family = "👨\u200d👩\u200d👧"  # ZWJ sequence
        heart = "❤\ufe0f"  # variation selector (a mark, not a format character)
        persian = "می\u200cخواهم"  # ZWNJ
        for text in (family, heart, persian):
            self.assertEqual(uimsg.sanitize(text), text)

    def test_ordinary_text_is_unchanged(self):
        text = "héllo 日本語 😀 ~ ok"
        self.assertEqual(uimsg.sanitize(text), text)

    def test_fuzz_leaves_no_control_characters(self):
        rng = random.Random(1234)
        pool = [chr(i) for i in range(0xA0)] + list("abc 日é😀‍\u202e\u2066\u2028\u2029\u200b\ufeff\u00ad")
        for _ in range(2000):
            s = "".join(rng.choice(pool) for _ in range(rng.randint(0, 40)))
            for newlines, tabs in ((False, False), (True, False), (True, True)):
                self.assertEqual(bad_chars(uimsg.sanitize(s, newlines=newlines, tabs=tabs)), [])


class Rejects(unittest.TestCase):
    """Every case is (validator, params): Invalid, with code -32602 and a reason."""

    def assertInvalid(self, fn, params, part=""):
        with self.assertRaises(uimsg.Invalid) as cm:
            fn(params)
        self.assertEqual(cm.exception.code, -32602)
        self.assertIsInstance(cm.exception, PluginError)
        self.assertIn(part, str(cm.exception))

    def test_ids(self):
        for fn, p in ((uimsg.bar, {}), (uimsg.table, {"columns": [], "rows": []}),
                      (uimsg.panel, {"items": []}), (uimsg.clear, {"kind": "bar"})):
            with self.subTest(fn=fn.__name__, case="missing"):
                self.assertInvalid(fn, p, "id")
        for bad in (1, None, "", ["x"], True):
            with self.subTest(id=bad):
                self.assertInvalid(uimsg.bar, {"id": bad}, "id")

    def test_id_length_limit(self):
        uimsg.bar({"id": "x" * uimsg.MAX_ID})
        self.assertInvalid(uimsg.bar, {"id": "x" * (uimsg.MAX_ID + 1)}, "id")

    def test_not_an_object(self):
        for fn in (uimsg.bar, uimsg.table, uimsg.panel, uimsg.clear, uimsg.form):
            for bad in (None, [], "x", 3):
                with self.subTest(fn=fn.__name__, p=bad):
                    self.assertInvalid(fn, bad)

    def test_bar_field_types(self):
        for key, value in (("label", 3), ("message", ["x"]), ("percent", "50"), ("percent", True),
                           ("done", "yes"), ("done", 1)):
            with self.subTest(key=key, value=value):
                self.assertInvalid(uimsg.bar, {"id": "a", key: value}, key)

    def test_table_field_types(self):
        base = {"id": "t", "columns": ["a"], "rows": [["x"]]}
        cases = [
            ({"columns": "a"}, "columns"), ({"columns": [1]}, "columns"), ({"columns": None}, "columns"),
            ({"rows": "x"}, "rows"), ({"rows": ["x"]}, "rows"), ({"rows": [[None]]}, "rows"),
            ({"rows": [[["x"]]]}, "rows"), ({"rows": [[True]]}, "rows"), ({"title": 3}, "title"),
        ]
        for change, part in cases:
            with self.subTest(change=change):
                self.assertInvalid(uimsg.table, {**base, **change}, part)
        for key in ("columns", "rows"):
            with self.subTest(missing=key):
                self.assertInvalid(uimsg.table, {k: v for k, v in base.items() if k != key}, key)

    def test_table_limits(self):
        uimsg.table({"id": "t", "columns": ["c"] * uimsg.MAX_COLS, "rows": []})
        self.assertInvalid(uimsg.table, {"id": "t", "columns": ["c"] * (uimsg.MAX_COLS + 1), "rows": []}, "columns")
        uimsg.table({"id": "t", "columns": ["c"], "rows": [["x"]] * uimsg.MAX_ROWS})
        self.assertInvalid(uimsg.table, {"id": "t", "columns": ["c"], "rows": [["x"]] * (uimsg.MAX_ROWS + 1)}, "rows")

    def test_panel_field_types(self):
        base = {"id": "p", "items": [["k", "v"]]}
        cases = [({"items": "x"}, "items"), ({"items": ["kv"]}, "items"), ({"items": [["k"]]}, "items"),
                 ({"items": [["k", "v", "w"]]}, "items"), ({"items": [[1, None]]}, "items"),
                 ({"items": [[["k"], "v"]]}, "items"), ({"title": 3}, "title")]
        for change, part in cases:
            with self.subTest(change=change):
                self.assertInvalid(uimsg.panel, {**base, **change}, part)
        self.assertInvalid(uimsg.panel, {"id": "p"}, "items")

    def test_clear_kind(self):
        for kind in ("form", "", None, 3, "Bar"):
            with self.subTest(kind=kind):
                self.assertInvalid(uimsg.clear, {"id": "a", "kind": kind}, "kind")
        self.assertInvalid(uimsg.clear, {"id": "a"}, "kind")

    def test_form_shape(self):
        ok = {"name": "a", "label": "A", "type": "text"}
        cases = [
            ({"fields": "x"}, "fields"), ({"fields": ["x"]}, "fields"), ({"title": 3, "fields": [ok]}, "title"),
            ({"fields": [{**ok, "name": ""}]}, "name"), ({"fields": [{**ok, "name": 3}]}, "name"),
            ({"fields": [{**ok, "label": 3}]}, "label"),
            ({"fields": [{k: v for k, v in ok.items() if k != "label"}]}, "label"),
            ({"fields": [{**ok, "type": "number"}]}, "type"), ({"fields": [{**ok, "type": None}]}, "type"),
            ({"fields": [{**ok, "default": 3}]}, "default"), ({"fields": [{**ok, "secret": "y"}]}, "secret"),
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

    def test_form_limits(self):
        def fields(n):
            return [{"name": f"f{i}", "label": "L", "type": "text"} for i in range(n)]
        uimsg.form({"fields": fields(uimsg.MAX_FIELDS)})
        self.assertInvalid(uimsg.form, {"fields": fields(uimsg.MAX_FIELDS + 1)}, "fields")
        choice = {"name": "c", "label": "C", "type": "choice"}
        uimsg.form({"fields": [{**choice, "choices": ["x"] * uimsg.MAX_CHOICES}]})
        self.assertInvalid(uimsg.form, {"fields": [{**choice, "choices": ["x"] * (uimsg.MAX_CHOICES + 1)}]}, "choices")


class Normalises(unittest.TestCase):
    def test_bar_defaults(self):
        self.assertEqual(uimsg.bar({"id": "a"}),
                         {"id": "a", "label": "", "percent": None, "message": "", "done": False})

    def test_bar_percent(self):
        cases = [(0, 0.0), (50, 50.0), (33.5, 33.5), (-5, 0.0), (101, 100.0), (1e9, 100.0),
                 (float("inf"), 100.0), (float("-inf"), 0.0), (float("nan"), None), (None, None)]
        for given, want in cases:
            with self.subTest(percent=given):
                got = uimsg.bar({"id": "a", "percent": given})["percent"]
                self.assertEqual(got, want)
                self.assertTrue(got is None or isinstance(got, float))

    def test_bar_text_is_sanitised(self):
        b = uimsg.bar({"id": "a", "label": "l\x1b[0m", "message": "m\n\x07", "done": True})
        self.assertEqual((b["label"], b["message"], b["done"]), (f"l{FFFD}[0m", f"m{FFFD}{FFFD}", True))

    def test_table_numbers_become_strings_and_text_is_sanitised(self):
        t = uimsg.table({"id": "t", "title": "T\x1b", "columns": ["a\x9b", "b"], "rows": [[1, 2.5], ["x\x00", -3]]})
        self.assertEqual(t, {"id": "t", "title": f"T{FFFD}", "columns": [f"a{FFFD}", "b"],
                             "rows": [["1", "2.5"], [f"x{FFFD}", "-3"]]})

    def test_table_without_title(self):
        self.assertEqual(uimsg.table({"id": "t", "columns": [], "rows": []})["title"], "")

    def test_panel(self):
        p = uimsg.panel({"id": "p", "title": "P", "items": [["speed", 4.2], ["a\x1b", "b"]]})
        self.assertEqual(p, {"id": "p", "title": "P", "items": [("speed", "4.2"), (f"a{FFFD}", "b")]})

    def test_clear(self):
        self.assertEqual(uimsg.clear({"id": "a", "kind": "table"}), {"id": "a", "kind": "table"})

    def test_form(self):
        f = uimsg.form({"title": "T\x1b", "fields": [
            {"name": "n", "label": "N\x07", "type": "text", "default": "d", "secret": True},
            {"name": "c", "label": "C", "type": "confirm", "default": True},
            {"name": "k", "label": "K", "type": "choice", "choices": ["a", "b\x1b"], "default": "a"},
        ]})
        self.assertEqual(f["title"], f"T{FFFD}")
        text, confirm, choice = f["fields"]
        self.assertEqual(text, {"name": "n", "label": f"N{FFFD}", "type": "text", "default": "d",
                                "secret": True, "choices": None, "values": None, "raw_default": "d"})
        self.assertEqual((confirm["type"], confirm["default"], confirm["secret"]), ("confirm", True, False))
        self.assertEqual((choice["choices"], choice["default"]), (["a", f"b{FFFD}"], "a"))

    def test_a_form_keeps_the_original_choice_and_default_strings_next_to_the_display_ones(self):
        f = uimsg.form({"fields": [
            {"name": "k", "label": "K", "type": "choice", "choices": ["a\x1b", "b\x07"], "default": "b\x07"},
            {"name": "t", "label": "T", "type": "text", "default": "d\x1b[0m"}]})
        choice, text = f["fields"]
        self.assertEqual((choice["choices"], choice["default"]), ([f"a{FFFD}", f"b{FFFD}"], f"b{FFFD}"))
        self.assertEqual((choice["values"], choice["raw_default"]), (["a\x1b", "b\x07"], "b\x07"))
        self.assertEqual((text["default"], text["raw_default"]), (f"d{FFFD}[0m", "d\x1b[0m"))

    def test_form_defaults_are_none_when_absent(self):
        f = uimsg.form({"fields": [{"name": "n", "label": "N", "type": "text"}]})
        self.assertEqual((f["title"], f["fields"][0]["default"]), ("", None))

    def test_fuzz_no_control_characters_reach_any_text(self):
        rng = random.Random(99)
        pool = [chr(i) for i in range(0xA0)] + list("ab 日")

        def s():
            return "".join(rng.choice(pool) for _ in range(rng.randint(0, 20)))

        def flat(x):
            if isinstance(x, str):
                yield x
            elif isinstance(x, dict):
                for k, v in x.items():
                    if k not in ("id", "name", "values", "raw_default"):  # keys, and answers: never drawn
                        yield from flat(v)
            elif isinstance(x, (list, tuple)):
                for v in x:
                    yield from flat(v)

        for _ in range(300):
            outs = [
                uimsg.bar({"id": "a", "label": s(), "message": s()}),
                uimsg.table({"id": "t", "title": s(), "columns": [s()], "rows": [[s()]]}),
                uimsg.panel({"id": "p", "title": s(), "items": [[s(), s()]]}),
                uimsg.form({"title": s(), "fields": [{"name": "c", "label": s(), "type": "choice",
                                                      "choices": [s()]}]}),
            ]
            for out in outs:
                for text in flat(out):
                    self.assertEqual(bad_chars(text), [], text)
                    self.assertNotIn("\n", text)
                    self.assertNotIn("\t", text)


if __name__ == "__main__":
    unittest.main()
