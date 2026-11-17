"""spawn -> initialize -> [provision] -> invoke -> [unprovision] -> shutdown

A Session starts the plugin that owns a command, plus one provider plugin per
capability it requires (for example chat requires `llm`, served by the llm
plugin), and keeps them running so it can serve many invokes (the shell).
One-shot `dock <command>` is a Session with a single invoke. The host relays
every call between them, so plugins never talk to each other directly. On
close, everything the session started is unprovisioned and stopped, newest
first.
"""

from __future__ import annotations

import json
import os
import threading
import time

from cli import API_VERSION, __version__, http_relay, isolation
from cli.inputs import parse_for
from cli.manifest import Manifest, ManifestError
from cli.process import CANCELLED, DEFERRED, Hub, PluginError, PluginProcess, SendAborted
from cli.store import Store
from cli.ui import TerminalUI

INIT_TIMEOUT = 10
PROVISION_TIMEOUT = 120
UNPROVISION_TIMEOUT = 30
CANCEL_GRACE = float(os.environ.get("DOCK_CANCEL_GRACE", 5))  # seconds a plugin gets to honour `cancel`


class Session:
    def __init__(self, store: Store, verbose: bool = False):
        self.store = store
        self.verbose = verbose
        self.hub = Hub()                             # all plugins of the session share one inbox
        self.procs: list[PluginProcess] = []        # start order
        self.providers: dict[str, PluginProcess] = {}  # capability -> running provider
        self.commands: dict[str, PluginProcess] = {}   # plugin id -> running command plugin
        self.last_used: dict[PluginProcess, float] = {}  # time.monotonic()
        self._active: list[str] = []                # capabilities mid-call, to catch cycles

    # starting plugins ------------------------------------------------------
    def start(self, manifest: Manifest, ui: TerminalUI) -> PluginProcess:
        launch = isolation.prepare(self.store, manifest)  # may build an image; raises before anything starts
        proc = PluginProcess(manifest, self.store.log_path(manifest.id), ui.handlers(), launch, self.hub)
        proc.handlers["call"] = lambda params: self._call(proc, params)
        proc.id_handlers["http"] = lambda params, msg_id: self._http(proc, params, msg_id)
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
            if manifest.provision:
                # Marker first: if dock dies during or after provision, the next run cleans up.
                self.store.marker(manifest.id).write_text(
                    json.dumps({"version": manifest.version, "since": time.time()})
                )
                proc.provisioned = True  # even a failed provision may have left something to release
                proc.request("provision", timeout=PROVISION_TIMEOUT)
        except BaseException as e:
            # A half-started plugin must not stay registered: it would be orphaned
            # (never mapped as command or provider) and a retry would start another.
            if isinstance(e, KeyboardInterrupt):
                try:
                    proc.notify("cancel")  # let a slow initialize/provision stop early
                except PluginError:
                    pass
            self.stop_proc(proc)
            raise
        return proc

    def provider_for(self, capability: str) -> PluginProcess:
        proc = self.providers.get(capability)
        if proc is not None and not proc.alive():
            self.stop_proc(proc)
            proc = None
        if proc is not None:
            self.last_used[proc] = time.monotonic()
        else:
            manifest = self.store.provider(capability)
            proc = self.start(manifest, TerminalUI(manifest.id, self.verbose))
            proc.handlers.pop("prompt_user", None)  # only the command's plugin talks to the user
            self.providers[capability] = proc
        return proc

    # relaying calls ----------------------------------------------------------
    def _call(self, caller: PluginProcess, params: dict):
        """caller -> host `call` -> provider `call`; provider `event`s -> caller `call_event`."""
        capability = params.get("capability")
        if capability not in caller.manifest.requires:
            raise PluginError(f"{caller.manifest.id} must list '{capability}' in capabilities.requires")
        if capability in self._active:
            raise PluginError(f"call cycle on '{capability}'")

        provider = self.provider_for(capability)
        self.last_used[provider] = time.monotonic()

        def forward(p: dict) -> None:
            try:
                caller.notify("call_event", {"capability": capability, "data": p.get("data")})
            except PluginError:
                pass  # the caller is gone; its own wait reports that

        provider.handlers["event"] = forward
        self._active.append(capability)
        try:
            return provider.request(
                "call",
                {"capability": capability, "method": params.get("method"), "params": params.get("params") or {}},
            )
        finally:
            self._active.pop()
            provider.handlers.pop("event", None)
            if provider in self.procs:
                self.last_used[provider] = time.monotonic()

    # relaying http -----------------------------------------------------------------
    def _http(self, proc: PluginProcess, params: dict, msg_id):
        """plugin -> host `http`: validate, then relay on a thread so other plugins keep being served.

        The thread streams `http_event` notifications and sends the reply itself (a body can take a long
        time; the hub must not be held up by it). Returns DEFERRED: the hub sends no reply of its own.
        """
        def log(line: str) -> None:
            try:
                proc._log.write(line)
            except (OSError, ValueError):
                pass  # the log is closed: the plugin was stopped

        if not isinstance(params, dict):
            raise PluginError("http params must be an object", -32602)
        if not proc.manifest.http:
            log(http_relay.refusal_line(params, "http is not declared"))
            raise PluginError("http is not declared: list the hosts this plugin may reach in [capabilities].http",
                              http_relay.REFUSED)
        if proc.http_active is not None:
            log(http_relay.refusal_line(params, "one http request at a time"))
            raise PluginError("one http request at a time", http_relay.REFUSED)

        def send_event(event: dict) -> None:
            try:  # waits (up to the relay's end) while the plugin reads nothing: never the hub, which does not send these
                proc.notify("http_event", {"id": msg_id, **event}, bulk=True, abort=lambda: relay.stopped)
            except SendAborted:
                relay.check_stopped()  # cancelled, or past the total deadline: that is the outcome
                raise

        relay = http_relay.Relay(proc.manifest.http, http_relay.config(self.store), send_event=send_event, log=log)

        def work() -> None:
            error = None
            result = None
            try:
                result = relay.run(params)
            except http_relay.Refused as e:
                error = {"code": http_relay.REFUSED, "message": str(e)}
            except http_relay.Cancelled:
                error = {"code": CANCELLED, "message": "cancelled"}
            except PluginError:
                return  # the plugin is gone; nobody is left to answer
            except Exception as e:  # noqa: BLE001 - whatever went wrong must still answer the plugin
                error = {"code": -32000, "message": f"http failed: {e}"}
            finally:
                if proc.http_active is relay:
                    proc.http_active = None  # before the reply: the plugin may send its next request at once
            try:
                proc.reply(msg_id, result=result, error=error)
            except PluginError:
                pass  # stopped meanwhile

        thread = threading.Thread(target=work, name=f"http-{proc.manifest.id}", daemon=True)
        proc.http_active = relay  # only now that it will run: work() is what clears it again
        try:
            thread.start()
        except BaseException:
            proc.http_active = None
            raise
        return DEFERRED

    # stopping ------------------------------------------------------------------
    @staticmethod
    def _abort_http(proc: PluginProcess) -> None:
        relay = proc.http_active
        if relay is not None:
            relay.cancel()

    def cancel_all(self) -> None:
        for proc in self.procs:
            self._abort_http(proc)
            try:
                proc.notify("cancel")
            except PluginError:
                pass

    def stop_proc(self, proc: PluginProcess, force: bool = False) -> None:
        """Unprovision (if it was provisioned) and stop one process, and forget it.

        force: the plugin stopped answering; kill it without unprovisioning. The
        marker stays, so a later unprovision or the next run cleans up.
        """
        unprovisioned = False
        self._abort_http(proc)  # a stopped plugin has no use for the answer: close the connection
        try:
            if proc.provisioned and not force:
                try:
                    proc.request("unprovision", timeout=UNPROVISION_TIMEOUT)
                    proc.provisioned = False
                    unprovisioned = True
                except PluginError as e:
                    TerminalUI(proc.manifest.id).warn(f"unprovision failed ({e}); will retry on next run")
        finally:  # still stop the process if unprovisioning was interrupted
            try:
                proc.kill() if force else proc.stop()
            finally:
                self._forget(proc)
                if unprovisioned:
                    self._release_marker(proc.manifest.id)

    def _forget(self, proc: PluginProcess) -> None:
        if proc in self.procs:
            self.procs.remove(proc)
        self.last_used.pop(proc, None)
        for registry in (self.commands, self.providers):
            for key in [k for k, v in registry.items() if v is proc]:
                del registry[key]
        proc.forgotten = True
        for key in [k for k in self.hub.replies if k[0] is proc]:
            del self.hub.replies[key]

    def _release_marker(self, plugin_id: str) -> None:
        """Drop the crash-recovery marker once no live process of this plugin is provisioned."""
        if any(p.manifest.id == plugin_id and p.provisioned for p in self.procs):
            return
        self.store.marker(plugin_id).unlink(missing_ok=True)

    def drain_pending(self, deadline: float) -> bool:
        """Serve the plugins until none owes a reply, or `deadline` (time.monotonic()) passes.

        After `cancel`, the command plugin may answer at once while a provider
        it was waiting on still has to wind down its `call`. That provider is
        well-behaved, so it gets the rest of the grace period. A plugin that
        exited can no longer answer and does not count. Returns True when
        nothing is owed.
        """
        while True:
            if not any(p.pending and p.alive() for p in self.procs):
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            self.hub.pump(remaining)

    def _stop_unresponsive(self) -> None:
        """After cancel and its grace period: a plugin still owing a reply is stuck.

        It would answer the next request late (or never) and leak output into it,
        so it is killed and starts afresh on next use. Replies that nobody waits
        for any more are dropped.
        """
        for proc in list(self.procs):
            if proc.pending:
                if proc.alive():
                    TerminalUI(proc.manifest.id).warn("did not stop on cancel; stopped it")
                self.stop_proc(proc, force=True)
        self.hub.replies.clear()

    def _interrupted(self, ui: TerminalUI) -> None:
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
            self._stop_unresponsive()

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

    def running(self) -> list[tuple[str, float, list[str], str | None, str]]:
        """(id, idle seconds, provided capabilities, container runtime or None, runner) per running plugin."""
        now = time.monotonic()
        return [
            (p.manifest.id, now - self.last_used.get(p, now), list(p.manifest.provides),
             p.launch.runtime, p.launch.runner)
            for p in self.procs
        ]

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
    def invoke(self, manifest: Manifest, command: str, args: list[str]) -> int:
        # Parse first: a usage error (UsageError) must surface before anything starts.
        inputs = parse_for(manifest, command, args)
        ui = TerminalUI(manifest.id, self.verbose)
        proc = self.commands.get(manifest.id)
        try:
            # Bring up required providers first, so a missing one fails before the
            # command starts and its provisioning (e.g. loading a model) happens up front.
            for capability in manifest.requires:
                self.provider_for(capability)
            if proc is not None and not proc.alive():
                self.stop_proc(proc)
                proc = None
            if proc is None:
                proc = self.start(manifest, ui)
                self.commands[manifest.id] = proc

            self.last_used[proc] = time.monotonic()
            params = {"command": command, "args": args, "cwd": os.getcwd()}  # the one prepare() mounts for fs:write:cwd
            if inputs is not None:
                params["inputs"] = inputs
            invoke_id = proc.send_request("invoke", params)
            try:
                result = proc.wait_for(invoke_id, "invoke", timeout=None)
            except KeyboardInterrupt:
                self._interrupted(ui)
                return 130
            finally:
                if proc in self.procs:
                    self.last_used[proc] = time.monotonic()
            return int((result or {}).get("exit_code", 0))

        except KeyboardInterrupt:  # during startup (provider, initialize, provision)
            ui.warn("interrupted")
            return 130
        except PluginError as e:
            if e.code != CANCELLED:
                ui.warn(f"{manifest.id}: {e}")
            for p in list(self.procs):
                if not p.alive():
                    self.stop_proc(p)
            return 1


