"""spawn -> initialize -> [provision] -> invoke -> [unprovision] -> shutdown"""

from __future__ import annotations

import json
import os
import time

from dock import API_VERSION, __version__
from dock.manifest import Manifest, ManifestError
from dock.process import CANCELLED, PluginError, PluginProcess
from dock.store import Store
from dock.ui import TerminalUI

INIT_TIMEOUT = 10
PROVISION_TIMEOUT = 120
UNPROVISION_TIMEOUT = 30
CANCEL_GRACE = 5


def _spawn(store: Store, manifest: Manifest, ui: TerminalUI) -> PluginProcess:
    proc = PluginProcess(manifest, store.log_path(manifest.id), ui.handlers())
    proc.start()
    proc.request(
        "initialize",
        {
            "api_version": API_VERSION,
            "host": {"name": "dock", "version": __version__},
            "data_dir": str(store.data_dir(manifest.id)),
        },
        timeout=INIT_TIMEOUT,
    )
    return proc


def run(store: Store, manifest: Manifest, command: str, args: list[str], verbose: bool = False) -> int:
    ui = TerminalUI(manifest.id, verbose)
    marker = store.marker(manifest.id)
    proc: PluginProcess | None = None
    try:
        proc = _spawn(store, manifest, ui)

        if manifest.provision:
            # Marker first: if we die during or after provision, next run cleans up.
            marker.write_text(json.dumps({"version": manifest.version, "since": time.time()}))
            proc.request("provision", timeout=PROVISION_TIMEOUT)

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
            proc.notify("cancel")
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
        if proc is not None:
            if marker.exists():
                try:
                    proc.request("unprovision", timeout=UNPROVISION_TIMEOUT)
                    marker.unlink()
                except PluginError as e:
                    ui.warn(f"unprovision failed ({e}); will retry on next run")
            proc.stop()


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
        proc = None
        try:
            proc = _spawn(store, manifest, ui)
            proc.request("unprovision", timeout=UNPROVISION_TIMEOUT)
            marker.unlink()
        except (PluginError, ManifestError) as e:
            ui.warn(f"cleanup of {plugin_id} failed: {e}")
        finally:
            if proc:
                proc.stop()
