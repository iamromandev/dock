"""Choosing files of a torrent: parsing --only/--skip picks, resolving them to file indexes, listing, pieces.

A pick is a number, a range `a-b` (1-based, inclusive) or a glob matched case-sensitively against the whole
path inside the item (`/` separated, after dm's name cleaning; `*` matches across `/`).
"""

import fnmatch
import re
from dataclasses import dataclass

from ..fmt import human_size
from ..target import DmError

_NUMBER = re.compile(r"[0-9]+")
_RANGE = re.compile(r"([0-9]*)-([0-9]*)")
# a torrent never has that many files; longer groups would also make int() fail past 4300 digits
_MAX_DIGITS = 9


@dataclass(frozen=True)
class Number:
    lo: int
    hi: int  # equal to lo for a single number


@dataclass(frozen=True)
class Glob:
    text: str
    regex: re.Pattern


@dataclass(frozen=True)
class Picks:
    only: tuple | None  # None: every file
    skip: tuple


def _bad(text):
    return DmError(f"bad pick '{text}'")


def _split(text):
    """Split on commas outside a `[...]` set, so `a[,]b` is one glob (an unclosed `[` is a plain
    character)."""
    out, start, i = [], 0, 0
    while i < len(text):
        c = text[i]
        if c == "[":
            j = i + 1
            if j < len(text) and text[j] == "!":
                j += 1
            if j < len(text) and text[j] == "]":
                j += 1
            j = text.find("]", j)
            if j != -1:
                i = j
        elif c == ",":
            out.append(text[start:i])
            start = i + 1
        i += 1
    out.append(text[start:])
    return out


def _items(text):
    out = []
    for raw in _split(text):
        item = raw.strip()
        if not item:
            raise _bad(item)
        out.append(_pick(item))
    return tuple(out)


def _pick(item):
    if _NUMBER.fullmatch(item):
        if len(item) > _MAX_DIGITS:
            raise _bad(item)
        n = int(item)
        if n < 1:
            raise _bad(item)
        return Number(n, n)
    m = _RANGE.fullmatch(item)
    if m:  # digits around a dash and nothing else; a dash between names is a glob
        if not m.group(1) or not m.group(2) or max(map(len, m.groups())) > _MAX_DIGITS:
            raise _bad(item)
        lo, hi = int(m.group(1)), int(m.group(2))
        if lo < 1 or lo > hi:
            raise _bad(item)
        return Number(lo, hi)
    return Glob(item, re.compile(fnmatch.translate(item)))


def parse(only, skip):
    """Picks for the two flag values, or None when neither is given. DmError on a bad pick."""
    if only is None and skip is None:
        return None
    return Picks(_items(only) if only is not None else None, _items(skip) if skip is not None else ())


def paths(specs):
    """The path of each file inside the item, `/` separated (a single-file torrent: its cleaned name)."""
    return ["/".join(f.parts) for f in specs]


def _match(specs, names, pick):
    if isinstance(pick, Number):
        n = len(specs)
        for k in (pick.lo, pick.hi):
            if k > n:
                raise DmError(f"file number {k} is out of range (1-{n})")
        return range(pick.lo - 1, pick.hi)
    found = [i for i, name in enumerate(names) if pick.regex.fullmatch(name)]
    if not found:
        raise DmError(f"no file matches '{pick.text}'")
    return found


def resolve(specs, picks):
    """The indexes of the picked files. Every pick must match something and the result must not be empty."""
    if picks is None:
        return frozenset(range(len(specs)))
    names = paths(specs) if any(isinstance(p, Glob) for p in (*(picks.only or ()), *picks.skip)) else None
    chosen = set(range(len(specs)))
    if picks.only is not None:
        chosen = set()
        for pick in picks.only:
            chosen.update(_match(specs, names, pick))
    for pick in picks.skip:
        chosen.difference_update(_match(specs, names, pick))
    if not chosen:
        raise DmError("no file selected")
    return frozenset(chosen)


def list_lines(specs, chosen=None):
    """The `--list` output: a header, one line per (chosen) file with its number in the whole torrent, and
    a total."""
    names = paths(specs)
    width = max(1, len(str(len(specs))))
    lines = [f"{'#':>{width}}  {'size':>9}  path"]
    count = total = 0
    for i, f in enumerate(specs):
        if chosen is not None and i not in chosen:
            continue
        lines.append(f"{i + 1:>{width}}  {human_size(f.length):>9}  {names[i]}")
        count += 1
        total += f.length
    lines.append(f"{count} file{'s' if count != 1 else ''}, {human_size(total)}")
    return lines


def wanted_pieces(specs, chosen, piece_length):
    """The pieces that hold a byte of a chosen file (empty files hold none)."""
    out = set()
    for i in chosen:
        f = specs[i]
        if f.length > 0:
            out.update(range(f.offset // piece_length, (f.offset + f.length - 1) // piece_length + 1))
    return frozenset(out)
