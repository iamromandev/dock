"""Plugins that are not installed: local catalog folders listed in config.toml, read without running anything.

  [catalog] paths = ["~/plugins", "/opt/dock-plugins"]

Only plugin.toml files are read (through Manifest.load); nothing is built, started or copied. This module takes
the Store as an argument and never imports dock_host.store (which imports this module).
"""

from __future__ import annotations

import difflib
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from dock_host.manifest import Manifest, ManifestError

CONFIG_KEYS = ("paths",)
_NAME = "catalog"


def _warner(store) -> Callable[[str], None]:
    """The default warning: the store's notice, with the `dock: ` prefix."""
    return lambda msg: store.notice(f"dock: {msg}")


def paths(store) -> list[Path]:
    """The folders of [catalog] paths, `~` expanded; [] without a [catalog] table. Fails closed like [container]."""
    path = store.home / "config.toml"
    data = store.config()
    if "paths" in data and not isinstance(data["paths"], dict):  # a [paths] table is the in/out folders
        raise ManifestError(f"{path}: 'paths' is at the top level; catalog settings belong in a [catalog] table")
    for key in data:
        if key != _NAME and difflib.get_close_matches(key.lower(), [_NAME], n=1, cutoff=0.75):
            raise ManifestError(f"{path}: [{key}] is not a known table; did you mean [catalog]?")
    table = data.get(_NAME)
    if table is None:
        return []
    where = f"{path}: [catalog]"
    if not isinstance(table, dict):
        raise ManifestError(f"{where} must be a table")
    for key in table:
        if key not in CONFIG_KEYS:
            raise ManifestError(f"{where} has unknown key '{key}' (known: {', '.join(CONFIG_KEYS)})")
    entries = table.get("paths", [])
    if not isinstance(entries, list) or not all(isinstance(e, str) and e for e in entries):
        raise ManifestError(f"{where} paths must be a list of folder paths")
    folders = []
    for entry in entries:
        expanded = os.path.expanduser(entry)
        if not os.path.isabs(expanded):
            raise ManifestError(f"{where} paths entry '{entry}' must be an absolute path or start with ~")
        folders.append(Path(expanded))
    return folders


@dataclass(frozen=True)
class Available:
    """A catalog plugin: its manifest and the folder it was read from."""
    manifest: Manifest
    folder: Path  # resolved real path


def _candidates(entry: Path) -> list[Path]:
    """The plugin folders of one catalog entry: itself, or its immediate subfolders that hold a plugin.toml."""
    if (entry / "plugin.toml").is_file():
        return [entry]
    return sorted(d for d in entry.iterdir() if d.is_dir() and (d / "plugin.toml").is_file())


def builtin_dir(here: Path | None = None) -> Path | None:
    """<repo>/plugins of the checkout dock runs from; None when there is none (a packaged install).

    The checkout is recognised by packages/host/dock_host/catalog.py being this very file, so an unrelated
    <three folders up>/plugins is not trusted. `here` is this file's path (tests give another)."""
    here = here or Path(__file__).resolve()
    repo = here.parents[3]
    marker = repo / "packages" / "host" / "dock_host" / "catalog.py"
    if not marker.is_file() or marker.resolve() != here:
        return None
    folder = repo / "plugins"
    return folder if folder.is_dir() else None


def load(store, warn: Callable[[str], None] | None = None) -> list[Available]:
    """Every readable plugin of the catalog, in scan order; one warning per entry or plugin that is skipped.

    The [catalog] paths come first, then the built-in plugins folder (silently skipped when missing)."""
    warn = warn or _warner(store)
    found: list[Available] = []
    seen: set[Path] = set()
    entries = paths(store)
    builtin = builtin_dir()
    if builtin is not None and builtin.is_dir():
        entries.append(builtin)
    for entry in entries:
        is_builtin = entry == builtin
        if not entry.exists():
            warn(f"catalog folder {entry} not found, skipped")
            continue
        try:
            if not entry.is_dir():
                raise NotADirectoryError(f"{entry} is not a folder")
            candidates = _candidates(entry)
        except OSError as e:
            warn(f"catalog folder {entry} cannot be read ({e}), skipped")
            continue
        for candidate in candidates:
            real = Path(os.path.realpath(candidate))
            if real in seen:
                continue
            seen.add(real)
            try:
                m = Manifest.load(real)
                if is_builtin and any(a.manifest.id == m.id for a in found):
                    continue  # a [catalog] copy of the same id wins
                found.append(Available(m, real))
            except (ManifestError, OSError) as e:  # ManifestError includes NotAContainerPlugin
                warn(f"catalog plugin {real} skipped: {e}")
    return found


