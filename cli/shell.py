"""The interactive `dock>` shell: one long-lived Session, one operation per line."""

from __future__ import annotations

import os
import shlex
import sys

from cli import __version__, catalog, isolation
from cli.inputs import UsageError
from cli.lifecycle import Session
from cli.manifest import Manifest, ManifestError
from cli.store import Store

PROMPT = "dock> "
BUILTINS = ["help", "version", "plugin", "search", "status", "exit", "quit"]
PLUGIN_SUBS = ["list", "info", "install", "build", "remove", "update"]


class Shell:
    def __init__(self, store: Store, session: Session, input_fn=input, readline=None, verbose: bool = False):
        self.store = store
        self.session = session
        self.input_fn = input_fn
        self.readline = readline  # the readline module, or None when unavailable
        self.verbose = verbose
        self._reap_error: str | None = None  # last config error shown, so it is shown once

    def _reap(self) -> None:
        try:
            self._reap_idle()
        except KeyboardInterrupt:  # Ctrl-C while stopping a plugin: back to the prompt
            print()

    def _reap_idle(self) -> None:
        try:
            timeout = self.store.idle_timeout()
        except ManifestError as e:  # config.toml is broken: keep the shell, skip reaping
            if str(e) != self._reap_error:
                self._reap_error = str(e)
                print(f"dock: {e}; idle plugins are not stopped until it is fixed", file=sys.stderr)
            return
        self._reap_error = None
        for plugin_id in self.session.reap_idle(timeout):
            if self.verbose:
                print(f"dock: stopped idle {plugin_id}", file=sys.stderr)

    def _save_history(self) -> None:
        if self.readline is None:
            return
        try:
            self.readline.write_history_file(self.store.history_path)
            os.chmod(self.store.history_path, 0o600)  # it records what the user typed
        except OSError as e:
            print(f"dock: cannot write history: {e}", file=sys.stderr)

    def candidates(self, line: str, text: str) -> list[str]:
        words = line.split()
        done = words if line.endswith(" ") else words[:-1]  # words before the one being typed
        if not done:
            names = set(BUILTINS)
            for entry in self.store.index().values():
                names.update(entry["commands"])
            return sorted(n for n in names if n.startswith(text))
        if done == ["plugin"]:
            return [s for s in PLUGIN_SUBS if s.startswith(text)]
        if len(done) == 2 and done[0] == "plugin" and done[1] in ("info", "build", "remove"):
            return sorted(i for i in self.store.index() if i.startswith(text))
        if done[:2] == ["plugin", "update"]:
            return sorted(i for i in self.store.index() if i.startswith(text) and i not in done)
        return []

    def complete(self, text: str, state: int) -> str | None:
        matches = self.candidates(self.readline.get_line_buffer(), text)
        return matches[state] if state < len(matches) else None

    def loop(self) -> int:
        try:
            while True:
                self._reap()
                try:
                    line = self.input_fn(PROMPT)
                except EOFError:
                    print()
                    break
                except KeyboardInterrupt:
                    print()
                    continue
                self._reap()  # the wait at the prompt counts as idle time too
                try:
                    if not self.run_line(line):
                        break
                except KeyboardInterrupt:
                    print()
        finally:
            self._save_history()
            self.session.close()
        return 0

    def run_line(self, line: str) -> bool:
        """Run one input line. False means the shell should exit."""
        from cli import main  # lazy: main imports this module

        if not line.strip():
            return True
        if self.readline is not None:
            self.readline.add_history(line)
        try:
            words = shlex.split(line)
        except ValueError as e:
            print(f"dock: {e}", file=sys.stderr)
            return True
        name, args = words[0], words[1:]
        try:
            if name in ("exit", "quit"):
                return False
            if name == "help":
                main.cmd_help(self.store, in_shell=True)
            elif name == "version":
                print(f"dock {__version__}")
            elif name == "search":
                main.cmd_search(self.store, args)
            elif name == "status":
                self._status()
            elif name == "plugin":
                self._plugin(main, args)
            else:
                manifest, command = self.store.resolve(name)
                code = self.session.invoke(manifest, command, args)
                if code not in (0, 130):
                    print(f"exit {code}", file=sys.stderr)
        except ManifestError as e:
            print(f"dock: {e}", file=sys.stderr)
        except UsageError as e:
            print(f"dock: {name.split(':', 1)[-1]}: {e}", file=sys.stderr)
        return True

    def _status(self) -> None:
        procs = self.session.running()
        if not procs:
            print("no plugins running")
        for plugin_id, idle, runtime in procs:
            m, sec = divmod(int(idle), 60)
            print(f"{plugin_id:<20} running   idle {m}m{sec:02d}s   {isolation.runtime_label(runtime)}")

    def _plugin(self, main, args: list[str]) -> None:
        if len(args) == 2 and args[0] == "remove":
            self.session.stop_plugin(args[1])
            main.cmd_plugin(self.store, args)
        elif len(args) == 2 and args[0] == "install":
            folder = catalog.install_source(self.store, args[1])  # read the catalog once; errors reach run()
            try:
                self.session.stop_plugin(Manifest.load(folder).id)
            except ManifestError:
                pass  # install_from reports the broken manifest
            main.install_from(self.store, folder)
        elif args[:1] == ["update"]:
            main.cmd_plugin_update(self.store, args[1:], stop=self.session.stop_plugin)
        else:
            main.cmd_plugin(self.store, args)


def _setup_readline(store: Store):
    """Import and configure readline; None when disabled or unavailable (R16)."""
    if os.environ.get("DOCK_NO_READLINE"):
        return None
    try:
        import readline
    except ImportError:
        return None
    readline.set_auto_history(False)
    readline.set_history_length(1000)
    if store.history_path.exists():
        try:
            readline.read_history_file(store.history_path)
        except OSError:
            pass
    readline.set_completer_delims(" ")
    readline.parse_and_bind("bind ^I rl_complete" if "libedit" in (readline.__doc__ or "") else "tab: complete")
    return readline


def start(store: Store, verbose: bool) -> int:
    isolation.sweep(store)
    readline = _setup_readline(store)
    shell = Shell(store, Session(store, verbose), readline=readline, verbose=verbose)
    if readline is not None:
        readline.set_completer(shell.complete)
    return shell.loop()
