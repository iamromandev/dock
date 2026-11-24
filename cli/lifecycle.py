"""spawn -> initialize -> invoke* -> shutdown (kill on timeout)

A Session starts the plugin that owns a command and keeps it running, so it can handle many invokes (the shell).
One-shot `dock <command>` is a Session with a single invoke. On close, everything the session started is stopped,
newest first.
"""

from __future__ import annotations

import os
import time

from cli import API_VERSION, __version__, isolation
from cli.inputs import parse_for
from cli.manifest import Manifest
from cli.process import CANCELLED, Hub, PluginError, PluginProcess
from cli.store import Store
from cli.ui import UI, make_ui

INIT_TIMEOUT = 10
CANCEL_GRACE = float(os.environ.get("DOCK_CANCEL_GRACE", 5))  # seconds a plugin gets to honour `cancel`


class Session:
    def __init__(self, store: Store, verbose: bool = False, term=None):
        self.store = store
        self.verbose = verbose
        self.term = term                             # the Terminal in live mode: plugins' messages are drawn on it
        self.hub = Hub()                             # all plugins of the session share one inbox
        self.procs: list[PluginProcess] = []        # start order
        self.commands: dict[str, PluginProcess] = {}   # plugin id -> running command plugin
        self.last_used: dict[PluginProcess, float] = {}  # time.monotonic()

    def _ui(self, plugin_id: str) -> UI:
        """Who shows this plugin's messages: the live renderer on our Terminal, or plain text."""
        return make_ui(plugin_id, self.verbose, self.term)

    # starting plugins ------------------------------------------------------
    def start(self, manifest: Manifest, ui: UI) -> PluginProcess:
        # may build an image; raises before anything starts
        launch = isolation.prepare(self.store, manifest)
        proc = PluginProcess(manifest, self.store.log_path(manifest.id), ui.handlers(), launch, self.hub)
        proc.start()
        self.procs.append(proc)
        self.last_used[proc] = time.monotonic()
        try:
            proc.request(
                "initialize",
                {
                    "api_version": API_VERSION,
                    "host": {"name": "dock", "version": __version__},
                    "data_dir": str(self.store.data_dir(manifest.id)),
                },
                timeout=INIT_TIMEOUT,
            )
        except BaseException as e:
            # A half-started plugin must not stay registered: it would be orphaned
            # (never mapped as a command) and a retry would start another.
            if isinstance(e, KeyboardInterrupt):
                try:
                    proc.notify("cancel")  # let a slow initialize stop early
                except PluginError:
                    pass
            self.stop_proc(proc)
            raise
        return proc

    # stopping ------------------------------------------------------------------
    def cancel_all(self) -> None:
        for proc in self.procs:
            try:
                proc.notify("cancel")
            except PluginError:
                pass

    def stop_proc(self, proc: PluginProcess, force: bool = False) -> None:
        """Stop one process (shutdown first), and forget it.

        force: the plugin stopped answering; kill it without asking.
        """
        if not force and proc.forgotten:
            return
        self._stop_one(proc, force)

    def _stop_one(self, proc: PluginProcess, force: bool) -> None:
        try:
            proc.kill() if force else proc.stop()
        finally:
            self._forget(proc)

    def _forget(self, proc: PluginProcess) -> None:
        if proc in self.procs:
            self.procs.remove(proc)
        self.last_used.pop(proc, None)
        for key in [k for k, v in list(self.commands.items()) if v is proc]:
            self.commands.pop(key, None)
        proc.forgotten = True
        for key in [k for k in list(self.hub.replies) if k[0] is proc]:
            self.hub.replies.pop(key, None)

    def drain_pending(self, deadline: float) -> bool:
        """Handle the plugins' messages until none owes a reply, or `deadline` (time.monotonic()) passes.

        After `cancel`, a plugin may still owe a reply (an invoke it is winding down, or a request
        the host sent); it gets the rest of the grace period. A plugin that exited can no longer
        answer and does not count. Returns True when nothing is owed.
        """
        while True:
            if not any(p.pending and p.alive() for p in self.procs):
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            self.hub.pump(remaining)

    def _stop_unresponsive(self, ui: UI | None = None) -> None:
        """After cancel and its grace period: a plugin still owing a reply is stuck.

        It would answer the next request late (or never) and leak output into it,
        so it is killed and starts afresh on next use. Replies that nobody waits
        for any more are dropped. `ui` is who says so: the one of the run, as it may be drawing.
        """
        for proc in list(self.procs):
            if proc.pending:
                if proc.alive():
                    (ui or self._ui(proc.manifest.id)).warn("did not stop on cancel; stopped it")
                self.stop_proc(proc, force=True)
        self.hub.replies.clear()

    def _interrupted(self, ui: UI) -> None:
        """Ctrl-C during an invoke: cancel, give everyone one shared grace period, kill the stuck.

        A second Ctrl-C ends the wait early. The kill step runs on every path.
        """
        deadline = time.monotonic() + CANCEL_GRACE
        try:
            ui.warn("interrupted, cancelling…")
            self.cancel_all()
            self.drain_pending(deadline)
        except (PluginError, KeyboardInterrupt):
            pass
        finally:
            self._stop_unresponsive(ui)  # the UI that draws this run: a second one would draw over it

    def stop_plugin(self, plugin_id: str) -> bool:
        victims = [p for p in self.procs if p.manifest.id == plugin_id]
        for proc in reversed(victims):
            self.stop_proc(proc)
        return bool(victims)

    def reap_idle(self, timeout: float) -> list[str]:
        """Stop processes idle for more than `timeout` seconds (0 = never); returns their ids."""
        if timeout <= 0:
            return []
        now = time.monotonic()
        reaped = []
        for proc in list(self.procs):
            if now - self.last_used.get(proc, now) > timeout:
                self.stop_proc(proc)
                reaped.append(proc.manifest.id)
        return reaped

    def running(self) -> list[tuple[str, float, str]]:
        """(plugin id, idle seconds, container runtime) per running plugin."""
        now = time.monotonic()
        return [(p.manifest.id, now - self.last_used.get(p, now), p.launch.runtime) for p in self.procs]

    def close(self) -> None:
        """Stop everything, newest first, even if one stop fails; then re-raise the first failure."""
        first: BaseException | None = None
        for proc in reversed(list(self.procs)):
            try:
                self.stop_proc(proc)
            except BaseException as e:  # noqa: BLE001 - includes Ctrl-C; the others must still stop
                first = first or e
        if first is not None:
            raise first

    # running a command -----------------------------------------------------------
    def _invoke(self, manifest: Manifest, command: str, args: list[str], ui: UI) -> dict:
        # Parse first: a usage error (UsageError) must surface before anything starts.
        inputs = parse_for(manifest, command, args)
        proc = self.commands.get(manifest.id)
        try:
            if proc is not None and not proc.alive():
                self.stop_proc(proc)
                proc = None
            if proc is None:
                proc = self.start(manifest, ui)
                self.commands[manifest.id] = proc

            self.last_used[proc] = time.monotonic()
            # A process outlives its invokes (the shell reuses it), so what it says goes to the UI of the invoke
            # it is answering. Between invokes it has no UI: whatever it said late (after a cancel) is dropped
            # now, so it can not show up in this run's UI.
            proc.handlers = {}
            while self.hub.pump(0):
                pass
            proc.handlers = ui.handlers()
            params = {"command": command, "args": args, "cwd": os.getcwd()}  # what prepare() mounts for fs:write:cwd
            if inputs is not None:
                params["inputs"] = inputs
            invoke_id = proc.send_request("invoke", params)
            try:
                result = proc.wait_for(invoke_id, "invoke", timeout=None)
            except KeyboardInterrupt:
                self._interrupted(ui)
                return {"exit_code": 130}
            finally:
                proc.handlers = {}
                if proc in self.procs:
                    self.last_used[proc] = time.monotonic()
            return result if isinstance(result, dict) else {"exit_code": 0}

        except KeyboardInterrupt:  # during startup (initialize)
            ui.warn("interrupted")
            return {"exit_code": 130}
        except PluginError:
            for p in list(self.procs):
                if not p.alive():
                    self.stop_proc(p)
            raise

    def invoke(self, manifest: Manifest, command: str, args: list[str]) -> int:
        ui = self._ui(manifest.id)
        ui.begin()
        try:
            result = self._invoke(manifest, command, args, ui)
        except PluginError as e:
            if e.code != CANCELLED:
                ui.warn(f"{manifest.id}: {e}")
            return 1
        finally:
            ui.end()  # whatever the plugin left on screen goes to scrollback, on every way out
        return int(result.get("exit_code", 0))


def run(store: Store, manifest: Manifest, command: str, args: list[str], verbose: bool = False, term=None) -> int:
    session = Session(store, verbose, term)
    try:
        return session.invoke(manifest, command, args)
    finally:
        session.close()

