"""The in and out folders: `--in`/`--out` flags and the [paths] table of config.toml."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from src import isolation
from src.manifest import ManifestError

KNOWN_KEYS = ("in", "out")
_NAME = "paths"


@dataclass(frozen=True)
class Dirs:
    in_dir: Path | None
    out_dir: Path
    in_source: str
    out_source: str
    start_dir: Path | None = None  # the folder dock was started in: the base of every path the user typed


def config(store) -> dict[str, Path]:
    """The folders of [paths] in config.toml, `~` expanded; {} without the table. Fails closed like [container]."""
    path = store.home / "config.toml"
    data = store.config()
    for key in KNOWN_KEYS:
        if key in data:
            raise ManifestError(f"{path}: '{key}' is at the top level; it belongs in a [paths] table")
    table = data.get(_NAME)
    if table is None:
        return {}
    where = f"{path}: [paths]"
    if not isinstance(table, dict):
        raise ManifestError(f"{where} must be a table")
    out: dict[str, Path] = {}
    for key, value in table.items():
        if key not in KNOWN_KEYS:
            raise ManifestError(f"{where} has unknown key '{key}' (known: {', '.join(KNOWN_KEYS)})")
        if not isinstance(value, str) or not value:
            raise ManifestError(f"{where} {key} must be a folder path")
        expanded = os.path.expanduser(value)
        if not os.path.isabs(expanded):
            raise ManifestError(f"{where} {key} '{value}' must be an absolute path or start with ~")
        out[key] = Path(expanded)
    return out


def take_flags(argv: list[str]) -> tuple[str | None, str | None, list[str]]:
    """Pop the leading `--in X` / `--out X` (or `--in=X`); the first other argument ends them, so a plugin's own
    `--out` after the subcommand is left alone."""
    rest = list(argv)
    found: dict[str, str] = {}
    while rest and rest[0].split("=", 1)[0] in ("--in", "--out"):
        flag, eq, value = rest.pop(0).partition("=")
        if not eq:
            if not rest or rest[0].startswith("-"):
                raise ManifestError(f"{flag} needs a folder")
            value = rest.pop(0)
        if not value:
            raise ManifestError(f"{flag} needs a folder")
        if flag in found:
            raise ManifestError(f"{flag} given more than once")
        found[flag] = value
    return found.get("--in"), found.get("--out"), rest


def _real(value: str | Path, cwd: Path) -> Path:
    return Path(os.path.realpath(cwd / value))


def resolve(store, flag_in: str | None, flag_out: str | None, cwd: Path | None = None) -> Dirs:
    """Both folders: flag, else [paths], else the current folder (in: none). The out folder is created and checked."""
    cwd = cwd or Path.cwd()
    try:
        store.config()
    except ManifestError:  # a config that does not parse: the command that needs it reports it, as it always has
        conf = {}
    else:
        conf = config(store)
    if flag_out is not None:
        out, out_source = _real(os.path.expanduser(flag_out), cwd), "--out"
    elif "out" in conf:
        out, out_source = _real(conf["out"], cwd), "[paths] out"
    else:
        out, out_source = _real(cwd, cwd), "current folder"
    if flag_in is not None:
        in_dir, in_source = _real(os.path.expanduser(flag_in), cwd), "--in"
    elif "in" in conf:
        in_dir, in_source = _real(conf["in"], cwd), "[paths] in"
    else:
        in_dir, in_source = None, "current folder"
    # The --in flag is checked now. A [paths] in default is checked when a plugin with fs:read:in launches
    # (isolation.in_folder), so a stale one fails only those plugins, not every command.
    if in_dir is not None and in_source == "--in":
        if not in_dir.is_dir():
            raise ManifestError(f"{in_source} {in_dir}: not an existing folder")
        isolation.check_mount(store, in_dir, in_source, mode="read-only")
    if out_source != "current folder":
        isolation.check_mount(store, out, out_source)  # before anything is created
        try:
            out.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise ManifestError(f"{out_source} {out}: cannot create it: {e.strerror or e}") from e
        isolation.check_mount(store, out, out_source)  # again, now that symlinks on the way are real
    return Dirs(in_dir, out, in_source, out_source, cwd)


def start_of(store) -> Path:
    """The folder dock was started in (before moving into the out folder)."""
    dirs = store.dirs
    return dirs.start_dir if dirs is not None and dirs.start_dir is not None else Path.cwd()


def warn_if_in_unused(store, manifests) -> None:
    """--in / [paths] in was given but no plugin of this run declares fs:read:in: say so once on stderr."""
    dirs = store.dirs
    if dirs is None or dirs.in_dir is None:
        return
    if dirs.in_source != "--in":  # a [paths] default is set once: it must not nag on every run
        return
    if not any("fs:read:in" in m.permissions for m in manifests):
        print(f"dock: {dirs.in_source} ignored: no plugin in this run reads it", file=sys.stderr)
