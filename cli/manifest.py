"""Reading plugin.toml. The host learns everything about a plugin from here,
without running any plugin code."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from cli import API_VERSION
from cli.inputs import InputSpec, specs_from_toml


class ManifestError(Exception):
    pass


class NotAContainerPlugin(ManifestError):
    """A `[run]` table with no `container` entry, or with an entry for something other than a container."""


REMOVED_TABLES = {  # config.toml tables of features dock no longer has: no plugin may be started with one
    "isolation": "[isolation] was replaced by [container] runtime = \"docker\" (or podman, or a path)",
    "wasm": "[wasm] is no longer supported: dock runs plugins only in a container",
    "http": "[http] is no longer supported: HTTP through dock was removed",
    "providers": "[providers] is no longer supported: capabilities were removed",
}


def check_removed_tables(data: dict, path) -> None:
    for table, message in REMOVED_TABLES.items():
        if table in data:
            raise ManifestError(f"{path}: {message}")


@dataclass
class Command:
    name: str
    summary: str = ""
    inputs: list[InputSpec] | None = None  # None = undeclared; [] = declares none


_PLUGIN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")  # one path component: it names folders under the store
_TAG_PART = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]*")
ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]*")
RESERVED_ENV = {"HOME", "PATH", "LANG", "TZ", "DOCK_PLUGIN_ID", "DOCK_API_VERSION"}  # dock sets these itself


def _env_names(path: Path, caps: dict) -> list[str]:
    """[capabilities].env: host variables a plugin asks to receive in its container, de-duplicated in order."""
    names = caps.get("env", [])
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise ManifestError(f"{path}: [capabilities].env must be a list of names")
    for n in names:
        if not ENV_NAME.fullmatch(n):
            raise ManifestError(f"{path}: env entry '{n}' must look like an environment variable name (A-Z, 0-9, _)")
        if n in RESERVED_ENV:
            raise ManifestError(f"{path}: env entry '{n}' is set by dock already")
    return list(dict.fromkeys(names))


@dataclass
class ContainerRun:
    """How to run the plugin inside the image built from its Dockerfile."""
    cmd: str
    args: list[str] = field(default_factory=list)
    dockerfile: str = "Dockerfile"  # relative to the plugin root, must stay inside it


def _container_run(path: Path, root: Path, spec) -> ContainerRun:
    where = f"{path}: [run].container"
    if not isinstance(spec, dict):
        raise ManifestError(f"{where} must be a table like {{ cmd = \"...\" }}")
    cmd = spec.get("cmd")
    if not isinstance(cmd, str) or not cmd:
        raise ManifestError(f"{where} needs a 'cmd'")
    args = spec.get("args", [])
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise ManifestError(f"{where} 'args' must be a list of strings")
    dockerfile = spec.get("dockerfile", "Dockerfile")
    if not isinstance(dockerfile, str) or not dockerfile:
        raise ManifestError(f"{where} 'dockerfile' must be a path")
    if not (root / dockerfile).resolve().is_relative_to(root):
        raise ManifestError(f"{where} 'dockerfile' must stay inside the plugin folder: {dockerfile}")
    return ContainerRun(cmd, list(args), dockerfile)


@dataclass
class Manifest:
    id: str
    version: str
    api_version: str
    description: str
    root: Path
    container: ContainerRun  # [run].container: how to run it in the container built from its Dockerfile
    commands: list[Command] = field(default_factory=list)
    permissions: list[str] = field(default_factory=list)
    env: list[str] = field(default_factory=list)  # host variables passed into the container, by name

    @classmethod
    def load(cls, root: Path) -> Manifest:
        root = Path(root).resolve()
        path = root / "plugin.toml"
        if not path.is_file():
            raise ManifestError(f"no plugin.toml in {root}")
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as e:
            raise ManifestError(f"{path}: {e}") from e

        for key in ("id", "version", "api_version", "run"):
            if key not in data:
                raise ManifestError(f"{path}: missing '{key}'")
        if str(data["api_version"]) != API_VERSION:
            raise ManifestError(
                f"{data['id']} needs protocol v{data['api_version']}, this dock speaks v{API_VERSION}"
            )
        if not isinstance(data["id"], str) or "." not in data["id"]:
            raise ManifestError(f"{path}: id must be namespaced, like 'vendor.name'")
        if not _PLUGIN_ID.fullmatch(data["id"]):
            raise ManifestError(
                f"{path}: id '{data['id']}' may only hold letters, digits, '.', '_' and '-', "
                "and must start with a letter or digit (it names folders in the store)"
            )

        run = data["run"]
        if not isinstance(run, dict):
            raise ManifestError(f"{path}: [run] must be a table")
        only = "dock runs plugins only in a container ([run].container)"
        if "wasm" in run:
            raise NotAContainerPlugin(f"{data['id']}: [run].wasm: WASM plugins are no longer supported; {only}")
        for key in run:  # before `container` is read: `any` next to `container` is refused, not quietly accepted
            if key != "container":
                raise NotAContainerPlugin(f"{data['id']}: [run].{key}: native plugins are no longer supported; {only}")
        if "container" not in run:
            raise NotAContainerPlugin(f"{data['id']}: [run].container is required: dock runs plugins only in a container")

        caps = data.get("capabilities", {})
        if "provision" in data.get("lifecycle", {}):
            raise ManifestError(f"{data['id']}: [lifecycle].provision is no longer supported")
        for key in ("provides", "requires"):
            if key in caps:
                raise ManifestError(f"{data['id']}: [capabilities].{key}: capabilities are no longer supported")
        if not data.get("commands"):
            raise ManifestError(f"{path}: a plugin needs at least one command")

        env = _env_names(path, caps)
        if "http" in caps:
            raise ManifestError(f"{data['id']}: [capabilities].http: HTTP through dock is no longer supported")

        commands = []
        for c in data.get("commands", []):
            if "requires_permission" in c:
                raise ManifestError(f"{data['id']}: {c['name']}: requires_permission is no longer supported")
            inputs = None
            if "inputs" in c:
                try:
                    inputs = specs_from_toml(c["name"], c["inputs"])
                except ValueError as e:
                    raise ManifestError(f"{path}: {e}") from e
            commands.append(Command(c["name"], c.get("summary", ""), inputs))

        container = _container_run(path, root, run["container"])
        for part in ("id", "version"):  # they become the image tag dock/<id>:<version>
            if not _TAG_PART.fullmatch(str(data[part])):
                raise ManifestError(
                    f"{path}: {part} '{data[part]}' cannot be used with [run].container: "
                    "only letters, digits, '.', '_' and '-', not starting with '.' or '-'"
                )

        if "network:listen" in caps.get("permissions", []):
            raise ManifestError(f"{data['id']}: permission 'network:listen' is no longer supported")

        return cls(
            id=data["id"],
            version=str(data["version"]),
            api_version=str(data["api_version"]),
            description=data.get("description", ""),
            root=root,
            container=container,
            commands=commands,
            permissions=list(caps.get("permissions", [])),
            env=env,
        )

    def command(self, name: str) -> Command | None:
        return next((c for c in self.commands if c.name == name), None)
