"""Permission prompts: what a plugin asks for, and whether the user approves it."""

from __future__ import annotations

import sys
from collections.abc import Callable

from src.manifest import Manifest

ORDER = ("network", "fs:write:cwd")

Grant = tuple[str, str]  # ("perm", permission) or ("env", NAME): tagged, so one kind never stands in for the other


def _tagged(m: Manifest) -> list[Grant]:
    """network, fs:write:cwd, other permissions in manifest order, then env names; no duplicates."""
    first = [p for p in ORDER if p in m.permissions]
    rest = [p for p in m.permissions if p not in ORDER]
    out: list[Grant] = []
    for g in [*(("perm", p) for p in [*first, *rest]), *(("env", n) for n in m.env)]:
        if g not in out:
            out.append(g)
    return out


def _show(grant: Grant) -> str:
    kind, value = grant
    if not value.isprintable():  # a terminal control sequence must not reach the prompt
        value = value.encode("unicode_escape").decode("ascii")
    return value if kind == "perm" else f"env {value}"


def grants(m: Manifest) -> list[str]:
    """Display strings: permissions (network, fs:write:cwd first), then `env <NAME>` per env name."""
    return [_show(g) for g in _tagged(m)]


def added(new: Manifest, old: Manifest | None) -> list[str]:
    """The grants of `new` that `old` does not have; all of them when `old` is None."""
    have = set(_tagged(old)) if old is not None else set()
    return [_show(g) for g in _tagged(new) if g not in have]


def _ask_stdin(question: str) -> str | None:
    print(question, end="", file=sys.stderr, flush=True)
    try:
        answer = sys.stdin.readline()
    except UnicodeDecodeError:  # an answer that is not text is not a yes
        return None
    return answer if answer else None


class Approver:
    def __init__(self, yes: bool, interactive: bool | None = None,
                 ask: Callable[[str], str | None] | None = None):
        self.yes = yes
        self.interactive = (sys.stdin.isatty() and sys.stderr.isatty()) if interactive is None else interactive  # the prompt is on stderr
        self.ask = ask or _ask_stdin

    def decide(self, line: str, question: str) -> str:
        """"yes", "no" (declined) or "refused" (no terminal to ask on). Ctrl-C propagates."""
        if self.yes:
            print(line, file=sys.stderr)
            return "yes"
        if not self.interactive:
            print(f"dock: {line}; rerun with --yes to approve", file=sys.stderr)
            return "refused"
        print(line, file=sys.stderr)
        answer = self.ask(question)
        if answer is None:
            return "no"
        return "yes" if answer.strip().lower() in ("y", "yes") else "no"
