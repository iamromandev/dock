"""dock <command> [args...]

Built-in commands manage plugins. Every other command is looked up in the
plugin index and forwarded, args untouched, to the plugin that owns it.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path

from src import __version__, approve, catalog, isolation, lifecycle, paths, uimsg
from src.inputs import UsageError, parse_for
from src.manifest import Manifest, ManifestError
from src.stdio import ask_stdin, tty
from src.store import Store, staged
from src.term import Terminal
from src.ui import live_mode, make_ui

SERVE_REMOVED = "dock: serve was removed; run commands with 'dock <command>' or 'dock shell'"

BUILTIN_HELP = """\
usage: dock <command> [args...]

built-in:
  help                      show this help
  version                   show dock version
  search <word>...          find commands by name, summary or keyword, installed or in the catalog
  shell                     open the interactive dock> prompt (default on a terminal)
  plugin list               list installed plugins
  plugin info <id|path>     show a plugin's manifest details
  plugin install [--yes] <path|id>
                            install a plugin from a folder, or a catalog plugin by id
  plugin build <id>         (re)build a plugin's container image
  plugin remove <id>        uninstall a plugin
  plugin update [--dry-run] [--yes] [<id>...]
                            update installed plugins from the catalog
  flow run [--yes] [--keep] <file> [name=value...]
                            run a flow: several plugins' commands, in order
  flow check <file>         check a flow and show what it would do
  do [--yes] [--keep] [--] "<request>"
                            plan a flow from a request with a local model, then run it (needs the brain plugin)

options (before the command, e.g. `dock --out ~/o shell`):
  -v, --verbose             show plugin debug logs
  --out DIR                 the folder plugins with fs:write:cwd work in (default: the current folder;
                            or [paths] out in config.toml)
  --in DIR                  the folder plugins with fs:read:in read, read-only (default: the out folder;
                            or [paths] in); ignored when no plugin in the run reads it
"""


SHELL_HELP = """\
built-in:
  help                      show this help
  version                   show dock version
  search <word>...          find commands by name, summary or keyword, installed or in the catalog
  status                    list running plugins with idle time
  exit, quit                leave the shell (or press Ctrl-D)
  plugin list               list installed plugins
  plugin info <id|path>     show a plugin's manifest details
  plugin install [--yes] <path|id>
                            install a plugin from a folder, or a catalog plugin by id
  plugin build <id>         (re)build a plugin's container image
  plugin remove <id>        uninstall a plugin
  plugin update [--dry-run] [--yes] [<id>...]
                            update installed plugins from the catalog

anything else runs a plugin command: <command> [args...]
"""


def cmd_help(store: Store, in_shell: bool = False) -> int:
    print(SHELL_HELP if in_shell else BUILTIN_HELP)
    idx = store.index()
    if idx:
        print("plugin commands:")
        for pid, entry in sorted(idx.items()):
            for name, summary in entry["commands"].items():
                print(f"  {uimsg.sanitize(name):<25} {uimsg.sanitize(summary)}  ({pid})")
    else:
        print("no plugins installed yet: dock plugin install <path>")
    return 0


def cmd_search(store: Store, args: list[str]) -> int:
    if not args or any(not a.strip() for a in args):
        print("usage: dock search <word>...", file=sys.stderr)
        return 2
    matches = catalog.search(store, args)
    if not matches:
        print(f"dock: no commands match '{' '.join(args)}'", file=sys.stderr)
        return 1
    for m in matches:
        if m.installed:
            status = "installed" + (f" (catalog has {m.catalog_version})" if m.catalog_version else "")
        elif m.installed_version:
            status = f"not in installed {m.installed_version}: {m.install}"
        else:
            status = f"not installed: {m.install}"
        print(f"{m.name:<16} {m.summary}  {m.plugin} {m.version}  {status}")
    return 0


def install_from(store: Store, folder: Path) -> int:
    m = store.install(folder)
    print(f"installed {m.id} {m.version}")
    if m.commands:
        print(f"  commands:    {', '.join(c.name for c in m.commands)}")
    if m.permissions:
        print(f"  permissions: {', '.join(m.permissions)}")
    if m.env:
        print(f"  env:         {', '.join(m.env)}")
    return 0


def installed_manifest(store: Store, plugin_id: str) -> Manifest | None:
    """The installed plugin's manifest, or None when it is not installed or cannot be read."""
    try:
        return store.get(plugin_id, any_api=True)
    except (ManifestError, OSError, KeyError):
        return None


