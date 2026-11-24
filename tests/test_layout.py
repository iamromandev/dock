"""cli.live.layout and LiveRegion: what a frame looks like and how it is drawn (R8, R9, AC7, NFR4)."""

import itertools
import random
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from cli import uimsg  # noqa: E402
from cli.areas import Area  # noqa: E402
from cli.live import LiveRegion, layout  # noqa: E402
from cli.textwidth import width  # noqa: E402
from screenkit import Screen  # noqa: E402

SGR = re.compile(r"\x1b\[[0-9;]*m")


def visible(line: str) -> str:
    return SGR.sub("", line)


def make(kind, **p):
    data = getattr(uimsg, kind)(p)
    return Area(kind, data["id"], data, data.get("done", False))


def bar(i, percent=50, **kw):
    return make("bar", id=f"b{i}", label=f"label {i}", percent=percent, message=kw.pop("message", "msg"), **kw)


def table(i, rows=3, title="peers"):
    return make("table", id=f"t{i}", title=title, columns=["peer", "client", "down"],
                rows=[[f"10.0.0.{r}:6881", "qBittorrent 4.6.5", f"{r} kB/s"] for r in range(rows)])


def panel(i, items=6):
    return make("panel", id=f"p{i}", title="stats", items=[[f"key{n}", f"value {n}"] for n in range(items)])


def scenario(n):
    """n areas of mixed kinds, in a fixed order."""
    kinds = [bar, table, panel]
    return [kinds[i % 3](i) for i in range(n)]


class Bars(unittest.TestCase):
    def test_a_bar_shows_label_bar_percent_and_message(self):
        [line] = layout([bar(1, 30)], "", 100, 24)
        self.assertEqual(line, "label 1  [######..............]  30.0%  msg")

    def test_a_bar_without_percent_shows_an_activity_indicator_that_moves(self):
        a = make("bar", id="x", label="meta", percent=None, message="fetching")
        one, two = layout([a], "", 80, 24, tick=0), layout([a], "", 80, 24, tick=1)
        self.assertNotEqual(one, two)
        self.assertTrue(one[0].startswith("meta  ") and one[0].endswith("fetching"))

    def test_a_bar_with_an_empty_label_starts_with_the_bar(self):
        [line] = layout([make("bar", id="x", label="", percent=100, message="done")], "", 80, 24)
        self.assertTrue(line.startswith("["), line)

    def test_a_narrow_terminal_drops_the_bar_but_keeps_the_percent(self):
        [line] = layout([bar(1, 30, message="")], "", 20, 24)
        self.assertIn("30.0%", line)
        self.assertLessEqual(width(line), 20)

    def test_the_label_takes_at_most_30_percent_of_the_width(self):
        [line] = layout([make("bar", id="x", label="L" * 90, percent=10, message="m")], "", 80, 24)
        self.assertTrue(line.startswith("L" * 23 + "…"), line)
        self.assertLessEqual(width(line), 80)


class Tables(unittest.TestCase):
    def test_a_table_has_title_aligned_header_and_rows(self):
        lines = layout([make("table", id="t", title="peers", columns=["peer", "down"],
                             rows=[["a", 1], ["bbb", 22]])], "", 80, 24)
        self.assertEqual(lines, ["peers", "peer  down", "a     1", "bbb   22"])

    def test_cells_are_cut_to_fit_the_width(self):
        lines = layout([table(1, 2)], "", 30, 24)
        self.assertTrue(all(width(line) <= 30 for line in lines))
        self.assertTrue(any("…" in line for line in lines))

    def test_a_table_over_budget_is_cut_with_a_count_of_the_rest(self):
        lines = layout([table(1, 500)], "", 80, 10)
        self.assertEqual(len(lines), 8)
        self.assertEqual(lines[-1], "… 495 more rows")
        self.assertEqual(lines[1].split()[0], "peer")

    def test_wide_characters_are_aligned_by_columns(self):
        lines = layout([make("table", id="t", columns=["名前", "n"], rows=[["日本語", 1], ["ab", 2]])], "", 80, 24)
        self.assertEqual(width(lines[0][:lines[0].index("n")]), width(lines[1][:lines[1].index("1")]))


class Panels(unittest.TestCase):
    def test_a_panel_has_a_title_line_and_one_item_per_row_when_narrow(self):
        lines = layout([make("panel", id="p", title="stats", items=[["speed", "1 MB/s"], ["peers", 3]])], "", 40, 24)
        self.assertEqual(lines, ["┌ stats", "│ speed  1 MB/s", "│ peers  3"])

    def test_items_are_packed_two_per_row_from_60_columns(self):
        lines = layout([panel(1, 4)], "", 80, 24)
        self.assertEqual(len(lines), 3)
        self.assertIn("key0", lines[1])
        self.assertIn("key1", lines[1])
        self.assertIn("key2", lines[2])

    def test_the_title_is_bold_with_color_and_plain_without(self):
        p = make("panel", id="p", title="stats", items=[["a", "b"]])
        self.assertEqual(layout([p], "", 80, 24, color=True)[0], "\x1b[1m┌ stats\x1b[0m")
        self.assertNotIn("\x1b", "".join(layout([p], "", 80, 24, color=False)))


