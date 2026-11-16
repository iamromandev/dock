"""The local plugin store under $DOCK_HOME (default ~/.dock).

  plugin/<id>/<version>/    installed plugin files
  index.json                cached manifests -> command lookup without spawning anything
  state/<id>.provisioned    crash-recovery markers
  data/<id>/                each plugin's private data dir
  logs/<id>.log             each plugin's stderr
  history                   shell input history
  config.toml               optional; [providers] picks a plugin per capability,
                            [shell] idle_timeout sets the idle stop (seconds)
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tomllib
from pathlib import Path

from cli.manifest import Manifest, ManifestError


class Store:
    def __init__(self, home: Path | None = None):
        self.home = Path(home or os.environ.get("DOCK_HOME") or Path.home() / ".dock")
        self.plugin_dir = self.home / "plugin"
        self.state_dir = self.home / "state"
        self.data_root = self.home / "data"
        self.logs_dir = self.home / "logs"
        self.index_path = self.home / "index.json"
        self.history_path = self.home / "history"
        self._warned_idle_timeout = False
        for d in (self.plugin_dir, self.state_dir, self.data_root, self.logs_dir):
            d.mkdir(parents=True, exist_ok=True)

    # index ---------------------------------------------------------------
    def index(self) -> dict:
        if not self.index_path.exists():
            return {}
        return json.loads(self.index_path.read_text(encoding="utf-8"))

    def _save_index(self, idx: dict) -> None:
        tmp = self.index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(idx, indent=2), encoding="utf-8")
        tmp.replace(self.index_path)

    # install / remove ----------------------------------------------------
    def install(self, src: Path) -> Manifest:
        manifest = Manifest.load(src)
        manifest.entrypoint()  # fail early if it can't run on this machine

        dest = self.plugin_dir / manifest.id / manifest.version
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(
            manifest.root, dest,
            ignore=shutil.ignore_patterns(".git", "__pycache__", "node_modules", "*.go", "go.mod"),
        )
        installed = Manifest.load(dest)

        idx = self.index()
        idx[installed.id] = {
            "version": installed.version,
            "path": str(dest),
            "description": installed.description,
            "commands": {c.name: c.summary for c in installed.commands},
            "provides": installed.provides,
            "requires": installed.requires,
        }
        self._save_index(idx)
        return installed

    def remove(self, plugin_id: str) -> None:
        idx = self.index()
        if plugin_id not in idx:
            raise ManifestError(f"{plugin_id} is not installed")
        shutil.rmtree(self.plugin_dir / plugin_id, ignore_errors=True)
        del idx[plugin_id]
        self._save_index(idx)

    # lookup ----------------------------------------------------------------
    def get(self, plugin_id: str) -> Manifest:
        entry = self.index().get(plugin_id)
        if entry is None:
            raise ManifestError(f"{plugin_id} is not installed")
        return Manifest.load(Path(entry["path"]))

    def resolve(self, name: str) -> tuple[Manifest, str]:
        """'chat' -> the plugin that owns it. 'dev.dock.chat:chat' picks explicitly."""
        if ":" in name:
            plugin_id, command = name.split(":", 1)
            return self.get(plugin_id), command

        owners = [pid for pid, e in self.index().items() if name in e["commands"]]
        if not owners:
            raise ManifestError(f"unknown command '{name}'. Run 'dock help'.")
        if len(owners) > 1:
            choices = ", ".join(f"{pid}:{name}" for pid in owners)
            raise ManifestError(f"'{name}' is provided by several plugins; use one of: {choices}")
        return self.get(owners[0]), name

    def providers_of(self, capability: str) -> list[str]:
        return sorted(pid for pid, e in self.index().items() if capability in e.get("provides", []))

    def config(self) -> dict:
        path = self.home / "config.toml"
        if not path.exists():
            return {}
        try:
            return tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as e:
            raise ManifestError(f"{path}: {e}") from e

    def idle_timeout(self) -> float:
        """Seconds a shell keeps an idle plugin running; 0 = never stop. Default 300."""
        value = self.config().get("shell", {}).get("idle_timeout", 300)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            if not self._warned_idle_timeout:  # every prompt asks; say it once
                self._warned_idle_timeout = True
                print(f"dock: invalid [shell] idle_timeout {value!r}; using 300", file=sys.stderr)
            return 300
        return value

    def provider(self, capability: str) -> Manifest:
        """The plugin that serves a capability: config.toml choice, else the only one installed."""
        owners = self.providers_of(capability)
        chosen = self.config().get("providers", {}).get(capability)
        if chosen:
            if chosen not in owners:
                raise ManifestError(f"config.toml picks {chosen} for '{capability}', but it does not provide it")
            return self.get(chosen)
        if not owners:
            raise ManifestError(f"no installed plugin provides '{capability}'")
        if len(owners) > 1:
            raise ManifestError(
                f"several plugins provide '{capability}' ({', '.join(owners)}); choose one in "
                f"{self.home / 'config.toml'}:\n  [providers]\n  {capability} = \"{owners[0]}\""
            )
        return self.get(owners[0])

    # per-plugin paths ----------------------------------------------------
    def data_dir(self, plugin_id: str) -> Path:
        d = self.data_root / plugin_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def marker(self, plugin_id: str) -> Path:
        return self.state_dir / f"{plugin_id}.provisioned"

    def log_path(self, plugin_id: str) -> Path:
        return self.logs_dir / f"{plugin_id}.log"
