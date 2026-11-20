"""The local plugin store under $DOCK_HOME (default ~/.dock).

  plugin/<id>/<version>/    installed plugin files
  index.json                cached manifests -> command lookup without spawning anything
  data/<id>/                each plugin's private data dir
  logs/<id>.log             each plugin's stderr
  history                   shell input history
  config.toml               optional; [container] runtime names the container runtime (else docker, then podman),
                            [shell] idle_timeout sets the idle stop (seconds)
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tomllib
from pathlib import Path

from cli import catalog, isolation
from cli.manifest import Manifest, ManifestError, NotAContainerPlugin, check_removed_tables


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


def _make_writable(path: Path) -> None:
    """Give the owner access to a folder tree so it can be listed, deleted or replaced.

    Symlinks are never followed (not even `path` itself): a link could lead outside the store.
    """
    if path.is_symlink() or not path.exists():
        return
    mode = path.stat().st_mode
    if path.is_dir():
        path.chmod(mode | 0o700)  # read and search too: a folder that cannot be listed cannot be emptied
        for entry in path.iterdir():
            _make_writable(entry)
    else:
        path.chmod(mode | 0o200)


class Store:
    def __init__(self, home: Path | None = None):
        self.home = Path(os.path.abspath(home or os.environ.get("DOCK_HOME") or Path.home() / ".dock"))  # never relative: it is mounted
        self.plugin_dir = self.home / "plugin"
        self.data_root = self.home / "data"
        self.logs_dir = self.home / "logs"
        self.index_path = self.home / "index.json"
        self.history_path = self.home / "history"
        self._warned_idle_timeout = False
        for d in (self.plugin_dir, self.data_root, self.logs_dir):
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
        dest = child(child(self.plugin_dir, manifest.id, "plugin id"), manifest.version, "plugin version")
        # The Dockerfile builds from the installed folder, so a plugin keeps all its sources.
        ignore = shutil.ignore_patterns(".git", "__pycache__", "node_modules", "target")  # target: a Rust build's output
        _refuse_escaping_symlinks(manifest.root, ignore)  # before anything is replaced
        if dest.exists():
            _make_writable(dest)  # an old install may have left the folder read-only
            shutil.rmtree(dest)
        shutil.copytree(manifest.root, dest, ignore=ignore, symlinks=True)  # links stay links: never followed
        installed = Manifest.load(dest)

        idx = self.index()
        reinstall = installed.id in idx  # a first install has no image of its own to go stale
        idx[installed.id] = {
            "version": installed.version,
            "path": str(dest),
            "description": installed.description,
            "commands": {c.name: c.summary for c in installed.commands},
            "keywords": {c.name: c.keywords for c in installed.commands if c.keywords},
        }
        self._save_index(idx)
        if reinstall:
            isolation.forget_image(self, installed)
        return installed

    def remove(self, plugin_id: str) -> None:
        idx = self.index()
        if plugin_id not in idx:
            raise ManifestError(f"{plugin_id} is not installed")
        folder = self.plugin_dir / plugin_id
        try:
            installed = Manifest.load(Path(idx[plugin_id]["path"]))  # before the folder goes: names the image to drop
        except (ManifestError, OSError, KeyError):
            installed = None  # damaged or old install: there is nothing to name an image by
        _make_writable(folder)
        try:
            if folder.is_symlink():
                folder.unlink()  # only the link: what it points to is not ours
            else:
                shutil.rmtree(folder)
        except FileNotFoundError:
            pass  # already gone: only the index entry was left
        except OSError as e:
            raise ManifestError(f"cannot remove {folder}: {e}") from e  # the index keeps it: it is still there
        del idx[plugin_id]
        self._save_index(idx)
        if installed is not None:
            isolation.forget_image(self, installed)  # a later install of the same version must not reuse it

    # lookup ----------------------------------------------------------------
    def get(self, plugin_id: str) -> Manifest:
        entry = self.index().get(plugin_id)
        if entry is None:
            raise ManifestError(f"{plugin_id} is not installed")
        try:
            return Manifest.load(Path(entry["path"]))
        except NotAContainerPlugin as e:  # an install from before dock ran only containers
            raise ManifestError(f"{e}; reinstall a container build of it (dock plugin install <folder>)") from e

    def resolve(self, name: str) -> tuple[Manifest, str]:
        """'hello' -> the plugin that owns it. 'dev.dock.hello:hello' picks explicitly."""
        if ":" in name:
            plugin_id, command = name.split(":", 1)
            return self.get(plugin_id), command

        owners = [pid for pid, e in self.index().items() if name in e["commands"]]
        if not owners:
            raise ManifestError(catalog.hint(self, name) or f"unknown command '{name}'. Run 'dock help'.")
        if len(owners) > 1:
            choices = ", ".join(f"{pid}:{name}" for pid in owners)
            raise ManifestError(f"'{name}' is provided by several plugins; use one of: {choices}")
        return self.get(owners[0]), name

    def config(self) -> dict:
        path = self.home / "config.toml"
        if not path.exists():
            return {}
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError, OSError) as e:
            raise ManifestError(f"{path}: {e}") from e
        check_removed_tables(data, path)
        return data

    def idle_timeout(self) -> float:
        """Seconds a shell keeps an idle plugin running; 0 = never stop. Default 300."""
        value = self.config().get("shell", {}).get("idle_timeout", 300)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            if not self._warned_idle_timeout:  # every prompt asks; say it once
                self._warned_idle_timeout = True
                print(f"dock: invalid [shell] idle_timeout {value!r}; using 300", file=sys.stderr)
            return 300
        return value

    # per-plugin paths ----------------------------------------------------
    def data_dir(self, plugin_id: str) -> Path:
        d = child(self.data_root, plugin_id, "plugin id")
        d.mkdir(parents=True, exist_ok=True)
        return d

    def log_path(self, plugin_id: str) -> Path:
        return self.logs_dir / f"{plugin_id}.log"