@dataclass(frozen=True)
class Match:
    name: str  # command name
    summary: str
    keywords: tuple[str, ...]
    plugin: str  # plugin id
    version: str
    installed: bool
    folder: Path | None  # catalog folder of a not-installed match; None when installed
    catalog_version: str | None  # installed, and the catalog holds other version(s): "0.2.0" or "0.2.0, 0.3.0"
    install: str | None  # not installed: the command that installs it
    installed_version: str | None = None  # not installed here, but the plugin id is installed without this command
    manifest: Manifest | None = field(default=None, compare=False)  # set for not-installed matches


def _matches(words: list[str], name: str, summary: str, keywords) -> bool:
    fields = [name.lower(), summary.lower(), *(k.lower() for k in keywords)]
    return all(any(w in f for f in fields) for w in words)


def search(store, words: list[str], warn: Callable[[str], None] | None = None) -> list[Match]:
    """Commands of installed and catalog plugins for which every word is in the name, summary or a keyword."""
    words = [w.lower() for w in words]
    index = store.index()
    available = load(store, warn)
    by_id: dict[str, list[Available]] = {}
    for a in available:
        by_id.setdefault(a.manifest.id, []).append(a)

    found: list[Match] = []
    for pid, entry in index.items():
        other = sorted({a.manifest.version for a in by_id.get(pid, []) if a.manifest.version != entry["version"]})
        keywords = entry.get("keywords", {})
        for name, summary in entry["commands"].items():
            kws = tuple(keywords.get(name, []))
            if _matches(words, name, summary, kws):
                found.append(Match(name, summary, kws, pid, entry["version"], True, None,
                                   ", ".join(other) or None, None))
    for a in available:
        m = a.manifest
        entry = index.get(m.id)
        target = m.id if len(by_id[m.id]) == 1 else str(a.folder)
        for c in m.commands:
            if entry is not None and c.name in entry["commands"]:
                continue  # listed once, from the installed copy
            if _matches(words, c.name, c.summary, c.keywords):
                found.append(Match(c.name, c.summary, tuple(c.keywords), m.id, m.version, False, a.folder, None,
                                   f"dock plugin install {target}", entry["version"] if entry else None, m))
    return sorted(found, key=lambda x: (x.name, x.plugin, str(x.folder or "")))


def _install_target(a: Available, by_id: dict[str, list[Available]]) -> str:
    return a.manifest.id if len(by_id[a.manifest.id]) == 1 else str(a.folder)


def hint(store, command: str, warn: Callable[[str], None] | None = None) -> str | None:
    """The 'unknown command' message naming the catalog plugins that have `command`; None when none has it."""
    available = load(store, warn)
    by_id: dict[str, list[Available]] = {}
    for a in available:
        by_id.setdefault(a.manifest.id, []).append(a)
    owners = sorted((a for a in available if a.manifest.command(command)), key=lambda a: (a.manifest.id, str(a.folder)))
    if not owners:
        return None
    index = store.index()
    if len(owners) == 1:
        a = owners[0]
        install = f"dock plugin install {_install_target(a, by_id)}"
        if a.manifest.id in index:
            return (f"unknown command '{command}'. {a.manifest.id} {a.manifest.version} in the catalog has it "
                    f"(installed: {index[a.manifest.id]['version']}): {install}")
        return f"unknown command '{command}'. {a.manifest.id} has it (not installed): {install}"

    def one(a: Available) -> str:
        install = f"dock plugin install {_install_target(a, by_id)}"
        if a.manifest.id in index:
            return f"{a.manifest.id} {a.manifest.version} (installed: {index[a.manifest.id]['version']}; {install})"
        return f"{a.manifest.id} ({install})"

    each = "; ".join(one(a) for a in owners)
    return f"unknown command '{command}'. Not installed, in the catalog: {each}"