def run(store: Store, manifest: Manifest, command: str, args: list[str], verbose: bool = False) -> int:
    session = Session(store, verbose)
    try:
        return session.invoke(manifest, command, args)
    finally:
        session.close()


def recover(store: Store) -> None:
    """Clean up plugins that were left provisioned by a crashed run."""
    for marker in store.state_dir.glob("*.provisioned"):
        plugin_id = marker.name.removesuffix(".provisioned")
        ui = TerminalUI(plugin_id)
        try:
            manifest = store.get(plugin_id)
        except ManifestError:
            marker.unlink()  # plugin was removed; nothing we can call
            continue
        ui.warn(f"{plugin_id} was left provisioned by an earlier run, cleaning up")
        try:
            launch = isolation.prepare(store, manifest)
        except ManifestError as e:
            ui.warn(f"cleanup of {plugin_id} failed: {e}")
            continue
        proc = PluginProcess(manifest, store.log_path(plugin_id), ui.handlers(), launch)
        try:
            proc.start()
            proc.request(
                "initialize",
                {"api_version": API_VERSION, "host": {"name": "dock", "version": __version__},
                 "data_dir": str(store.data_dir(plugin_id))},
                timeout=INIT_TIMEOUT,
            )
            proc.request("unprovision", timeout=UNPROVISION_TIMEOUT)
            marker.unlink()
        except (PluginError, ManifestError) as e:
            ui.warn(f"cleanup of {plugin_id} failed: {e}")
        finally:
            proc.stop()
