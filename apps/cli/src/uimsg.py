"""The v2 UI messages a plugin sends (bar, table, panel, clear, form): limits, validation and sanitising.

Everything a plugin says reaches a terminal one day, so each string goes through `sanitize` here, before any
renderer sees it. Each validator returns a normalised copy of the params, or raises Invalid with the reason.
"""

from __future__ import annotations

import math
import re
import unicodedata

from src.process import PluginError

MAX_ID = 64
MAX_AREAS = 32
MAX_COLS = 64
MAX_ROWS = 10_000
MAX_FIELDS = 32
MAX_CHOICES = 256

KINDS = ("bar", "table", "panel")
FIELD_TYPES = ("text", "confirm", "choice")
REPLACEMENT = "�"

# Unicode format characters (category Cf: bidi overrides and isolates, zero-width and invisible marks, tags) and the line
# and paragraph separators (Zl, Zp) are replaced too: they reorder or hide what a user reads, or end a line on some
# terminals. Kept: ZWJ, which glues emoji sequences, and ZWNJ, which Persian and Indic words need. Neither moves or
# hides anything. Variation selectors are marks (Mn), not format characters, so they stay without a rule.
JOINERS = "\u200c\u200d"
_HIDDEN = {"Cf", "Zl", "Zp"}

_SURROGATES = re.compile("[\ud800-\udfff]")
_CONTROL = {c: REPLACEMENT for c in [*range(0x20), 0x7F, *range(0x80, 0xA0)]}
# one translation table per (newlines, tabs): sanitize runs on every string of every update
_TABLES = {}
for _newlines in (False, True):
    for _tabs in (False, True):
        _table = dict(_CONTROL)
        if _newlines:
            del _table[ord("\n")]
        if _tabs:
            _table[ord("\t")] = "    "
        _TABLES[(_newlines, _tabs)] = _table


class Invalid(PluginError):
    """A UI message the host refuses: the plugin gets -32602 (requests), or a line in its log (notifications)."""

    code = -32602

    def __init__(self, reason: str):
        super().__init__(reason, -32602)


def sanitize(text: str, *, newlines: bool = False, tabs: bool = False) -> str:
    """`text` with C0, DEL and C1 controls (ESC included), format characters (but ZWJ and ZWNJ), line and paragraph
    separators replaced by U+FFFD; `\\n` kept when `newlines`, `\\t` shown as 4 spaces when `tabs`."""
    if text.isprintable():  # nothing to replace: the common case, and the cheap one
        return text
    text = _SURROGATES.sub(REPLACEMENT, text.translate(_TABLES[(newlines, tabs)]))  # a lone one can not be encoded
    if text.isascii():
        return text
    return "".join(REPLACEMENT if unicodedata.category(c) in _HIDDEN and c not in JOINERS else c for c in text)


# field checks ----------------------------------------------------------------------
def _obj(p) -> dict:
    if not isinstance(p, dict):
        raise Invalid("params must be an object")
    return p


def _id(p: dict) -> str:
    v = p.get("id")
    if not isinstance(v, str) or not v:
        raise Invalid("id must be a non-empty string")
    if len(v) > MAX_ID:
        raise Invalid(f"id is longer than {MAX_ID} characters")
    return v


def _text(p: dict, key: str, default: str = "") -> str:
    v = p.get(key, default)
    if v is None:
        v = default
    if not isinstance(v, str):
        raise Invalid(f"{key} must be a string")
    return sanitize(v)


def _cell(v, where: str) -> str:
    if isinstance(v, bool) or not isinstance(v, (str, int, float)):
        raise Invalid(f"{where} must hold only strings and numbers")
    return sanitize(v if isinstance(v, str) else str(v))


def _cells(row: list, where: str) -> list[str]:
    """`_cell` over a row; a table of 10,000 rows is checked on every update, so the common case is inlined."""
    out = []
    for v in row:
        kind = v.__class__
        if kind is str and v.isprintable():
            out.append(v)
        elif kind is int or kind is float:  # bool is its own class: still refused by _cell
            out.append(str(v))
        else:
            out.append(_cell(v, where))
    return out


def _bool(p: dict, key: str, default):
    v = p.get(key, default)
    if v is None:
        return default
    if not isinstance(v, bool):
        raise Invalid(f"{key} must be a boolean")
    return v


