"""dock <command> [args...]

Built-in commands manage plugins. Every other command is looked up in the
plugin index and forwarded, args untouched, to the plugin that owns it.
"""

from __future__ import annotations

import sys
from pathlib import Path

from cli import __version__, isolation, lifecycle
from cli.inputs import UsageError, parse_for
from cli.manifest import ManifestError, platform_key
from cli.store import Store

BUILTIN_HELP = """\
usage: dock <command> [args...]

built-in:
  help                      show this help
  version                   show dock version
  shell                     open the interactive dock> prompt (default on a terminal)
  plugin list               list installed plugins
  plugin info <id>          show a plugin's manifest details
  plugin install <path>     install a plugin from a folder
  plugin build <id>         (re)build a plugin's container image
  plugin remove <id>        uninstall a plugin

options (before the command):
  -v, --verbose             show plugin debug logs
"""


SHELL_HELP = """\
built-in:
  help                      show this help
  version                   show dock version
  status                    list running plugins with idle time
  exit, quit                leave the shell (or press Ctrl-D)
  plugin list               list installed plugins
  plugin info <id>          show a plugin's manifest details
  plugin install <path>     install a plugin from a folder
  plugin build <id>         (re)build a plugin's container image
  plugin remove <id>        uninstall a plugin

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


def cmd_plugin(store: Store, args: list[str]) -> int:
    sub = args[0] if args else "list"
    if sub == "list":
        idx = store.index()
        if not idx:
            print("no plugins installed")
        for pid, e in sorted(idx.items()):
            parts = []
            if e["commands"]:
                parts.append("commands: " + ", ".join(e["commands"]))
            if e.get("provides"):
                parts.append("provides: " + ", ".join(e["provides"]))
            print(f"{pid:<22} {e['version']:<8} {'  '.join(parts)}")
        return 0
    if sub == "install" and len(args) == 2:
        m = store.install(Path(args[1]))
        print(f"installed {m.id} {m.version}")
        if m.commands:
            print(f"  commands:    {', '.join(c.name for c in m.commands)}")
        if m.provides:
            print(f"  provides:    {', '.join(m.provides)}")
        if m.requires:
            print(f"  requires:    {', '.join(m.requires)}")
        if m.permissions:
            print(f"  permissions: {', '.join(m.permissions)}")
        if m.env:
            print(f"  env:         {', '.join(m.env)}")
        for cap in m.requires:
            if not store.providers_of(cap):
                print(f"  note: nothing installed provides '{cap}' yet; install a plugin that does")
        return 0
    if sub == "remove" and len(args) == 2:
        store.remove(args[1])
        print(f"removed {args[1]}")
        return 0
    if sub == "build" and len(args) == 2:
        tag = isolation.build(store, store.get(args[1]))
        print(f"built {tag}")
        return 0
    if sub == "info" and len(args) == 2:
        m = store.get(args[1])
        print(f"id:          {m.id}\nversion:     {m.version}\nprotocol:    v{m.api_version}")
        print(f"description: {m.description}\nprovision:   {m.provision}")
        print(f"commands:    {', '.join(c.name for c in m.commands) or '-'}")
        print(f"provides:    {', '.join(m.provides) or '-'}")
        print(f"requires:    {', '.join(m.requires) or '-'}")
        print(f"permissions: {', '.join(m.permissions) or '-'}")
        print(f"env:         {', '.join(m.env) or '-'}")
        print(f"runner:      {isolation.runner_line(store, m)}")
        print(f"isolation:   {isolation.describe(store, m)}")
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
    command = ""
    try:
        if argv[:1] == ["shell"] or (not argv and sys.stdin.isatty()):
            from cli import shell

            return shell.start(store, verbose)
        if not argv or argv[0] in ("help", "-h", "--help"):
            return cmd_help(store)
        if argv[0] in ("version", "--version"):
            print(f"dock {__version__}")
            return 0
        if argv[0] == "plugin":
            return cmd_plugin(store, argv[1:])

        manifest, command = store.resolve(argv[0])
        parse_for(manifest, command, argv[1:])  # a bad invocation must start no process, not even recovery
        isolation.sweep(store)
        lifecycle.recover(store)
        return lifecycle.run(store, manifest, command, argv[1:], verbose=verbose)
    except ManifestError as e:
        print(f"dock: {e}", file=sys.stderr)
        return 1
    except UsageError as e:
        print(f"dock: {command}: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
