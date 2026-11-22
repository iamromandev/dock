"""dock <command> [args...]

Built-in commands manage plugins. Every other command is looked up in the
plugin index and forwarded, args untouched, to the plugin that owns it.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

from cli import __version__, catalog, isolation, lifecycle
from cli.inputs import UsageError, parse_for
from cli.manifest import Manifest, ManifestError
from cli.store import Store

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
  plugin install <path|id>  install a plugin from a folder, or a catalog plugin by id
  plugin build <id>         (re)build a plugin's container image
  plugin remove <id>        uninstall a plugin
  plugin update [--dry-run] [<id>...]
                            update installed plugins from the catalog

options (before the command):
  -v, --verbose             show plugin debug logs
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
  plugin install <path|id>  install a plugin from a folder, or a catalog plugin by id
  plugin build <id>         (re)build a plugin's container image
  plugin remove <id>        uninstall a plugin
  plugin update [--dry-run] [<id>...]
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
                print(f"  {name:<25} {summary}  ({pid})")
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


def cmd_plugin_update(store: Store, args: list[str], stop: Callable[[str], object] | None = None) -> int:
    """Update installed plugins from the catalog; `stop(id)` runs right before each real update."""
    dry_run = "--dry-run" in args
    ids = [a for a in args if a != "--dry-run"]
    for a in ids:
        if a.startswith("-"):
            print(f"dock: plugin update: unknown option '{a}'", file=sys.stderr)
            print(BUILTIN_HELP, file=sys.stderr)
            return 2
    if not ids and not store.index():
        print("no plugins installed")
        return 0
    failed = changed = False
    for c in catalog.check_updates(store, ids):
        if c.status == "update":
            if dry_run:
                print(f"would update {c.plugin_id} {c.old} → {c.new}")
                changed = True
                continue
            try:
                if stop is not None:
                    stop(c.plugin_id)
                old, m = store.update(c.plugin_id, c.folder)
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
    if sub == "install" and len(args) == 2:
        return install_from(store, catalog.install_source(store, args[1]))
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
            m = Manifest.load(catalog.install_source(store, args[1]))
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
    while argv and argv[0] in ("-v", "--verbose"):
        verbose = True
        argv.pop(0)

    store = Store()
    command = ""
    try:
        if argv[:1] == ["shell"] or (not argv and sys.stdin.isatty()):
            from cli import shell

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

        manifest, command = store.resolve(argv[0])
        parse_for(manifest, command, argv[1:])  # a bad invocation must start no process
        isolation.sweep(store)
        return lifecycle.run(store, manifest, command, argv[1:], verbose=verbose)
    except ManifestError as e:
        print(f"dock: {e}", file=sys.stderr)
        return 1
    except UsageError as e:
        print(f"dock: {command}: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