def _approver(yes: bool, interactive: bool | None, ask: Callable[[str], str | None] | None,
              store: Store) -> approve.Approver:
    return approve.Approver(yes, tty() if interactive is None else interactive, ask or ask_stdin, store.notice)


def cmd_plugin_install(store: Store, args: list[str], stop: Callable[[str], object] | None = None,
                       interactive: bool | None = None, ask: Callable[[str], str | None] | None = None) -> int:
    """Install a folder or catalog plugin; ask first when it asks for permissions or env it is not yet granted."""
    yes = "--yes" in args or "-y" in args
    rest = [a for a in args if a not in ("--yes", "-y")]
    for a in rest:
        if a.startswith("-"):
            print(f"dock: plugin install: unknown option '{a}'", file=sys.stderr)
            print(BUILTIN_HELP, file=sys.stderr)
            return 2
    if len(rest) != 1:
        print(BUILTIN_HELP, file=sys.stderr)
        return 2
    source = catalog.install_source(store, rest[0], paths.start_of(store))
    with staged(source) as folder:  # what is shown and approved is what gets installed
        new = Manifest.load(folder)
        index = store.index()
        installed = new.id in index
        old = installed_manifest(store, new.id) if installed else None
        add = approve.added(new, old)
        if add:
            if installed:
                line = f"{new.id} {index[new.id]['version']} → {new.version} adds: {', '.join(add)}"
            else:
                line = f"{new.id} {new.version} asks for: {', '.join(add)}"
            decision = _approver(yes, interactive, ask, store).decide(line, "Install it? [y/N] ")
            if decision == "no":
                print("not installed")
                return 1
            if decision == "refused":
                return 1
        if stop is not None:
            stop(new.id)
        return install_from(store, folder)


def cmd_plugin_update(store: Store, args: list[str], stop: Callable[[str], object] | None = None,
                      interactive: bool | None = None, ask: Callable[[str], str | None] | None = None) -> int:
    """Update installed plugins from the catalog; `stop(id)` runs right before each real update."""
    dry_run = "--dry-run" in args
    yes = "--yes" in args or "-y" in args
    ids = [a for a in args if a not in ("--dry-run", "--yes", "-y")]
    for a in ids:
        if a.startswith("-"):
            print(f"dock: plugin update: unknown option '{a}'", file=sys.stderr)
            print(BUILTIN_HELP, file=sys.stderr)
            return 2
    if not ids and not store.index():
        print("no plugins installed")
        return 0
    approver = _approver(yes, interactive, ask, store)
    failed = changed = False
    for c in catalog.check_updates(store, ids):
        if c.status == "update":
            try:
                if dry_run:
                    add = approve.added(Manifest.load(c.folder), installed_manifest(store, c.plugin_id))
                    print(f"would update {c.plugin_id} {c.old} → {c.new}" + (f" (adds: {', '.join(add)})" if add else ""))
                    changed = True
                    continue
                with staged(c.folder) as folder:  # what is shown and approved is what gets installed
                    add = approve.added(Manifest.load(folder), installed_manifest(store, c.plugin_id))
                    if add:
                        line = f"{c.plugin_id} {c.old} → {c.new} adds: {', '.join(add)}"
                        decision = approver.decide(line, "Update it? [y/N] ")
                        if decision == "no":
                            print(f"skipped {c.plugin_id}: new permissions not approved")
                            changed = True
                            continue
                        if decision == "refused":
                            failed = True
                            continue
                    if stop is not None:
                        stop(c.plugin_id)
                    old, m = store.update(c.plugin_id, folder)
            except (ManifestError, OSError) as e:  # shutil.Error is an OSError
                print(f"dock: {c.plugin_id}: {e}", file=sys.stderr)
                failed = True
                continue
            print(f"updated {c.plugin_id} {old} → {m.version}")
            changed = True
        elif c.status == "current":
            if ids:
                print(f"{c.plugin_id} {c.old} is up to date")
        elif c.status == "skip":
            print(f"dock: {c.message}; skipped", file=sys.stderr)
        else:
            print(f"dock: {c.message}", file=sys.stderr)
            failed = True
    if not ids and not changed and not failed:
        print("all plugins up to date")
    return 1 if failed else 0


