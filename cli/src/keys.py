"""Keys from a terminal: bytes in, names out.

`read_key` reads one byte at a time with os.read, so what select() reports is exactly what is left unread (the
editor uses that to read a whole paste before it redraws once).

Names: a typed character is itself ("a", "é", " "). Others: "enter", "backspace", "delete", "tab", "left",
"right", "up", "down", "home", "end", "esc", "ctrl-a" .. "ctrl-z", and "eof" at the end of the input. Anything
else (Alt combinations, function keys, bracketed paste markers, bad UTF-8) is dropped.
"""

from __future__ import annotations

import os
import select
import time

ESC_WAIT = 0.05  # seconds to wait for the rest of a sequence after ESC
_CSI_FINAL = {"A": "up", "B": "down", "C": "right", "D": "left", "H": "home", "F": "end"}
_CSI_TILDE = {"1": "home", "7": "home", "4": "end", "8": "end", "3": "delete"}
_CONTROL = {0x09: "tab", 0x0A: "enter", 0x0D: "enter", 0x08: "backspace", 0x7F: "backspace"}


def _ready(fd: int, timeout: float | None) -> bool:
    return bool(select.select([fd], [], [], timeout)[0])


def _byte(fd: int, timeout: float | None) -> bytes | None:
    """One byte; None when none came within `timeout`, b"" at the end of the input."""
    if not _ready(fd, timeout):
        return None
    return os.read(fd, 1)


def pending(fd: int) -> bool:
    """Is a key already waiting to be read?"""
    return _ready(fd, 0)


def _sequence(fd: int) -> str:
    """The key of an escape sequence (the ESC is read); "" when it is unknown or ends early."""
    first = _byte(fd, ESC_WAIT)
    if first is None:
        return "esc"
    if first == b"[":
        params = b""
        while True:
            b = _byte(fd, ESC_WAIT)
            if not b:
                return ""
            if 0x40 <= b[0] <= 0x7E:  # the final byte
                break
            params += b
        final, number = b.decode("latin-1"), params.decode("latin-1").split(";")[0]
        if final == "~":
            return _CSI_TILDE.get(number, "")
        return _CSI_FINAL.get(final, "") if not params.startswith(b"?") else ""
    if first == b"O":
        b = _byte(fd, ESC_WAIT)
        return _CSI_FINAL.get(b.decode("latin-1"), "") if b else ""
    return ""  # Alt+key


def read_key(fd: int, timeout: float | None = None) -> str:
    """The next key; "" when `timeout` seconds pass without one."""
    end = None if timeout is None else time.monotonic() + timeout
    while True:
        left = None if end is None else max(0.0, end - time.monotonic())
        b = _byte(fd, left)
        if b is None:
            return ""
        if not b:
            return "eof"
        code = b[0]
        key = ""
        if code == 0x1B:
            key = _sequence(fd)
        elif code in _CONTROL:
            key = _CONTROL[code]
        elif code < 0x20:
            key = f"ctrl-{chr(code + 0x60)}" if code else ""
        elif code >= 0x80:
            length = next((n for n, lo, hi in ((2, 0xC2, 0xDF), (3, 0xE0, 0xEF), (4, 0xF0, 0xF4)) if lo <= code <= hi), 0)
            rest = b"".join(_byte(fd, ESC_WAIT) or b"" for _ in range(length - 1))
            try:
                key = (b + rest).decode("utf-8") if length else ""
            except UnicodeDecodeError:
                key = ""
        else:
            key = chr(code)
        if key:
            return key
        if end is not None and time.monotonic() >= end:
            return ""
