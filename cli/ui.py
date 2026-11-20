"""The host owns the terminal. Plugins ask it to print, log, show progress, or ask the user."""

from __future__ import annotations

import sys
from typing import Protocol


class UI(Protocol):
    """What a Session needs from whoever shows a plugin's messages and asks its questions: the terminal (TerminalUI)."""

    def output(self, p: dict) -> None: ...

    def log(self, p: dict) -> None: ...

    def progress(self, p: dict) -> None: ...

    def prompt_user(self, p: dict) -> dict: ...

    def warn(self, message: str) -> None: ...

    def handlers(self) -> dict: ...


class TerminalUI:
    def __init__(self, plugin_id: str, verbose: bool = False):
        self.plugin_id = plugin_id
        self.verbose = verbose
        self._progress_shown = False

    def _clear_progress(self) -> None:
        if self._progress_shown:
            sys.stderr.write("\r\033[K")
            self._progress_shown = False

    def output(self, p: dict) -> None:
        self._clear_progress()
        sys.stdout.write(p.get("text", ""))
        sys.stdout.flush()

    def log(self, p: dict) -> None:
        level = p.get("level", "info")
        if level == "debug" and not self.verbose:
            return
        self._clear_progress()
        print(f"[{self.plugin_id}] {level}: {p.get('message', '')}", file=sys.stderr)

    def progress(self, p: dict) -> None:
        if not sys.stderr.isatty():
            return
        pct, message = p.get("percent"), p.get("message", "")
        if pct is None:
            line = f"  … {message}"
        else:
            filled = int(pct / 5)
            line = f"  [{'#' * filled}{'.' * (20 - filled)}] {pct:5.1f}% {message}"
        sys.stderr.write("\r\033[K" + line)
        sys.stderr.flush()
        self._progress_shown = True

    def prompt_user(self, p: dict) -> dict:
        self._clear_progress()
        try:
            return {"text": input(p.get("prompt", "> "))}
        except EOFError:
            return {"eof": True}

    def warn(self, message: str) -> None:
        self._clear_progress()
        print(f"dock: {message}", file=sys.stderr)

    def handlers(self) -> dict:
        return {
            "output": self.output,
            "log": self.log,
            "progress": self.progress,
            "prompt_user": self.prompt_user,
        }