def cmd_plugin(store: Store, args: list[str]) -> int:
    sub = args[0] if args else "list"
    if sub == "update":
        return cmd_plugin_update(store, args[1:])
    if sub == "list":
        idx = store.index()
        if not idx:
            print("no plugins installed")
        for pid, e in sorted(idx.items()):
            parts = []
            if e["commands"]:
                parts.append("commands: " + ", ".join(e["commands"]))
            print(f"{pid:<22} {e['version']:<8} {'  '.join(parts)}")
        return 0
    if sub == "install":
        return cmd_plugin_install(store, args[1:])
    if sub == "remove" and len(args) == 2:
        store.remove(args[1])
        print(f"removed {args[1]}")
        return 0
    if sub == "build" and len(args) == 2:
        tag = isolation.build(store, store.get(args[1]))
        print(f"built {tag}")
        return 0
    if sub == "info" and len(args) == 2:
        index = store.index()
        installed = args[1] in index
        if installed:
            m = store.get(args[1])
        else:  # a folder, or the id of a catalog plugin: read its plugin.toml, nothing is installed
            m = Manifest.load(catalog.install_source(store, args[1], paths.start_of(store)))
        print(f"id:          {m.id}\nversion:     {m.version}\nprotocol:    v{m.api_version}")
        print(f"description: {m.description}")
        print(f"commands:    {', '.join(c.name for c in m.commands) or '-'}")
        print(f"permissions: {', '.join(m.permissions) or '-'}")
        print(f"env:         {', '.join(m.env) or '-'}")
        print(f"keywords:    {'; '.join(f'{c.name}: ' + ', '.join(c.keywords) for c in m.commands if c.keywords) or '-'}")
        print(f"runtime:     {isolation.describe(store, m)}")
        print(f"location:    {m.root}")
        if not installed:
            there = index.get(m.id)
            print(f"installed:   yes, {there['version']} ({there['path']})" if there
                  else f"installed:   no ({m.root})")
        return 0
    print(BUILTIN_HELP, file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    verbose = False
    flag_in = flag_out = None
    store = Store()
    try:
        while argv:  # -v, --in and --out in any order, up to the subcommand
            if argv[0] in ("-v", "--verbose"):
                verbose = True
                argv.pop(0)
                continue
            got_in, got_out, rest = paths.take_flags(argv)
            if rest == argv:
                break
            for flag, new, old in (("--in", got_in, flag_in), ("--out", got_out, flag_out)):
                if new is not None and old is not None:
                    raise ManifestError(f"{flag} given more than once")
            flag_in, flag_out, argv = got_in or flag_in, got_out or flag_out, rest
        if argv[:1] and argv[0] in ("help", "-h", "--help", "version", "--version"):
            dirs = None  # these need no folders, so a bad [paths] must not block them
        else:
            dirs = paths.resolve(store, flag_in, flag_out)
    except ManifestError as e:
        print(f"dock: {e}", file=sys.stderr)
        return 1
    if dirs is not None and dirs.out_source != "current folder":
        try:
            os.chdir(dirs.out_dir)  # every later os.getcwd()/Path.cwd() (the mounted folder) is the out folder
        except OSError as e:
            print(f"dock: {dirs.out_source} {dirs.out_dir}: cannot enter it: {e.strerror or e}", file=sys.stderr)
            return 1
    store.dirs = dirs
    command = ""
    try:
        if argv[:1] == ["shell"] or (not argv and sys.stdin.isatty()):
            from src import shell

            return shell.start(store, verbose)
        if argv[:1] == ["serve"]:
            print(SERVE_REMOVED, file=sys.stderr)
            return 1
        if not argv or argv[0] in ("help", "-h", "--help"):
            return cmd_help(store)
        if argv[0] in ("version", "--version"):
            print(f"dock {__version__}")
            return 0
        if argv[0] == "plugin":
            return cmd_plugin(store, argv[1:])
        if argv[0] == "search":
            return cmd_search(store, argv[1:])
        if argv[0] == "flow":
            from src import flow_cli

            return flow_cli.cmd_flow(store, argv[1:], verbose=verbose)
        if argv[0] == "do":
            from src import flow_cli

            return flow_cli.cmd_do(store, argv[1:], verbose=verbose)

        manifest, command = store.resolve(argv[0])
        parse_for(manifest, command, argv[1:])  # a bad invocation must start no process
        paths.warn_if_in_unused(store, [manifest])
        isolation.sweep(store)
        term = Terminal() if live_mode() else None  # live areas are drawn on it; given back on every way out
        if term is not None:
            term.install()
        try:
            return lifecycle.run(store, manifest, command, argv[1:], lambda pid: make_ui(pid, verbose, term), verbose=verbose)
        finally:
            if term is not None:
                term.close()
    except ManifestError as e:
        print(f"dock: {e}", file=sys.stderr)
        return 1
    except UsageError as e:
        print(f"dock: {command}: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
