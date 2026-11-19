"""The local plugin store under $DOCK_HOME (default ~/.dock).

  plugin/<id>/<version>/    installed plugin files
  index.json                cached manifests -> command lookup without spawning anything
  state/<id>.provisioned    crash-recovery markers
  data/<id>/                each plugin's private data dir
  logs/<id>.log             each plugin's stderr
  history                   shell input history
  config.toml               optional; [providers] picks a plugin per capability,
                            [shell] idle_timeout sets the idle stop (seconds),
                            [serve] idle_timeout the same for `dock serve`'s warm plugin processes
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tomllib
from pathlib import Path

from cli import isolation, wasm
from cli.manifest import Manifest, ManifestError


def child(root: Path, name: str, what: str) -> Path:
    """`root/name`, refusing a name that is not a plain single component (so never outside `root`)."""
    path = root / name
    if name in ("", ".", "..") or Path(name).name != name or path.parent != root:
        raise ManifestError(f"{what} '{name}' is not a plain name and would leave {root}")
    return path


def _refuse_escaping_symlinks(root: Path, ignore) -> None:
    """Refuse a plugin folder holding a symlink that points outside it: a copy would hand over the host file."""
    real_root = Path(os.path.realpath(root))
    for here, dirs, files in os.walk(root):  # os.walk does not descend into symlinked folders
        skipped = ignore(here, dirs + files)
        dirs[:] = [d for d in dirs if d not in skipped]
        for name in dirs + [f for f in files if f not in skipped]:
            entry = Path(here, name)
            if entry.is_symlink() and not Path(os.path.realpath(entry)).is_relative_to(real_root):
                raise ManifestError(
                    f"{root}: symlink {entry.relative_to(root)} points outside the plugin folder "
                    f"({os.readlink(entry)}); plugin folders must hold their files, not links to the host"
                )


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
        self._warned_serve_idle_timeout = False
        self.isolation_warned: set[str] = set()  # [isolation] plugins entries already reported
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
        if manifest.container is None and manifest.wasm is None:
            manifest.entrypoint()  # fail early if it can't run on this machine
        # With a container or wasm entry the plugin can run without a native build for this machine.

        dest = child(child(self.plugin_dir, manifest.id, "plugin id"), manifest.version, "plugin version")
        # The Dockerfile builds from the installed folder, so a container plugin keeps its sources.
        ignored = (".git", "__pycache__", "node_modules", "target")  # target: a Rust plugin's build output
        if manifest.container is None:
            ignored += ("*.go", "go.mod")
        ignore = shutil.ignore_patterns(*ignored)
        _refuse_escaping_symlinks(manifest.root, ignore)  # before anything is replaced
        if dest.exists():
            wasm.unseal(dest)  # a WASM plugin's folder is read-only
            shutil.rmtree(dest)
        shutil.copytree(manifest.root, dest, ignore=ignore, symlinks=True)  # links stay links: never followed
        installed = Manifest.load(dest)
        if installed.wasm is not None:
            wasm.seal(dest)  # the guest gets the folder through a read-write grant; permissions make it read-only

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
        isolation.forget_image(self, installed)  # after the index: a `plugins` entry for it is then valid
        return installed

    def remove(self, plugin_id: str) -> None:
        idx = self.index()
        if plugin_id not in idx:
            raise ManifestError(f"{plugin_id} is not installed")
        wasm.unseal(self.plugin_dir / plugin_id)
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
        except (tomllib.TOMLDecodeError, UnicodeDecodeError, OSError) as e:
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

    def serve_idle_timeout(self) -> float:
        """Seconds `dock serve` keeps a finished call's plugin process warm; 0 = never stop. Default 300."""
        value = self.config().get("serve", {}).get("idle_timeout", 300)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            if not self._warned_serve_idle_timeout:
                self._warned_serve_idle_timeout = True
                print(f"dock: invalid [serve] idle_timeout {value!r}; using 300", file=sys.stderr)
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
        d = child(self.data_root, plugin_id, "plugin id")
        d.mkdir(parents=True, exist_ok=True)
        return d

    def marker(self, plugin_id: str) -> Path:
        return self.state_dir / f"{plugin_id}.provisioned"

    def log_path(self, plugin_id: str) -> Path:
        return self.logs_dir / f"{plugin_id}.log"
