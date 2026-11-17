"""Reading plugin.toml. The host learns everything about a plugin from here,
without running any plugin code."""

from __future__ import annotations

import platform
import re
import shutil
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from cli import API_VERSION
from cli.inputs import InputSpec, specs_from_toml


class ManifestError(Exception):
    pass


def platform_key() -> str:
    """e.g. linux-x86_64, darwin-arm64, windows-x86_64"""
    os_name = {"linux": "linux", "darwin": "darwin", "win32": "windows"}.get(sys.platform, sys.platform)
    arch = platform.machine().lower()
    arch = {"amd64": "x86_64", "aarch64": "arm64"}.get(arch, arch)
    return f"{os_name}-{arch}"


@dataclass
class Command:
    name: str
    summary: str = ""
    inputs: list[InputSpec] | None = None  # None = undeclared; [] = declares none


_TAG_PART = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]*")


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
    run: dict
    root: Path
    commands: list[Command] = field(default_factory=list)
    provision: bool = False
    permissions: list[str] = field(default_factory=list)
    provides: list[str] = field(default_factory=list)  # capabilities other plugins can call
    requires: list[str] = field(default_factory=list)  # capabilities this plugin calls
    container: ContainerRun | None = None  # [run].container: how to run it in an isolated container

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
        if "." not in data["id"]:
            raise ManifestError(f"{path}: id must be namespaced, like 'vendor.name'")

        caps = data.get("capabilities", {})
        if not data.get("commands") and not caps.get("provides"):
            raise ManifestError(f"{path}: a plugin needs at least one command or one provided capability")

        commands = []
        for c in data.get("commands", []):
            inputs = None
            if "inputs" in c:
                try:
                    inputs = specs_from_toml(c["name"], c["inputs"])
                except ValueError as e:
                    raise ManifestError(f"{path}: {e}") from e
            commands.append(Command(c["name"], c.get("summary", ""), inputs))

        container = None
        if "container" in data["run"]:
            container = _container_run(path, root, data["run"]["container"])
            for part in ("id", "version"):  # they become the image tag dock/<id>:<version>
                if not _TAG_PART.fullmatch(str(data[part])):
                    raise ManifestError(
                        f"{path}: {part} '{data[part]}' cannot be used with [run].container: "
                        "only letters, digits, '.', '_' and '-', not starting with '.' or '-'"
                    )

        return cls(
            id=data["id"],
            version=str(data["version"]),
            api_version=str(data["api_version"]),
            description=data.get("description", ""),
            run=data["run"],
            root=root,
            commands=commands,
            provision=bool(data.get("lifecycle", {}).get("provision", False)),
            permissions=list(caps.get("permissions", [])),
            provides=list(caps.get("provides", [])),
            requires=list(caps.get("requires", [])),
            container=container,
        )

    def command(self, name: str) -> Command | None:
        return next((c for c in self.commands if c.name == name), None)

    def entrypoint(self) -> list[str]:
        """Pick the run entry for this machine: exact platform first, then 'any'."""
        key = platform_key()
        native = {k: v for k, v in self.run.items() if k != "container"}  # container is not a native build
        if not native:
            raise ManifestError(f"{self.id} has no native build (only a container entry)")
        spec = native.get(key) or native.get("any")
        if spec is None:
            raise ManifestError(f"{self.id} has no build for {key} (has: {', '.join(native)})")

        for tool in spec.get("requires", []):
            if shutil.which(tool) is None:
                raise ManifestError(f"{self.id} needs '{tool}' on PATH")

        cmd = spec["cmd"]
        if "/" in cmd or "\\" in cmd:  # a path inside the plugin, not a tool on PATH
            cmd = str(self.root / cmd)
            if not Path(cmd).exists():
                raise ManifestError(f"{self.id}: {spec['cmd']} not found (did you build it?)")
        return [cmd, *spec.get("args", [])]
