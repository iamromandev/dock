"""Permission prompts: what a plugin asks for, and whether the user approves it."""

from __future__ import annotations

import os
from collections.abc import Callable

from dock_host.manifest import Manifest
from dock_host.notice import default_notice

ORDER = ("network", "fs:write:cwd", "fs:read:in")

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


def _folder(path) -> str:
    """A folder for a prompt: `~/…` when it is under the home directory."""
    home = os.path.expanduser("~").rstrip(os.sep)
    text = str(path)
    if home and (text == home or text.startswith(home + os.sep)):  # a home of "/" or "" would hide every path
        text = "~" + text[len(home):]
    return text if text.isprintable() else text.encode("unicode_escape").decode("ascii")


def _show(grant: Grant, dirs=None, writes: bool = False) -> str:
    """`dirs` names the out and in folders: only run-time prompts pass it (an install is folder-independent).

    `writes` says the plugin also has fs:write:cwd: its in folder is then mounted read-write when it is the out folder."""
    kind, value = grant
    if (grant == ("perm", "fs:write:cwd") and dirs is not None
            and dirs.out_source != "current folder"):  # name the folder only when --out / [paths] moved it
        return f"{value} → {_folder(dirs.out_dir)}"
    if grant == ("perm", "fs:read:in") and dirs is not None:
        folder = dirs.in_dir if dirs.in_dir is not None else dirs.out_dir
        mode = "same folder as fs:write:cwd, read-write" if writes and folder == dirs.out_dir else "read-only"
        return f"{value} → {_folder(folder)} ({mode})"
    if not value.isprintable():  # a terminal control sequence must not reach the prompt
        value = value.encode("unicode_escape").decode("ascii")
    return value if kind == "perm" else f"env {value}"


def grants(m: Manifest, dirs=None) -> list[str]:
    """Display strings: permissions (network, fs:write:cwd first), then `env <NAME>` per env name.

    With `dirs` (paths.Dirs) set by --out or [paths], fs:write:cwd shows the folder it will write to."""
    writes = "fs:write:cwd" in m.permissions
    return [_show(g, dirs, writes) for g in _tagged(m)]


def added(new: Manifest, old: Manifest | None) -> list[str]:
    """The grants of `new` that `old` does not have; all of them when `old` is None."""
    have = set(_tagged(old)) if old is not None else set()
    return [_show(g) for g in _tagged(new) if g not in have]


class Approver:
    def __init__(self, yes: bool, interactive: bool, ask: Callable[[str], str | None],
                 notice: Callable[[str], None] | None = None):
        self.yes = yes
        self.interactive = interactive  # whether there is a terminal to ask on: the caller decides
        self.ask = ask
        self.notice = notice or default_notice

    def decide(self, line: str, question: str) -> str:
        """"yes", "no" (declined) or "refused" (no terminal to ask on). Ctrl-C propagates."""
        if self.yes:
            self.notice(line)
            return "yes"
        if not self.interactive:
            self.notice(f"dock: {line}; rerun with --yes to approve")
            return "refused"
        self.notice(line)
        answer = self.ask(question)
        if answer is None:
            return "no"
        return "yes" if answer.strip().lower() in ("y", "yes") else "no"
