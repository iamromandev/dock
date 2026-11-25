"""The model of a run's live areas, and their plain-text form. No runtime needed."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import uimsg
from src.areas import Area, Areas, plain_lines


def bar(i="a", **kw):
    return uimsg.bar({"id": i, **kw})


def table(i="t", columns=("a",), rows=(), **kw):
    return uimsg.table({"id": i, "columns": list(columns), "rows": [list(r) for r in rows], **kw})


def panel(i="p", items=(), **kw):
    return uimsg.panel({"id": i, "items": [list(x) for x in items], **kw})


class AreasModel(unittest.TestCase):
    def test_apply_creates_in_order_and_updates_in_place(self):
        a = Areas()
        a.apply("bar", bar("one", percent=1))
        a.apply("table", table("two"))
        a.apply("bar", bar("one", percent=2))
        self.assertEqual([(x.kind, x.id) for x in a.items()], [("bar", "one"), ("table", "two")])
        self.assertEqual(a.items()[0].data["percent"], 2.0)

    def test_done_follows_the_bar(self):
        a = Areas()
        a.apply("bar", bar(percent=100, done=True))
        self.assertTrue(a.items()[0].done)
        a.apply("table", table())
        self.assertFalse(a.items()[1].done)

    def test_same_id_for_another_kind_is_a_new_area(self):
        a = Areas()
        a.apply("bar", bar("x"))
        a.apply("table", table("x"))
        self.assertEqual([(x.kind, x.id) for x in a.items()], [("bar", "x"), ("table", "x")])

    def test_clear_returns_the_removed_area_and_forgets_it(self):
        a = Areas()
        a.apply("bar", bar("x", percent=5))
        removed = a.clear("bar", "x")
        self.assertEqual((removed.kind, removed.id, removed.data["percent"]), ("bar", "x", 5.0))
        self.assertEqual(a.items(), [])

    def test_clear_of_an_unknown_id_or_kind_is_none(self):
        a = Areas()
        a.apply("bar", bar("x"))
        self.assertIsNone(a.clear("bar", "nope"))
        self.assertIsNone(a.clear("table", "x"))
        self.assertEqual(len(a.items()), 1)

    def test_update_after_clear_creates_a_new_area_at_the_end(self):
        a = Areas()
        a.apply("bar", bar("x"))
        a.apply("bar", bar("y"))
        a.clear("bar", "x")
        a.apply("bar", bar("x"))
        self.assertEqual([x.id for x in a.items()], ["y", "x"])

    def test_the_33rd_area_is_invalid_but_updates_still_work(self):
        a = Areas()
        for i in range(uimsg.MAX_AREAS):
            a.apply("bar", bar(f"b{i}"))
        with self.assertRaises(uimsg.Invalid):
            a.apply("bar", bar("extra"))
        a.apply("bar", bar("b0", percent=9))  # existing: fine
        a.clear("bar", "b1")
        a.apply("bar", bar("extra"))  # room again
        self.assertEqual(len(a.items()), uimsg.MAX_AREAS)

    def test_items_is_a_copy(self):
        a = Areas()
        a.apply("bar", bar())
        a.items().clear()
        self.assertEqual(len(a.items()), 1)


class PlainBar(unittest.TestCase):
    def lines(self, **kw):
        return plain_lines(Area("bar", "a", bar(**kw), kw.get("done", False)))

    def test_with_percent(self):
        self.assertEqual(self.lines(label="file", percent=30, message="3 of 10"),
                         ["file  [######..............]  30.0%  3 of 10"])

    def test_percent_edges(self):
        self.assertEqual(self.lines(label="x", percent=0), ["x  [....................]  0.0%"])
        self.assertEqual(self.lines(label="x", percent=100), ["x  [####################]  100.0%"])

    def test_unknown_percent_shows_an_ellipsis(self):
        self.assertEqual(self.lines(label="x", message="waiting"), ["x  …  waiting"])

    def test_done_without_percent_says_done(self):
        self.assertEqual(self.lines(label="x", done=True), ["x  done"])
        self.assertEqual(self.lines(label="x", message="ok", done=True), ["x  done  ok"])

    def test_empty_label_is_omitted(self):
        self.assertEqual(self.lines(percent=50, message="m"), ["[##########..........]  50.0%  m"])
        self.assertEqual(self.lines(message="m"), ["…  m"])


class PlainTable(unittest.TestCase):
    def lines(self, **kw):
        return plain_lines(Area("table", "t", table(**kw)))

    def test_title_header_and_aligned_rows(self):
        self.assertEqual(
            self.lines(title="peers", columns=("peer", "down"), rows=[("1.2.3.4", "1 MB/s"), ("x", "2")]),
            ["peers", "peer     down", "1.2.3.4  1 MB/s", "x        2"])

    def test_no_title_no_title_line(self):
        self.assertEqual(self.lines(columns=("a", "b"), rows=[("1", "2")]), ["a  b", "1  2"])

    def test_wide_characters_align_by_display_width(self):
        self.assertEqual(self.lines(columns=("n", "v"), rows=[("日本", "1"), ("ab", "2")]),
                         ["n     v", "日本  1", "ab    2"])

    def test_ragged_rows_are_padded_or_extended(self):
        self.assertEqual(self.lines(columns=("a", "b"), rows=[("1",), ("1", "2", "3")]),
                         ["a  b", "1", "1  2  3"])

    def test_numbers_and_empty_table(self):
        self.assertEqual(self.lines(columns=("n",), rows=[(1,), (2.5,)]), ["n", "1", "2.5"])
        self.assertEqual(self.lines(title="t", columns=(), rows=[]), ["t"])

    def test_no_width_cut(self):
        long = "x" * 300
        self.assertIn(long, self.lines(columns=("a",), rows=[(long,)]))


class PlainPanel(unittest.TestCase):
    def test_title_and_aligned_pairs(self):
        a = Area("panel", "p", panel(title="stats", items=[("speed", "4 MB/s"), ("eta", "1m")]))
        self.assertEqual(plain_lines(a), ["stats", "speed  4 MB/s", "eta    1m"])

    def test_no_title(self):
        self.assertEqual(plain_lines(Area("panel", "p", panel(items=[("a", "b")]))), ["a  b"])


if __name__ == "__main__":
    unittest.main()