class Budget(unittest.TestCase):
    def test_the_partial_output_line_is_drawn_first(self):
        lines = layout([bar(1)], "downloading...", 80, 24)
        self.assertEqual(lines[0], "downloading...")
        self.assertEqual(len(lines), 2)

    def test_tail_lines_come_last_and_are_never_dropped(self):
        lines = layout(scenario(32), "", 80, 10, tail=["Overwrite? (y/N) "])
        self.assertEqual(lines[-1], "Overwrite? (y/N) ")
        self.assertLessEqual(len(lines), 8)

    def test_tables_are_cut_before_areas_are_collapsed(self):
        lines = layout([bar(1), table(2, 100), panel(3)], "", 80, 24)
        text = "\n".join(lines)
        self.assertIn("more rows", text)
        self.assertIn("key0", text)  # the panel keeps its detail
        self.assertEqual(len(lines), 22)

    def test_oldest_areas_collapse_to_one_line_when_cutting_tables_is_not_enough(self):
        areas = [panel(i, 6) for i in range(5)] + [table(9, 50)]
        lines = layout(areas, "", 80, 10)
        self.assertLessEqual(len(lines), 8)
        self.assertIn("┌ stats", lines[0])
        self.assertNotIn("│", lines[0])

    def test_when_even_one_line_each_is_too_many_the_oldest_are_dropped_with_a_count(self):
        areas = [bar(i) for i in range(20)]
        lines = layout(areas, "", 80, 10)
        self.assertEqual(len(lines), 8)
        self.assertRegex(lines[-1], r"^… \d+ more$")
        self.assertIn("label 19", "\n".join(lines))

    def test_a_terminal_narrower_than_20_columns_shows_one_line_per_area(self):
        areas = [bar(1), panel(2), table(3, 5)]
        lines = layout(areas, "", 15, 50)
        self.assertEqual(len(lines), 3)
        self.assertTrue(all(width(line) <= 15 for line in lines))

    def test_nothing_is_shown_without_areas_or_partial(self):
        self.assertEqual(layout([], "", 80, 24), [])


class Fits(unittest.TestCase):
    def test_every_line_fits_and_the_region_fits_the_terminal_in_every_size(self):
        big = [table(100, 500), *scenario(32)[:31]]
        for cols, rows, areas in itertools.product((*range(1, 20), 20, 80, 200), (10, 50), (scenario(1), scenario(5), scenario(32), big)):
            for color in (False, True):
                lines = layout(areas, "partial " * 20, cols, rows, tick=3, color=color)
                with self.subTest(cols=cols, rows=rows, areas=len(areas), color=color):
                    self.assertLessEqual(len(lines), max(1, rows - 2))
                    for line in lines:
                        self.assertNotIn("\n", line)
                        self.assertLessEqual(width(visible(line)), cols, line)

    def test_a_500_row_table_among_32_areas_shows_more_rows_and_collapses_older_areas(self):
        areas = [*scenario(31), table(99, 500)]
        for cols, rows in ((20, 10), (80, 24), (200, 50)):
            lines = layout(areas, "", cols, rows)
            with self.subTest(cols=cols, rows=rows):
                self.assertLessEqual(len(lines), rows - 2)
                self.assertTrue(any("more" in line for line in lines))

    def test_a_seeded_fuzz_of_areas_sizes_and_texts_never_overflows_in_width_or_height(self):
        rnd = random.Random(1)
        alphabet = ["a", "bb", "日本", "e\u0301", "😀", "x" * 30, "", "·", "→"]

        def text():
            return "".join(rnd.choice(alphabet) for _ in range(rnd.randint(0, 6)))

        for _ in range(1500):
            areas = []
            for i in range(rnd.randint(0, 32)):
                kind = rnd.choice(["bar", "table", "panel"])
                if kind == "bar":
                    areas.append(make("bar", id=f"i{i}", label=text(), percent=rnd.choice([None, 0, 33, 100]),
                                      message=text(), done=rnd.random() < 0.2))
                elif kind == "table":
                    n = rnd.randint(0, 70)
                    areas.append(make("table", id=f"i{i}", title=text(), columns=[text() for _ in range(min(n, 64))],
                                      rows=[[text() for _ in range(rnd.randint(0, n))] for _ in range(rnd.randint(0, 60))]))
                else:
                    areas.append(make("panel", id=f"i{i}", title=text(),
                                      items=[[text(), text()] for _ in range(rnd.randint(0, 8))]))
            cols, rows = rnd.randint(1, 120), rnd.randint(1, 60)
            partial, tail = text() * rnd.randint(0, 3), [text() for _ in range(rnd.randint(0, 3))]
            lines = layout(areas, partial, cols, rows, 3, True, tail)
            with self.subTest(cols=cols, rows=rows, areas=len(areas)):
                self.assertLessEqual(len(lines), max(1, rows - 2))
                for line in lines:
                    self.assertNotIn("\n", line)
                    self.assertLessEqual(width(visible(line)), cols, line)

    def test_wide_characters_never_overflow(self):
        areas = [make("bar", id="b", label="日本語" * 20, percent=10, message="日本語" * 20),
                 make("table", id="t", columns=["日本語" * 5], rows=[["日本語" * 9]] * 4),
                 make("panel", id="p", title="日本語" * 30, items=[["日" * 30, "本" * 30]])]
        for cols in (*range(1, 22), 79, 80, 81, 200):
            for line in layout(areas, "日本語" * 40, cols, 24):
                self.assertLessEqual(width(line), cols)


