"""A small terminal emulator for tests: feed it the bytes dock wrote to a pty, read back the screen.

It understands what dock may legitimately send: printable text (wide characters take 2 cells, combining marks
none), CR, LF, BS, CUU/CUD/CUF/CUB (`ESC [ n A/B/C/D`), CHA (`G`), EL (`K`), ED (`J`), CUP (`H`), SGR (`m`,
ignored) and `?25h/l`. It does not reflow on resize. Deferred autowrap is modelled, as in xterm: a character in
the last column leaves the cursor there until the next printable character.

Everything else is recorded, not interpreted: `seqs` holds every escape sequence seen (as text), and `controls`
every other control character, so a test can compare them to a whitelist.
"""

from __future__ import annotations

import re
import unicodedata


def _width(c: str) -> int:
    if unicodedata.combining(c) or unicodedata.category(c) in ("Mn", "Me", "Cf", "Cc"):
        return 0
    return 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1


_CSI = re.compile(r"\x1b\[([0-?]*)([ -/]*)([@-~])")


class Screen:
    def __init__(self, cols: int = 80, rows: int = 24):
        self.cols, self.rows = cols, rows
        self.grid: list[list[str]] = [[" "] * cols for _ in range(rows)]
        self.scrollback: list[str] = []
        self.row = self.col = 0
        self.wrap_pending = False
        self.cursor_visible = True
        self.seqs: list[str] = []
        self.controls: list[str] = []
        self._pending = ""  # an incomplete escape sequence at the end of the last chunk
        self.max_row_seen = 0

    # reading ---------------------------------------------------------------------
    def lines(self) -> list[str]:
        """The screen rows without trailing blanks."""
        return ["".join(r).rstrip() for r in self.grid]

    def text(self) -> str:
        """Scrollback and screen as lines, trailing blank screen rows dropped."""
        screen = self.lines()
        while screen and not screen[-1]:
            screen.pop()
        return "\n".join(self.scrollback + screen)

    # feeding ---------------------------------------------------------------------
    def feed(self, data: bytes | str) -> None:
        text = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
        text, self._pending = self._pending + text, ""
        i = 0
        while i < len(text):
            c = text[i]
            if c == "\x1b":
                m = _CSI.match(text, i)
                if m:
                    self._csi(m)
                    i = m.end()
                    continue
                rest = text[i:]
                if re.fullmatch(r"\x1b(\[[0-?]*[ -/]*)?", rest):  # may be completed by the next chunk
                    self._pending = rest
                    return
                self.seqs.append(text[i:i + 2])
                i += 2
                continue
            i += 1
            if c == "\r":
                self.col, self.wrap_pending = 0, False
            elif c == "\n":
                self._linefeed()
            elif c == "\b":
                self.col, self.wrap_pending = max(0, self.col - 1), False
            elif ord(c) < 0x20 or 0x7F <= ord(c) < 0xA0:
                self.controls.append(c)
            else:
                self._put(c)

    def _linefeed(self) -> None:
        if self.row == self.rows - 1:
            self.scrollback.append("".join(self.grid.pop(0)).rstrip())
            self.grid.append([" "] * self.cols)
        else:
            self.row += 1

    def _put(self, c: str) -> None:
        w = _width(c)
        if w == 0:
            return
        if self.wrap_pending or self.col + w > self.cols:
            self.col, self.wrap_pending = 0, False
            self._linefeed()
        self.grid[self.row][self.col] = c
        if w == 2 and self.col + 1 < self.cols:
            self.grid[self.row][self.col + 1] = ""  # covered by the wide character
        self.col += w
        if self.col >= self.cols:
            self.col, self.wrap_pending = self.cols - 1, True
        self.max_row_seen = max(self.max_row_seen, self.row)

    def _csi(self, m: re.Match) -> None:
        params, inter, final = m.group(1), m.group(2), m.group(3)
        self.seqs.append(m.group(0))
        nums = [int(p) if p.isdigit() else 0 for p in params.replace("?", "").split(";")] if params else []
        n = nums[0] if nums and nums[0] else 1
        if params.startswith("?") and final in "hl" and params == "?25":
            self.cursor_visible = final == "h"
        elif params.startswith("?") or inter:
            pass
        elif final == "A":
            self.row, self.wrap_pending = max(0, self.row - n), False
        elif final == "B":
            self.row, self.wrap_pending = min(self.rows - 1, self.row + n), False
        elif final == "C":
            self.col, self.wrap_pending = min(self.cols - 1, self.col + n), False
        elif final == "D":
            self.col, self.wrap_pending = max(0, self.col - n), False
        elif final == "G":
            self.col, self.wrap_pending = min(self.cols - 1, n - 1), False
        elif final == "H":
            row = nums[0] if nums and nums[0] else 1
            col = nums[1] if len(nums) > 1 and nums[1] else 1
            self.row, self.col, self.wrap_pending = min(self.rows, row) - 1, min(self.cols, col) - 1, False
        elif final == "K":
            mode = nums[0] if nums else 0
            lo, hi = {0: (self.col, len(self.grid[self.row])), 1: (0, self.col + 1),
                      2: (0, len(self.grid[self.row]))}.get(mode, (0, 0))
            for x in range(lo, hi):
                self.grid[self.row][x] = " "
        elif final == "J":
            mode = nums[0] if nums else 0
            if mode == 0:
                for x in range(self.col, len(self.grid[self.row])):
                    self.grid[self.row][x] = " "
                for y in range(self.row + 1, self.rows):
                    self.grid[y] = [" "] * self.cols
            elif mode in (2, 3):
                for y in range(self.rows):
                    self.grid[y] = [" "] * self.cols
            elif mode == 1:
                for y in range(self.row):
                    self.grid[y] = [" "] * self.cols
                for x in range(self.col + 1):
                    self.grid[self.row][x] = " "
        # SGR (m) and anything else: recorded in seqs only
