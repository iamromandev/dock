"""The live areas of one run (bars, tables, panels), and what they look like as plain text."""

from __future__ import annotations

from dataclasses import dataclass

from src import uimsg
from src.textwidth import width

BAR_CELLS = 20


@dataclass
class Area:
    kind: str
    id: str
    data: dict
    done: bool = False


class Areas:
    """Areas by (kind, id), in creation order. Every renderer draws from one of these."""

    def __init__(self):
        self._areas: dict[tuple[str, str], Area] = {}

    def apply(self, kind: str, data: dict) -> Area:
        """Create the area, or replace its contents; returns it. A 33rd area is Invalid."""
        key = (kind, data["id"])
        if key not in self._areas and len(self._areas) >= uimsg.MAX_AREAS:
            raise uimsg.Invalid(f"more than {uimsg.MAX_AREAS} live areas")
        area = self._areas[key] = Area(kind, data["id"], data, bool(data.get("done", False)))
        return area

    def clear(self, kind: str, id: str) -> Area | None:
        """Remove an area and return it; None when there is no such area."""
        return self._areas.pop((kind, id), None)

    def items(self) -> list[Area]:
        return list(self._areas.values())


def _pad(s: str, cols: int) -> str:
    return s + " " * (cols - width(s))


def _grid(rows: list[list[str]]) -> list[str]:
    n = max((len(r) for r in rows), default=0)
    widths = [max((width(r[i]) for r in rows if i < len(r)), default=0) for i in range(n)]
    return ["  ".join(_pad(c, widths[i]) for i, c in enumerate(r)).rstrip() for r in rows]


def plain_lines(area: Area) -> list[str]:
    """The area as text lines, without escape codes or any width limit."""
    d = area.data
    if area.kind == "bar":
        parts = [d["label"]] if d["label"] else []
        percent = d["percent"]
        if percent is None:
            parts.append("done" if area.done else "…")
        else:
            filled = int(percent / 5)
            parts += [f"[{'#' * filled}{'.' * (BAR_CELLS - filled)}]", f"{percent:.1f}%"]
        if d["message"]:
            parts.append(d["message"])
        return ["  ".join(parts)]
    head = [d["title"]] if d["title"] else []
    if area.kind == "table":
        return head + (_grid([d["columns"], *d["rows"]]) if d["columns"] or d["rows"] else [])
    return head + _grid([list(item) for item in d["items"]])