class Region(unittest.TestCase):
    def setUp(self):
        self.screen = Screen(40, 10)
        self.region = LiveRegion(lambda s: self.screen.feed(s))

    def test_after_the_width_shrinks_the_region_climbs_over_the_lines_as_the_terminal_reflowed_them(self):
        out, size = [], [40]
        region = LiveRegion(out.append, lambda: size[0])
        region.draw(["a" * 30, "b" * 10, "c" * 40])            # 3 rows at 40 columns
        size[0] = 20                                            # reflowed: 2 + 1 + 2 rows
        region.invalidate()
        region.draw(["new"])
        self.assertIn("\x1b[4A\x1b[J", out[-1])
        out.clear()
        region.erase()                                          # one row: nothing to climb
        self.assertEqual(out, ["\r\x1b[J\x1b[?25h"])

    def test_wide_characters_and_a_bold_title_count_as_they_are_shown_when_the_region_is_reflowed(self):
        out, size = [], [40]
        region = LiveRegion(out.append, lambda: size[0])
        region.draw(["\x1b[1m" + "日" * 15 + "\x1b[0m", "x"])   # 30 columns, then 1
        size[0] = 10
        region.draw(["new"])
        self.assertIn("\x1b[3A", out[-1])                      # 3 rows for the first line, 1 for the second

    def test_a_region_that_is_not_narrowed_climbs_by_its_rows(self):
        out = []
        region = LiveRegion(out.append, lambda: 40)
        region.draw(["a", "b", "c"])
        region.draw(["x"])
        self.assertIn("\x1b[2A", out[-1])

    def test_a_redraw_replaces_the_region_in_place(self):
        self.region.draw(["one", "two", "three"])
        self.region.draw(["ONE", "TWO", "THREE"])
        self.assertEqual(self.screen.lines()[:4], ["ONE", "TWO", "THREE", ""])
        self.assertEqual(self.screen.scrollback, [])

    def test_a_shorter_frame_leaves_nothing_of_the_longer_one(self):
        self.region.draw(["a", "b", "c", "d"])
        self.region.draw(["a"])
        self.assertEqual(self.screen.text(), "a")

    def test_a_region_taller_than_the_space_below_scrolls_once_and_stays_in_place(self):
        self.screen.feed("\n" * 8)
        for i in range(3):
            self.region.draw([f"{i}-1", f"{i}-2", f"{i}-3", f"{i}-4"])
        self.assertEqual(self.screen.lines()[-4:], ["2-1", "2-2", "2-3", "2-4"])
        self.assertEqual([s for s in self.screen.scrollback if s], [])

    def test_the_cursor_is_hidden_while_drawn_and_shown_by_erase(self):
        self.region.draw(["x"])
        self.assertFalse(self.screen.cursor_visible)
        self.region.erase()
        self.assertTrue(self.screen.cursor_visible)
        self.assertEqual(self.screen.text(), "")

    def test_text_above_the_region_stays_and_the_region_comes_back_below_it(self):
        self.region.draw(["live 1", "live 2"])
        self.region.above("log line\n")
        self.region.draw(["live 1", "live 2"])
        self.assertEqual(self.screen.text(), "log line\nlive 1\nlive 2")

    def test_an_identical_frame_is_not_written_again(self):
        self.region.draw(["same"])
        seen = len(self.screen.seqs)
        self.region.draw(["same"])
        self.assertEqual(len(self.screen.seqs), seen)

    def test_forget_stops_the_next_draw_from_climbing_over_older_text(self):
        self.region.draw(["old 1", "old 2"])
        self.region.forget()
        self.screen.feed("\r\nnew prompt line\r\n")
        self.region.draw(["fresh"])
        self.assertEqual(self.screen.text(), "old 1\nold 2\nnew prompt line\nfresh")

    def test_only_whitelisted_sequences_are_written(self):
        allowed = re.compile(r"\x1b\[(\d*A|J|K|\?25[hl])")
        self.region.draw(["a", "b"])
        self.region.draw(["a"])
        self.region.above("x\n")
        self.region.erase()
        for s in self.screen.seqs:
            self.assertRegex(s, allowed)
        self.assertEqual(self.screen.controls, [])


if __name__ == "__main__":
    unittest.main()
