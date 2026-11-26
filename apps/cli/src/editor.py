"""The inline shell's line editor: a text buffer, how it looks, and the key loop that drives it.

`LineBuffer` and `render` are pure. `LineEditor.readline` is the only part that touches the terminal, through a
`Terminal`: it keeps the prompt on one row (a long line scrolls sideways), so a redraw is "go to column 0,
write the row, clear to the end, put the cursor back".
"""

from __future__ import annotations

import os
from collections.abc import Callable

from src import keys
from src.term import Terminal
from src.textwidth import cut, width
from src.uimsg import sanitize


def _w(c: str) -> int:
    return width(c)


class LineBuffer:
    """Text and a cursor; the cursor counts code points. Combining marks stay with their base character."""

    def __init__(self):
        self.text = ""
        self.cursor = 0

    def insert(self, s: str) -> None:
        self.text = self.text[:self.cursor] + s + self.text[self.cursor:]
        self.cursor += len(s)

    def set(self, text: str) -> None:
        self.text, self.cursor = text, len(text)

    def _prev(self) -> int:
        i = self.cursor
        if i == 0:
            return 0
        i -= 1
        while i > 0 and _w(self.text[i]) == 0:
            i -= 1
        return i

    def _next(self) -> int:
        i, n = self.cursor, len(self.text)
        if i >= n:
            return n
        i += 1
        while i < n and _w(self.text[i]) == 0:
            i += 1
        return i

    def left(self) -> None:
        self.cursor = self._prev()

    def right(self) -> None:
        self.cursor = self._next()

    def home(self) -> None:
        self.cursor = 0

    def end(self) -> None:
        self.cursor = len(self.text)

    def backspace(self) -> None:
        start = self._prev()
        self.text = self.text[:start] + self.text[self.cursor:]
        self.cursor = start

    def delete(self) -> None:
        end = self._next()
        self.text = self.text[:self.cursor] + self.text[end:]

    def kill_end(self) -> None:
        self.text = self.text[:self.cursor]

    def kill_start(self) -> None:
        self.text = self.text[self.cursor:]
        self.cursor = 0

    def kill_word(self) -> None:
        """Delete the word before the cursor (and the spaces between it and the cursor)."""
        i = self.cursor
        while i > 0 and self.text[i - 1].isspace():
            i -= 1
        while i > 0 and not self.text[i - 1].isspace():
            i -= 1
        self.text = self.text[:i] + self.text[self.cursor:]
        self.cursor = i


def render(prompt: str, buf: LineBuffer, cols: int) -> tuple[str, int]:
    """(the row to show, the cursor's column). The row is at most `cols - 1` wide so it never wraps: a long
    line scrolls so that the cursor stays visible."""
    text, cursor = buf.text, buf.cursor
    avail = max(1, cols - width(prompt) - 1)
    start = 0
    if width(text) > avail:
        used = 0
        start = cursor
        while start > 0 and used + _w(text[start - 1]) <= avail - 1:
            used += _w(text[start - 1])
            start -= 1
        while start < cursor and _w(text[start]) == 0:  # not a combining mark without its base
            start += 1
    end, used = start, 0
    while end < len(text) and used + _w(text[end]) <= avail:
        used += _w(text[end])
        end += 1
    return prompt + text[start:end], width(prompt) + width(text[start:cursor])


def _columns(items: list[str], cols: int) -> list[str]:
    """`items` packed into rows no wider than `cols`."""
    rows, row = [], ""
    for item in items:
        if row and width(row) + 2 + width(item) > cols:
            rows.append(row)
            row = ""
        row = f"{row}  {item}" if row else item
    return [cut(r, cols) for r in [*rows, row] if r]


class LineEditor:
    def __init__(self, term: Terminal, history: list[str], complete: Callable[[str, str], list[str]]):
        self.term = term
        history[:] = [sanitize(h) for h in history]  # a history file may hold anything: it is shown and written back
        self.history = history  # oldest first; Enter appends to it
        self.complete = complete  # (line up to the cursor, word before the cursor) -> matches
        self._prompt = ""
        self._buf = LineBuffer()

    # drawing -----------------------------------------------------------------------
    def _redraw(self) -> None:
        cols = self.term.size()[0]
        line, col = render(self._prompt, self._buf, cols)
        self.term.write(f"\r{line}\x1b[K\r" + (f"\x1b[{col}C" if col else ""))

    def _erase(self) -> None:
        self.term.write("\r\x1b[K")

    # reading -----------------------------------------------------------------------
    def readline(self, prompt: str) -> str:
        """One line, edited at `prompt`. EOFError on Ctrl-D at an empty line, KeyboardInterrupt on Ctrl-C."""
        term, buf = self.term, LineBuffer()
        self._prompt, self._buf = prompt, buf
        index, stash = len(self.history), ""
        with term.raw():
            term.on_resume, term.erase_region = self._redraw, self._erase
            try:
                self._redraw()
                while True:
                    key = keys.read_key(term.fd_in, 0.05)
                    if key == "":
                        if term.take_resized():
                            self._redraw()
                        continue
                    if len(key) == 1 and key.isprintable():
                        buf.insert(key)
                    elif key == "enter":
                        buf.end()
                        self._redraw()
                        term.write("\r\n")
                        text = buf.text
                        if text.strip() and (not self.history or self.history[-1] != text):
                            self.history.append(text)
                        return text
                    elif key == "ctrl-c":
                        buf.end()
                        self._redraw()
                        term.write("^C")
                        raise KeyboardInterrupt
                    elif key == "eof" or (key == "ctrl-d" and not buf.text):
                        raise EOFError
                    elif key in ("up", "down"):
                        if key == "up" and index > 0:
                            if index == len(self.history):
                                stash = buf.text
                            index -= 1
                            buf.set(self.history[index])
                        elif key == "down" and index < len(self.history):
                            index += 1
                            buf.set(self.history[index] if index < len(self.history) else stash)
                    elif key == "tab":
                        self._tab()
                    elif key == "ctrl-l":
                        term.write("\x1b[H\x1b[2J")
                    elif key == "ctrl-z":
                        term.suspend()
                    else:
                        self._edit(key)
                    if term.take_resized() or not keys.pending(term.fd_in):
                        self._redraw()
            finally:
                term.on_resume = term.erase_region = None

    def _edit(self, key: str) -> None:
        buf = self._buf
        action = {"left": buf.left, "right": buf.right, "home": buf.home, "ctrl-a": buf.home, "end": buf.end,
                  "ctrl-e": buf.end, "backspace": buf.backspace, "delete": buf.delete, "ctrl-d": buf.delete,
                  "ctrl-k": buf.kill_end, "ctrl-u": buf.kill_start, "ctrl-w": buf.kill_word,
                  "ctrl-b": buf.left, "ctrl-f": buf.right}.get(key)
        if action is not None:
            action()

    def _tab(self) -> None:
        buf = self._buf
        before = buf.text[:buf.cursor]
        word = before[before.rfind(" ") + 1:]
        matches = [sanitize(m) for m in self.complete(before, word)]  # the candidates come from manifests
        if not matches:
            return
        common = os.path.commonprefix(matches)
        if len(matches) == 1:
            insert = matches[0] + " "
        elif len(common) > len(word):
            insert = common
        else:
            rows = _columns(matches, self.term.size()[0])
            self.term.write("\r\n" + "\r\n".join(rows) + "\r\n")
            return
        start = buf.cursor - len(word)
        buf.text = buf.text[:start] + insert + buf.text[buf.cursor:]
        buf.cursor = start + len(insert)