def find(store, plugin_id: str, warn: Callable[[str], None] | None = None) -> Available | None:
    """The one catalog plugin with this id; None when there is none; ManifestError when several folders have it."""
    owners = sorted((a for a in load(store, warn) if a.manifest.id == plugin_id), key=lambda a: str(a.folder))
    if len(owners) > 1:
        each = ", ".join(f"{a.folder} ({a.manifest.version})" for a in owners)
        raise ManifestError(f"{plugin_id} is in several catalog folders; install one by path: {each}")
    return owners[0] if owners else None


def parse_version(v: str) -> tuple[int, ...] | None:
    """A dotted-number version as ints with trailing zeros dropped (1.0 == 1.0.0); None when it is not one."""
    if not re.fullmatch(r"[0-9]+(\.[0-9]+)*", v):
        return None
    try:
        parts = [int(p) for p in v.split(".")]
    except ValueError:  # a part over Python's digit limit for int()
        return None
    while parts and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


@dataclass(frozen=True)
class Check:
    """What `plugin update` finds for one installed plugin id."""
    plugin_id: str
    status: str  # "update" | "current" | "skip" | "error"
    old: str
    new: str | None = None
    folder: Path | None = None  # the catalog copy to install, when status is "update"
    message: str | None = None  # the warning (skip) or error text


def check_updates(store, ids: list[str], warn: Callable[[str], None] | None = None) -> list[Check]:
    """Compare installed plugins (all, or `ids`) with the catalog; sorted by id, nothing is written."""
    if not paths(store):
        raise ManifestError(f"no catalog folders configured ([catalog] paths in {store.home / 'config.toml'})")
    warn = warn or _warner(store)
    index = store.index()
    named = bool(ids)
    wanted = sorted(set(ids)) if named else sorted(index)
    by_id: dict[str, list[tuple[tuple[int, ...], Available]]] = {}
    for a in load(store, warn):
        parsed = parse_version(a.manifest.version)
        if parsed is None:
            warn(f"catalog plugin {a.folder} skipped: version '{a.manifest.version}' is not dotted numbers")
            continue
        by_id.setdefault(a.manifest.id, []).append((parsed, a))

    result: list[Check] = []
    for pid in wanted:
        if pid not in index:
            result.append(Check(pid, "error", "", message=f"{pid} is not installed"))
            continue
        old = index[pid]["version"]
        installed = parse_version(old)
        if installed is None:
            msg = f"{pid}: installed version '{old}' is not dotted numbers"
            result.append(Check(pid, "error" if named else "skip", old, message=msg))
            continue
        copies = by_id.get(pid, [])
        if not copies:
            if named:
                result.append(Check(pid, "error", old, message=f"{pid} is not in the catalog"))
            continue
        best = max(v for v, _ in copies)
        top = sorted((a for v, a in copies if v == best), key=lambda a: str(a.folder))
        if best <= installed:
            result.append(Check(pid, "current", old))
        elif len(top) > 1:
            each = ", ".join(f"{a.folder}" for a in top)
            result.append(Check(pid, "error", old, message=(
                f"{pid} {top[0].manifest.version} is in several catalog folders; "
                f"update by installing one by path: {each}")))
        else:
            result.append(Check(pid, "update", old, top[0].manifest.version, top[0].folder))
    return result


def install_source(store, arg: str, base: Path | None = None) -> Path:
    """The folder `plugin install <arg>` reads: `arg` itself (against `base`) when it is an existing folder, else the catalog's."""
    here = Path(base) / arg if base is not None else Path(arg)
    if here.is_dir():
        return here
    found = find(store, arg)
    if found is None:
        raise ManifestError(f"no plugin folder or catalog plugin named '{arg}'")
    return found.folder