# messages ---------------------------------------------------------------------------
def bar(p) -> dict:
    p = _obj(p)
    percent = p.get("percent")
    if percent is not None:
        if isinstance(percent, bool) or not isinstance(percent, (int, float)):
            raise Invalid("percent must be a number or null")
        percent = None if math.isnan(percent) else float(min(100.0, max(0.0, percent)))
    return {"id": _id(p), "label": _text(p, "label"), "percent": percent,
            "message": _text(p, "message"), "done": _bool(p, "done", False)}


def table(p) -> dict:
    p = _obj(p)
    i = _id(p)
    columns, rows = p.get("columns"), p.get("rows")
    if not isinstance(columns, list) or not all(isinstance(c, str) for c in columns):
        raise Invalid("columns must be a list of strings")
    if len(columns) > MAX_COLS:
        raise Invalid(f"columns has more than {MAX_COLS} entries")
    if not isinstance(rows, list) or not all(isinstance(r, list) for r in rows):
        raise Invalid("rows must be a list of lists")
    if len(rows) > MAX_ROWS:
        raise Invalid(f"rows has more than {MAX_ROWS} entries")
    return {"id": i, "title": _text(p, "title"), "columns": [sanitize(c) for c in columns],
            "rows": [_cells(r, "rows") for r in rows]}


def panel(p) -> dict:
    p = _obj(p)
    i = _id(p)
    items = p.get("items")
    if not isinstance(items, list) or not all(isinstance(x, (list, tuple)) and len(x) == 2 for x in items):
        raise Invalid("items must be a list of [key, value] pairs")
    return {"id": i, "title": _text(p, "title"),
            "items": [(_cell(k, "items"), _cell(v, "items")) for k, v in items]}


def clear(p) -> dict:
    p = _obj(p)
    i = _id(p)
    kind = p.get("kind")
    if kind not in KINDS:  # `kind in tuple` with an unhashable value is fine: it compares by ==
        raise Invalid(f"kind must be one of {', '.join(KINDS)}")
    return {"id": i, "kind": kind}


def _field(f, seen: set) -> dict:
    if not isinstance(f, dict):
        raise Invalid("fields must be a list of objects")
    name = f.get("name")
    if not isinstance(name, str) or not name:
        raise Invalid("field name must be a non-empty string")
    if name in seen:
        raise Invalid(f"duplicate field name {sanitize(name)!r}")
    seen.add(name)
    label = f.get("label")
    if not isinstance(label, str):
        raise Invalid("field label must be a string")
    kind = f.get("type")
    if kind not in FIELD_TYPES:
        raise Invalid(f"field type must be one of {', '.join(FIELD_TYPES)}")
    # `default` and `choices` are for display; what the plugin gets back is `raw_default` and `values`, as it sent them
    out = {"name": name, "label": sanitize(label), "type": kind, "default": None, "secret": False,
           "choices": None, "values": None, "raw_default": None}
    if kind == "text":
        out["default"] = _text(f, "default") if f.get("default") is not None else None
        out["raw_default"] = f.get("default")
        out["secret"] = _bool(f, "secret", False)
    elif kind == "confirm":
        out["default"] = _bool(f, "default", None)
    else:
        choices = f.get("choices")
        if not isinstance(choices, list) or not choices or not all(isinstance(c, str) for c in choices):
            raise Invalid("choices must be a non-empty list of strings")
        if len(choices) > MAX_CHOICES:
            raise Invalid(f"choices has more than {MAX_CHOICES} entries")
        default = f.get("default")
        if default is not None and default not in choices:
            raise Invalid("default must be one of the choices")
        out["choices"] = [sanitize(c) for c in choices]
        out["values"], out["raw_default"] = list(choices), default
        out["default"] = None if default is None else out["choices"][choices.index(default)]
    return out


def form(p) -> dict:
    p = _obj(p)
    fields = p.get("fields")
    if not isinstance(fields, list):
        raise Invalid("fields must be a list")
    if len(fields) > MAX_FIELDS:
        raise Invalid(f"fields has more than {MAX_FIELDS} entries")
    seen: set = set()
    return {"title": _text(p, "title"), "fields": [_field(f, seen) for f in fields]}
