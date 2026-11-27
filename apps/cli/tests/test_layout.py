"""src.live.layout and LiveRegion: what a frame looks like and how it is drawn (R8, R9, AC7, NFR4)."""

import random
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from src import uimsg
from src.areas import Area
from src.live import layout
from src.textwidth import width

SGR = re.compile(r"\x1b\[[0-9;]*m")


def visible(line: str) -> str:
    return SGR.sub("", line)


def make(kind, **p):
    data = getattr(uimsg, kind)(p)
    return Area(kind, data["id"], data, data.get("done", False))


def bar(i, percent=50, **kw):
    return make(
        "bar", id=f"b{i}", label=f"label {i}", percent=percent, message=kw.pop("message", "msg"), **kw
    )


def table(i, rows=3, title="peers"):
    return make(
        "table",
        id=f"t{i}",
        title=title,
        columns=["peer", "client", "down"],
        rows=[[f"10.0.0.{r}:6881", "qBittorrent 4.6.5", f"{r} kB/s"] for r in range(rows)],
    )


def panel(i, items=6):
    return make("panel", id=f"p{i}", title="stats", items=[[f"key{n}", f"value {n}"] for n in range(items)])


def scenario(n):
    """n areas of mixed kinds, in a fixed order."""
    kinds = [bar, table, panel]
    return [kinds[i % 3](i) for i in range(n)]


class Fits(unittest.TestCase):
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
                    areas.append(
                        make(
                            "bar",
                            id=f"i{i}",
                            label=text(),
                            percent=rnd.choice([None, 0, 33, 100]),
                            message=text(),
                            done=rnd.random() < 0.2,
                        )
                    )
                elif kind == "table":
                    n = rnd.randint(0, 70)
                    areas.append(
                        make(
                            "table",
                            id=f"i{i}",
                            title=text(),
                            columns=[text() for _ in range(min(n, 64))],
                            rows=[
                                [text() for _ in range(rnd.randint(0, n))] for _ in range(rnd.randint(0, 60))
                            ],
                        )
                    )
                else:
                    areas.append(
                        make(
                            "panel",
                            id=f"i{i}",
                            title=text(),
                            items=[[text(), text()] for _ in range(rnd.randint(0, 8))],
                        )
                    )
            cols, rows = rnd.randint(1, 120), rnd.randint(1, 60)
            partial, tail = text() * rnd.randint(0, 3), [text() for _ in range(rnd.randint(0, 3))]
            lines = layout(areas, partial, cols, rows, 3, True, tail)
            with self.subTest(cols=cols, rows=rows, areas=len(areas)):
                self.assertLessEqual(len(lines), max(1, rows - 2))
                for line in lines:
                    self.assertNotIn("\n", line)
                    self.assertLessEqual(width(visible(line)), cols, line)


if __name__ == "__main__":
    unittest.main()
