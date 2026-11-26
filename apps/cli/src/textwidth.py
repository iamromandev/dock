"""How many terminal columns text takes: wide characters 2, combining and zero-width ones 0, the rest 1."""

from __future__ import annotations

import unicodedata


def _char_width(c: str) -> int:
    if unicodedata.combining(c) or unicodedata.category(c) in ("Mn", "Me", "Cf", "Cc"):
        return 0
    return 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1


def width(s: str) -> int:
    return sum(_char_width(c) for c in s)


def cut(s: str, cols: int) -> str:
    """`s` as it is when it fits `cols` columns, else shortened to fit with a trailing "…"."""
    if width(s) <= cols:
        return s
    if cols <= 0:
        return ""
    out, used = [], 0
    for c in s:
        w = _char_width(c)
        if used + w > cols - 1:
            break
        out.append(c)
        used += w
    return "".join(out) + "…"
