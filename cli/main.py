"""dock <command> [args...]

Built-in commands manage plugins. Every other command is looked up in the
plugin index and forwarded, args untouched, to the plugin that owns it.
"""

from __future__ import annotations

import sys
from pathlib import Path

from cli import __version__, lifecycle
from cli.manifest import ManifestError, platform_key
from cli.store import Store

BUILTIN_HELP = """\
usage: dock <command> [args...]

built-in:
  help                      show this help
  version                   show dock version
  plugins list              list installed plugins
  plugins info <id>         show a plugin's manifest details
  plugins install <path>    install a plugin from a folder
  plugins remove <id>       uninstall a plugin

options (before the command):
  -v, --verbose             show plugin debug logs
"""


def cmd_help(store: Store) -> int:
    print(BUILTIN_HELP)
    idx = store.index()
    if idx:
        print("plugin commands:")
        for pid, entry in sorted(idx.items()):
            for name, summary in entry["commands"].items():
                print(f"  {name:<25} {summary}  ({pid})")
    else:
        print("no plugins installed yet: dock plugins install <path>")
    return 0


def cmd_plugins(store: Store, args: list[str]) -> int:
    sub = args[0] if args else "list"
    if sub == "list":
        idx = store.index()
        if not idx:
            print("no plugins installed")
        for pid, e in sorted(idx.items()):
            print(f"{pid:<22} {e['version']:<8} {', '.join(e['commands'])}")
        return 0
    if sub == "install" and len(args) == 2:
        m = store.install(Path(args[1]))
        print(f"installed {m.id} {m.version} -> commands: {', '.join(c.name for c in m.commands)}")
        if m.permissions:
            print(f"  requested permissions: {', '.join(m.permissions)}")
        return 0
    if sub == "remove" and len(args) == 2:
        store.remove(args[1])
        print(f"removed {args[1]}")
        return 0
    if sub == "info" and len(args) == 2:
        m = store.get(args[1])
        print(f"id:          {m.id}\nversion:     {m.version}\nprotocol:    v{m.api_version}")
        print(f"description: {m.description}\nprovision:   {m.provision}")
        print(f"permissions: {', '.join(m.permissions) or '-'}")
        print(f"builds:      {', '.join(m.run)}  (this machine: {platform_key()})")
        print(f"location:    {m.root}")
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
    try:
        if not argv or argv[0] in ("help", "-h", "--help"):
            return cmd_help(store)
        if argv[0] in ("version", "--version"):
            print(f"dock {__version__}")
            return 0
        if argv[0] == "plugins":
            return cmd_plugins(store, argv[1:])

        lifecycle.recover(store)
        manifest, command = store.resolve(argv[0])
        return lifecycle.run(store, manifest, command, argv[1:], verbose=verbose)
    except ManifestError as e:
        print(f"dock: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
