"""The live renderer: what a frame looks like (`layout`), how it is drawn in place (`LiveRegion`), and `LiveUI`.

Everything the terminal receives from here is one of dock's own sequences, never plugin bytes (those are
sanitised before they get here): `\\r`, `ESC[nA`, `ESC[J`, `ESC[?25l/h`, and `ESC[1m` / `ESC[0m` around a panel
title.
"""

from __future__ import annotations

import contextlib
import math
import os
import re
import sys
import threading
import time
from collections.abc import Callable

from src import keys, term, uimsg
from src.areas import BAR_CELLS, Area, Areas, plain_lines
from src.editor import LineBuffer
from src.textwidth import cut, width
from src.ui import PlainUI

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
BOLD, RESET = "\x1b[1m", "\x1b[0m"
_SGR = re.compile(r"\x1b\[[0-9;]*m")
HIDE_CURSOR, SHOW_CURSOR = "\x1b[?25l", "\x1b[?25h"


def _pad(s: str, cols: int) -> str:
    return s + " " * (cols - width(s))


# one area as lines ------------------------------------------------------------------
def _bar_line(area: Area, cols: int, tick: int) -> str:
    d = area.data
    parts = [cut(d["label"], max(1, cols * 30 // 100))] if d["label"] else []
    percent = d["percent"]
    if percent is None:
        parts.append("done" if area.done else SPINNER[tick % len(SPINNER)])
    else:
        cells = BAR_CELLS if cols >= 60 else BAR_CELLS // 2 if cols >= 40 else 0
        if cells:
            filled = int(percent / 100 * cells)
            parts.append(f"[{'#' * filled}{'.' * (cells - filled)}]")
        parts.append(f"{percent:.1f}%")
    line = "  ".join(parts)
    room = cols - width(line) - 2
    if d["message"] and room >= 1:
        line += "  " + cut(d["message"], room)
    return cut(line, cols)


def _fit(widths: list[int], cols: int) -> list[int]:
    """Column widths shrunk, widest first, until the columns and their gaps fit `cols` (or are 1 wide)."""
    spare = cols - 2 * (len(widths) - 1)
    if sum(widths) <= spare:
        return widths
    lo, hi = 1, max(widths)  # the largest cap that fits
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if sum(min(w, mid) for w in widths) <= spare:
            lo = mid
        else:
            hi = mid - 1
    return [min(w, lo) for w in widths]


def _table_size(d: dict, shown: int) -> int:
    trimmed = shown < len(d["rows"])
    return (1 if d["title"] else 0) + (1 if d["columns"] else 0) + shown + (1 if trimmed else 0)


def _table_lines(d: dict, cols: int, shown: int) -> list[str]:
    """Title, header, the first `shown` rows (cut cells), and `… N more rows` when rows are left out."""
    lines = [cut(d["title"], cols)] if d["title"] else []
    grid = ([d["columns"]] if d["columns"] else []) + d["rows"][:shown]
    n = max((len(r) for r in grid), default=0)
    natural = [max((width(r[i]) for r in grid if i < len(r)), default=0) for i in range(n)]
    fitted = _fit(natural, cols) if natural else []
    for r in grid:
        cells = [_pad(cut(c, fitted[i]), fitted[i]) for i, c in enumerate(r)]
        lines.append(cut("  ".join(cells).rstrip(), cols))
    if shown < len(d["rows"]):
        lines.append(cut(f"… {len(d['rows']) - shown} more rows", cols))
    return lines


def _panel_per_row(d: dict, cols: int) -> int:
    return 2 if cols >= 60 and len(d["items"]) > 1 else 1


def _panel_size(d: dict, cols: int) -> int:
    return (1 if d["title"] else 0) + math.ceil(len(d["items"]) / _panel_per_row(d, cols))


def _panel_lines(d: dict, cols: int, color: bool, limit: int) -> list[str]:
    """The first `limit` lines of a panel: a title row, then `│ key  value`, two items to a row when wide."""
    lines = []
    if d["title"]:
        title = cut(f"┌ {d['title']}", cols)
        lines.append(f"{BOLD}{title}{RESET}" if color else title)
    per = _panel_per_row(d, cols)
    items = d["items"][:max(0, limit - len(lines)) * per]
    keys = [max((width(k) for k, _ in items[i::per]), default=0) for i in range(per)]
    cells = [f"{_pad(k, keys[i % per])}  {v}" for i, (k, v) in enumerate(items)]
    left = min(max((width(c) for c in cells[0::2]), default=0), max(1, cols // 2 - 2))
    for r in range(0, len(cells), per):
        row = cells[r:r + per]
        text = _pad(cut(row[0], left), left) + "  " + row[1] if len(row) == 2 else row[0]
        lines.append(cut(("│ " + text).rstrip(), cols))
    return lines[:limit]


class _Group:
    """One area at a level of detail: all of it, a table cut to some rows, or its first line."""

    def __init__(self, area: Area, cols: int):
        self.area, self.cols = area, cols
        self.shown = len(area.data["rows"]) if area.kind == "table" else 0
        self.collapsed = False

    def size(self) -> int:
        if self.collapsed:
            return 1
        d = self.area.data
        if self.area.kind == "bar":
            return 1
        if self.area.kind == "table":
            return _table_size(d, self.shown)
        return _panel_size(d, self.cols)

    def lines(self, tick: int, color: bool) -> list[str]:
        d, cols = self.area.data, self.cols
        if self.area.kind == "bar":
            return [_bar_line(self.area, cols, tick)]
        if self.area.kind == "table":
            lines = _table_lines(d, cols, 0 if self.collapsed else self.shown)
        else:
            lines = _panel_lines(d, cols, color, 1 if self.collapsed else self.size())
        return lines[:1] if self.collapsed else lines


def layout(areas: list[Area], partial: str, cols: int, rows: int, tick: int = 0, color: bool = False,
           tail: list[str] | tuple = ()) -> list[str]:
    """The lines of one frame: the unfinished output line, the areas in creation order, then `tail` (a form).

    A pure function. The frame is at most `rows - 2` lines (at least 1) and every line at most `cols` wide.
    Over that, tables are cut to fewer rows (largest first), then the oldest areas shrink to their first line,
    then the oldest lines are left out, ending with a `… N more` line. The partial line and the tail are
    kept as long as they fit.
    """
    budget = max(1, rows - 2)
    head = [cut(partial, cols)] if partial else []
    foot = [cut(t, cols) for t in tail]
    room = budget - len(head) - len(foot)
    if room <= 0:
        return (head + foot)[-budget:]
    groups = [_Group(a, cols) for a in areas]
    if cols < 20:  # too narrow for detail: one line each
        for g in groups:
            g.collapsed = True

    def total() -> int:
        return sum(g.size() for g in groups)

    while total() > room:  # 1. cut the largest table
        tables = [g for g in groups if g.area.kind == "table" and not g.collapsed and g.shown > 1]
        if not tables:
            break
        g = max(tables, key=lambda t: t.size())
        cut_already = g.shown < len(g.area.data["rows"])
        g.shown = max(1, g.shown - (total() - room) - (0 if cut_already else 1))
    for g in groups:  # 2. the oldest areas become one line
        if total() <= room:
            break
        g.collapsed = True
    body = [line for g in groups for line in g.lines(tick, color)] if total() <= room else None
    if body is None:  # 3. one line each is still too many: the newest fit, with a count of the rest
        keep = [g for g in groups][-(room - 1):] if room > 1 else []
        body = [line for g in keep for line in g.lines(tick, color)]
        body.append(cut(f"… {len(groups) - len(keep)} more", cols))
    return head + body + foot


class LiveRegion:
    """The last few terminal rows, redrawn in place. It knows how many rows it drew, so the next draw climbs back
    over them and clears down; the cursor is hidden while it is drawn."""

    def __init__(self, write: Callable[[str], None], cols: Callable[[], int] | None = None):
        self._write = write
        self._cols = cols                    # the terminal's width now, to know how the old lines were reflowed
        self._rows = 0
        self._last: list[str] | None = None  # the frame to compare the next one with (None: draw it even if equal)
        self._drawn: list[str] = []          # the lines on screen
        self._drawn_cols = 0                 # the width the lines were drawn at
        self._hidden = False

    def _rows_now(self) -> int:
        """The rows the drawn lines take now: when the terminal got narrower it reflowed them, one row per
        `cols` columns (a terminal that does not reflow is climbed over a little too far: only erased)."""
        now = self._cols() if self._cols else 0
        if not self._drawn or not now or now >= self._drawn_cols:
            return self._rows
        return sum(max(1, math.ceil(width(_SGR.sub("", line)) / now)) for line in self._drawn)

    def _clear(self) -> str:
        rows = self._rows_now()
        out = "\r" + (f"\x1b[{rows - 1}A" if rows > 1 else "") + "\x1b[J" if self._rows else ""
        self._rows, self._last, self._drawn = 0, None, []
        return out

    def draw(self, lines: list[str]) -> None:
        if not lines:
            self.erase()
            return
        if lines == self._last:
            return
        out = ("" if self._hidden else HIDE_CURSOR) + (self._clear() or "\r") + "\r\n".join(lines)
        self._hidden = True
        self._rows, self._last, self._drawn = len(lines), list(lines), list(lines)
        self._drawn_cols = self._cols() if self._cols else 0
        self._write(out)

    def erase(self) -> None:
        out = self._clear() + (SHOW_CURSOR if self._hidden else "")
        self._hidden = False
        if out:
            self._write(out)

    def above(self, text: str, write: Callable[[str], None] | None = None) -> None:
        """Write `text` where the region starts (it scrolls up as output); the region is drawn again by the
        next draw. `write` sends the text somewhere else than the terminal's stderr (stdout)."""
        clear = self._clear()
        if clear:
            self._write(clear)
        (write or self._write)(text)

    def invalidate(self) -> None:
        """The next draw writes even an identical frame (the terminal changed size)."""
        self._last = None

    def forget(self) -> None:
        """What was drawn is somewhere the cursor no longer is (Ctrl-Z and `fg`): do not climb over it."""
        self._rows, self._last, self._drawn = 0, None, []


class _FormEnd(Exception):
    """The input of an open form ended (Ctrl-D, or the terminal closed)."""


class LiveUI:
    """The live renderer: a UI (see src.ui.UI) that draws a run's areas on the terminal's stderr.

    Handlers run on the main thread and only change the model (under the terminal lock) and mark it dirty. A
    render thread draws at most once per FRAME, so a plugin sending thousands of updates a second costs one
    redraw per frame, and the last state is always drawn when the updates stop.
    """

    FRAME = 1 / 30   # seconds between two redraws
    POLL = 0.02      # how often an idle renderer looks for a resize or a spinner step
    PARTIAL_MAX = 65_536  # unfinished output past this is moved to scrollback as a line

    def __init__(self, plugin_id: str, verbose: bool, term, out_is_tty: bool | None = None):
        self.plugin_id, self.verbose, self.term = plugin_id, verbose, term
        self.out_is_tty = os.isatty(sys.stdout.fileno()) if out_is_tty is None else out_is_tty
        self.color = not os.environ.get("NO_COLOR")
        self.frames = 0                          # redraws done, for the benchmark test
        self.resize_latency: float | None = None  # seconds from SIGWINCH to the redraw
        self._plain = PlainUI(plugin_id, verbose)  # forms when stdin is not a terminal
        self._areas = Areas()
        self._partial = ""
        self._tail: list[str] = []
        self._region = LiveRegion(term.write, lambda: term.size()[0])
        self._cv = threading.Condition(term.lock)
        self._dirty = False
        self._paused = False   # a question is open: the terminal is its
        self._failed = False   # the render thread failed: nothing is drawn for the rest of the run
        self._halted = False   # the terminal was given back (a signal, Ctrl-Z): nothing is drawn until `fg`
        self._last_tick = -1
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._quiet = None

    # a run -------------------------------------------------------------------------
    def begin(self) -> None:
        with self._cv:
            self._areas, self._partial, self._tail = Areas(), "", []
            self._dirty = self._paused = self._failed = self._halted = False
        self._stop.clear()
        self._quiet = self.term.quiet()
        self._quiet.__enter__()
        self.term.erase_region, self.term.on_resume = self._halt, self._resumed
        self._thread = term.thread(self._run, name="dock-render")

    def end(self) -> None:
        """Stop drawing, take the region away, and write what it showed once, as plain lines."""
        self._stop.set()
        with self._cv:
            self._cv.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        with self._cv:
            self._region.erase()
            lines = [self._partial] if self._partial else []
            lines += [line for area in self._areas.items() for line in plain_lines(area)]
            if lines:
                self.term.write("\n".join(lines) + "\n")
            self._areas, self._partial, self._tail = Areas(), "", []
        if self.term.erase_region == self._halt:
            self.term.erase_region = self.term.on_resume = None
        if self._quiet is not None:
            quiet, self._quiet = self._quiet, None
            quiet.__exit__(None, None, None)
            self.term.flush_input()

    def _halt(self) -> None:
        """The terminal is being given back (Terminal.restore, before Ctrl-Z, a fatal signal, exit): take the region
        away, and stop the render thread drawing it again before the process stops or ends."""
        self._halted = True
        self._region.erase()

    def _resumed(self) -> None:
        """After Ctrl-Z and `fg` (a signal handler: no locks): what was drawn is gone, draw it again below."""
        self._region.forget()
        self._halted = False
        self._dirty = True

    # the render thread ---------------------------------------------------------------
    def _mark_dirty(self) -> None:  # called with the lock held
        if not self._dirty:
            self._dirty = True
            self._cv.notify()

    def _ticking(self) -> bool:
        return any(a.kind == "bar" and a.data["percent"] is None and not a.done for a in self._areas.items())

    def _blocked(self) -> bool:
        return self._paused or self._failed or self._halted

    def _wanted(self) -> bool:
        if self._blocked():
            return False
        spin = self._ticking() and int(time.monotonic() * 10) != self._last_tick
        return self._dirty or self.term.resized or spin

    def _run(self) -> None:
        last = 0.0
        try:
            while not self._stop.is_set():
                with self._cv:
                    while not self._stop.is_set() and not self._wanted():
                        self._cv.wait(self.POLL)
                if self._stop.is_set():
                    return
                delay = last + self.FRAME - time.monotonic()
                if delay > 0 and self._stop.wait(delay):
                    return
                with self._cv:
                    self._frame()
                last = time.monotonic()
        except Exception:  # a failing renderer must not take the run or the terminal with it
            with self._cv:
                self._failed = True  # nothing is drawn any more; end() still writes the final lines
                self._region.erase()

    def _frame(self) -> None:
        """Lay out and draw once. The lock is held."""
        if self._blocked():
            return
        resized = self.term.take_resized()  # before the size is read: a resize in between is seen by the next frame
        cols, rows = self.term.size()
        if resized:
            self._region.invalidate()
            self.resize_latency = None
            started = getattr(self.term, "resized_at", None)
        else:
            started = None
        tick = int(time.monotonic() * 10)
        lines = layout(self._areas.items(), self._partial, cols, rows, tick, self.color, self._tail)
        self._dirty, self._last_tick = False, tick
        self._region.draw(lines)
        self.frames += 1
        if started is not None:
            self.resize_latency = time.monotonic() - started

    # text ----------------------------------------------------------------------------
    def _above(self, text: str, write=None) -> None:
        with self._cv:
            self._region.above(text, write)
            self._mark_dirty()

    @staticmethod
    def _to_stdout(text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()

    def output(self, p: dict) -> None:
        text = uimsg.sanitize(str(p.get("text", "")), newlines=True, tabs=True)
        if not self.out_is_tty:  # redirected: the text goes as it is, the region is not in its way
            self._to_stdout(text)
            return
        with self._cv:
            head, newline, rest = (self._partial + text).rpartition("\n")
            if len(rest) > self.PARTIAL_MAX:
                head, newline, rest = head + newline + rest, "\n", ""
            if newline:
                self._region.above(head + "\n", self._to_stdout)
            self._partial = rest
            self._mark_dirty()

    def log(self, p: dict) -> None:
        level = uimsg.sanitize(str(p.get("level", "info")))
        if level == "debug" and not self.verbose:
            return
        message = uimsg.sanitize(str(p.get("message", "")), newlines=True)
        self._above(f"[{self.plugin_id}] {level}: {message}\n")

    def warn(self, message: str) -> None:
        self._above(f"dock: {uimsg.sanitize(message, newlines=True)}\n")

    # areas ---------------------------------------------------------------------------
    def _apply(self, kind: str, data: dict) -> None:
        with self._cv:
            self._areas.apply(kind, data)
            self._mark_dirty()

    def bar(self, p: dict) -> None:
        self._apply("bar", uimsg.bar(p))

    def table(self, p: dict) -> None:
        self._apply("table", uimsg.table(p))

    def panel(self, p: dict) -> None:
        self._apply("panel", uimsg.panel(p))

    def clear(self, p: dict) -> None:
        data = uimsg.clear(p)
        with self._cv:
            self._areas.clear(data["kind"], data["id"])
            self._mark_dirty()

    # questions -----------------------------------------------------------------------
    @contextlib.contextmanager
    def _drawing_paused(self):
        """No region while something else uses the terminal; it is drawn again afterwards."""
        with self._cv:
            self._paused = True
            self._region.erase()
        try:
            yield
        finally:
            with self._cv:
                self._paused = False
                self._mark_dirty()

    def prompt_user(self, p: dict) -> dict:
        prompt = uimsg.sanitize(str(p.get("prompt", "> ")))
        with self._drawing_paused(), self.term.cooked():
            self.term.flush_input()  # what was typed while the command ran is not the answer
            try:
                return {"text": input(prompt)}
            except EOFError:
                return {"eof": True}

    def form(self, p: dict) -> dict:
        data = uimsg.form(p)
        if not sys.stdin.isatty() or self._failed:
            # No keys to read, or nothing is drawn any more (the renderer failed): numbered prompts on stderr and
            # answers as lines from stdin, with the terminal's own line editing and echo so the user sees what is typed.
            with self._drawing_paused(), self.term.cooked():
                self.term.flush_input()
                return self._plain.form(p)
        values = {}
        if data["title"]:
            self._above(data["title"] + "\n")
        try:
            with self.term.raw():
                self.term.flush_input()  # what was typed while the command ran is not an answer
                for field in data["fields"]:
                    values[field["name"]] = self._ask(field)
        except _FormEnd:
            return {"eof": True}
        finally:
            self._set_tail([])
        return {"values": values}

    def _set_tail(self, lines: list[str]) -> None:
        """Show these lines below the areas, drawn at once: a key is echoed without waiting for a frame."""
        with self._cv:
            self._tail = lines
            self._dirty = True
            if self._failed:  # the layout is what failed: draw only the question, so the user does not type blind
                cols = self.term.size()[0]
                self._region.draw([cut(t, cols) for t in lines])
            else:
                self._frame()

    def _ask(self, field: dict):
        kind, label, default = field["type"], field["label"], field["default"]
        key_fd = self.term.fd_in
        if kind == "confirm":
            hint = {None: "(y/n)", True: "(Y/n)", False: "(y/N)"}[default]
            self._set_tail([f"{label} {hint} "])
            while True:
                key = self._key(key_fd)
                if key in ("y", "Y"):
                    answer = True
                elif key in ("n", "N"):
                    answer = False
                elif key == "enter" and default is not None:
                    answer = default
                else:
                    continue
                break
            self._set_tail([])
            self._above(f"{label} {hint} {'y' if answer else 'n'}\n")
            return answer
        if kind == "choice":
            choices = field["choices"]
            current = choices.index(default) if default in choices else 0
            while True:
                self._set_tail(self._choice_lines(label, choices, current))
                key = self._key(key_fd)
                if key == "up":
                    current = max(0, current - 1)
                elif key == "down":
                    current = min(len(choices) - 1, current + 1)
                elif key == "enter":
                    break
            self._set_tail([])
            self._above(f"{label} {choices[current]}\n")
            return field["values"][current]
        buf = LineBuffer()
        secret = field["secret"]
        hint = f" [{default}]" if default and not secret else ""
        while True:
            self._set_tail([f"{label}{hint} " + ("" if secret else buf.text)])
            key = self._key(key_fd)
            if len(key) == 1 and key.isprintable():
                buf.insert(key)
            elif key == "backspace":
                buf.backspace()
            elif key == "ctrl-u":
                buf.kill_start()
            elif key == "ctrl-w":
                buf.kill_word()
            elif key == "enter":
                break
        self._set_tail([])
        self._above(f"{label}{hint} " + ("" if secret else buf.text) + "\n")
        return buf.text or field["raw_default"] or ""

    def _choice_lines(self, label: str, choices: list[str], current: int) -> list[str]:
        """The question and a window of the options that fits the terminal, `›` on the current one."""
        visible = max(1, min(len(choices), self.term.size()[1] - 4))
        start = max(0, min(current - visible // 2, len(choices) - visible))
        marks = (f"  {'›' if i == current else ' '} {choices[i]}" for i in range(start, start + visible))
        return [label, *marks]

    def _key(self, fd: int) -> str:
        """The next key of a form; Ctrl-C cancels it as it does prompt_user, Ctrl-D ends the input."""
        while True:
            key = keys.read_key(fd, 0.1)
            if key == "ctrl-c":
                raise KeyboardInterrupt
            if key in ("eof", "ctrl-d"):
                raise _FormEnd
            if key == "ctrl-z":
                self.term.suspend()
            elif key:
                return key

    def handlers(self) -> dict:
        return {
            "output": self.output, "log": self.log, "bar": self.bar, "table": self.table, "panel": self.panel,
            "clear": self.clear, "prompt_user": self.prompt_user, "form": self.form,
        }
