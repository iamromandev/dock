"""spawn -> initialize -> [provision] -> invoke -> [unprovision] -> shutdown

One dock command is one Session. The session starts the plugin that owns the
command, plus one provider plugin per capability it requires (for example chat
requires `llm`, served by the llm plugin). The host relays every call between
them, so plugins never talk to each other directly. At the end, everything
the session started is unprovisioned and stopped, newest first.
"""

from __future__ import annotations

import json
import os
import time

from cli import API_VERSION, __version__
from cli.manifest import Manifest, ManifestError
from cli.process import CANCELLED, Hub, PluginError, PluginProcess
from cli.store import Store
from cli.ui import TerminalUI

INIT_TIMEOUT = 10
PROVISION_TIMEOUT = 120
UNPROVISION_TIMEOUT = 30
CANCEL_GRACE = 5


class Session:
    def __init__(self, store: Store, verbose: bool = False):
        self.store = store
        self.verbose = verbose
        self.hub = Hub()                             # all plugins of the session share one inbox
        self.procs: list[PluginProcess] = []        # start order
        self.providers: dict[str, PluginProcess] = {}  # capability -> running provider
        self._active: list[str] = []                # capabilities mid-call, to catch cycles

    # starting plugins ------------------------------------------------------
    def start(self, manifest: Manifest, ui: TerminalUI) -> PluginProcess:
        proc = PluginProcess(manifest, self.store.log_path(manifest.id), ui.handlers(), self.hub)
        proc.handlers["call"] = lambda params: self._call(proc, params)
        proc.start()
        self.procs.append(proc)
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
            proc.request("provision", timeout=PROVISION_TIMEOUT)
        return proc

    def provider_for(self, capability: str) -> PluginProcess:
        proc = self.providers.get(capability)
        if proc is None:
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
        provider.handlers["event"] = lambda p: caller.notify(
            "call_event", {"capability": capability, "data": p.get("data")}
        )
        self._active.append(capability)
        try:
            return provider.request(
                "call",
                {"capability": capability, "method": params.get("method"), "params": params.get("params") or {}},
            )
        finally:
            self._active.pop()

    # stopping ------------------------------------------------------------------
    def cancel_all(self) -> None:
        for proc in self.procs:
            try:
                proc.notify("cancel")
            except PluginError:
                pass

    def close(self) -> None:
        for proc in reversed(self.procs):
            marker = self.store.marker(proc.manifest.id)
            if marker.exists():
                try:
                    proc.request("unprovision", timeout=UNPROVISION_TIMEOUT)
                    marker.unlink()
                except PluginError as e:
                    TerminalUI(proc.manifest.id).warn(f"unprovision failed ({e}); will retry on next run")
            proc.stop()


def run(store: Store, manifest: Manifest, command: str, args: list[str], verbose: bool = False) -> int:
    ui = TerminalUI(manifest.id, verbose)
    session = Session(store, verbose)
    try:
        # Bring up required providers first, so a missing one fails before the
        # command starts and its provisioning (e.g. loading a model) happens up front.
        for capability in manifest.requires:
            session.provider_for(capability)
        proc = session.start(manifest, ui)

        invoke_id = next(proc._ids)
        proc._send({
            "id": invoke_id,
            "method": "invoke",
            "params": {"command": command, "args": args, "cwd": os.getcwd()},
        })
        try:
            result = proc.wait_for(invoke_id, "invoke", timeout=None)
        except KeyboardInterrupt:
            ui.warn("interrupted, cancelling…")
            session.cancel_all()
            try:
                proc.wait_for(invoke_id, "invoke", timeout=CANCEL_GRACE)
            except (PluginError, KeyboardInterrupt):
                pass
            return 130
        return int((result or {}).get("exit_code", 0))

    except PluginError as e:
        if e.code != CANCELLED:
            ui.warn(f"{manifest.id}: {e}")
        return 1
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
        proc = PluginProcess(manifest, store.log_path(plugin_id), ui.handlers())
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
