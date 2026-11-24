"""The host owns the terminal. Plugins ask it to print, log, show bars, tables and panels, or ask the user."""

from __future__ import annotations

import getpass
import os
import sys
from typing import Protocol

from cli import uimsg
from cli.areas import Areas, plain_lines


class UI(Protocol):
    """What a Session needs from whoever shows a plugin's messages and asks its questions (PlainUI)."""

    def begin(self) -> None: ...

    def end(self) -> None: ...

    def output(self, p: dict) -> None: ...

    def log(self, p: dict) -> None: ...

    def bar(self, p: dict) -> None: ...

    def table(self, p: dict) -> None: ...

    def panel(self, p: dict) -> None: ...

    def clear(self, p: dict) -> None: ...

    def prompt_user(self, p: dict) -> dict: ...

    def form(self, p: dict) -> dict: ...

    def warn(self, message: str) -> None: ...

    def handlers(self) -> dict: ...


FORM_TRIES = 3  # answers a plain form takes per field before it gives up


def _parse_answer(field: dict, line: str) -> tuple[bool, object]:
    """(valid, value) for one answer line to a field; an empty line takes the default, where there is one."""
    kind, default, answer = field["type"], field["default"], line.strip()
    if kind == "text":
        return True, (field["raw_default"] or "") if line == "" else line
    if answer == "":
        return (default is not None), (field["raw_default"] if kind == "choice" else default)
    if kind == "confirm":
        yes_no = {"y": True, "yes": True, "n": False, "no": False}.get(answer.lower())
        return (yes_no is not None), yes_no
    choices, values = field["choices"], field["values"]  # shown as `choices`, answered with `values`
    if answer in choices:
        return True, values[choices.index(answer)]
    if answer.isascii() and answer.isdigit() and 1 <= int(answer) <= len(choices):
        return True, values[int(answer) - 1]
    return False, None


class PlainUI:
    """Text only, no escape codes: a bar prints when it is done, tables and panels when they are cleared or the run ends."""

    def __init__(self, plugin_id: str, verbose: bool = False):
        self.plugin_id = plugin_id
        self.verbose = verbose
        self._areas = Areas()
        self._printed: set[str] = set()  # ids of bars whose final line is already out

    def begin(self) -> None:
        self._reset()

    def end(self) -> None:
        for area in self._areas.items():
            if not (area.kind == "bar" and area.id in self._printed):
                self._print(area)
        self._reset()

    def _reset(self) -> None:
        self._areas = Areas()
        self._printed = set()

    @staticmethod
    def _print(area) -> None:
        for line in plain_lines(area):
            print(line, file=sys.stderr)

    def output(self, p: dict) -> None:
        sys.stdout.write(uimsg.sanitize(str(p.get("text", "")), newlines=True, tabs=True))
        sys.stdout.flush()

    def log(self, p: dict) -> None:
        level = uimsg.sanitize(str(p.get("level", "info")))
        if level == "debug" and not self.verbose:
            return
        message = uimsg.sanitize(str(p.get("message", "")), newlines=True)
        print(f"[{self.plugin_id}] {level}: {message}", file=sys.stderr)

    def bar(self, p: dict) -> None:
        data = uimsg.bar(p)
        area = self._areas.apply("bar", data)
        if data["done"]:
            self._print(area)
            self._printed.add(data["id"])
        else:
            self._printed.discard(data["id"])

    def table(self, p: dict) -> None:
        self._areas.apply("table", uimsg.table(p))

    def panel(self, p: dict) -> None:
        self._areas.apply("panel", uimsg.panel(p))

    def clear(self, p: dict) -> None:
        data = uimsg.clear(p)
        area = self._areas.clear(data["kind"], data["id"])
        if data["kind"] == "bar":
            self._printed.discard(data["id"])
        elif area is not None:
            self._print(area)

    def prompt_user(self, p: dict) -> dict:
        try:
            return {"text": input(uimsg.sanitize(str(p.get("prompt", "> "))))}
        except EOFError:
            return {"eof": True}

    def form(self, p: dict) -> dict:
        """Ask each field on stderr and read the answers from stdin, one line each (`{"eof": True}` when input ends)."""
        data = uimsg.form(p)
        if data["title"]:
            print(data["title"], file=sys.stderr)
        values = {}
        for field in data["fields"]:
            for _ in range(FORM_TRIES):
                line = self._read_answer(field)
                if line is None:
                    return {"eof": True}
                ok, value = _parse_answer(field, line)
                if ok:
                    values[field["name"]] = value
                    break
            else:
                raise uimsg.Invalid(f"no valid answer for {field['name']}")
        return {"values": values}

    @staticmethod
    def _read_answer(field: dict) -> str | None:
        """One line for the field, without its newline; None at the end of input."""
        kind, default, label = field["type"], field["default"], field["label"]
        if kind == "choice":
            print(label, file=sys.stderr)
            for i, c in enumerate(field["choices"], 1):
                print(f"  {i}) {c}", file=sys.stderr)
            hint = f" [{default}]" if default is not None else ""
            prompt = f"Choose 1-{len(field['choices'])}{hint}: "
        elif kind == "confirm":
            hint = {None: "(y/n)", True: "(Y/n)", False: "(y/N)"}[default]
            prompt = f"{label} {hint} "
        else:
            hint = f" [{default}]" if default and not field["secret"] else ""
            prompt = f"{label}{hint} "
        if kind == "text" and field["secret"] and sys.stdin.isatty():
            try:
                return getpass.getpass(prompt, stream=sys.stderr)
            except EOFError:
                return None
        sys.stderr.write(prompt)
        sys.stderr.flush()
        line = sys.stdin.readline()
        return line.rstrip("\r\n") if line else None

    def warn(self, message: str) -> None:
        print(f"dock: {uimsg.sanitize(message, newlines=True)}", file=sys.stderr)

    def handlers(self) -> dict:
        return {
            "output": self.output,
            "log": self.log,
            "bar": self.bar,
            "table": self.table,
            "panel": self.panel,
            "clear": self.clear,
            "prompt_user": self.prompt_user,
            "form": self.form,
        }


def live_mode() -> bool:
    """Draw live areas only when stderr is a terminal that understands escape codes, and the user did not opt out."""
    try:
        import termios  # noqa: F401 - no termios (Windows): no live mode
    except ImportError:
        return False
    return (sys.stderr.isatty() and os.environ.get("TERM", "") not in ("", "dumb")
            and not os.environ.get("DOCK_PLAIN"))


def make_ui(plugin_id: str, verbose: bool = False, term=None) -> UI:
    """The renderer for one plugin: the live one on the given Terminal, plain text without one."""
    if term is None:
        return PlainUI(plugin_id, verbose)
    from cli.live import LiveUI  # lazy: live imports this module

    return LiveUI(plugin_id, verbose, term)
