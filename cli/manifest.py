"""Reading plugin.toml. The host learns everything about a plugin from here,
without running any plugin code."""

from __future__ import annotations

import platform
import shutil
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from cli import API_VERSION


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

        return cls(
            id=data["id"],
            version=str(data["version"]),
            api_version=str(data["api_version"]),
            description=data.get("description", ""),
            run=data["run"],
            root=root,
            commands=[Command(c["name"], c.get("summary", "")) for c in data.get("commands", [])],
            provision=bool(data.get("lifecycle", {}).get("provision", False)),
            permissions=list(caps.get("permissions", [])),
            provides=list(caps.get("provides", [])),
            requires=list(caps.get("requires", [])),
        )

    def entrypoint(self) -> list[str]:
        """Pick the run entry for this machine: exact platform first, then 'any'."""
        key = platform_key()
        spec = self.run.get(key) or self.run.get("any")
        if spec is None:
            raise ManifestError(f"{self.id} has no build for {key} (has: {', '.join(self.run)})")

        for tool in spec.get("requires", []):
            if shutil.which(tool) is None:
                raise ManifestError(f"{self.id} needs '{tool}' on PATH")

        cmd = spec["cmd"]
        if "/" in cmd or "\\" in cmd:  # a path inside the plugin, not a tool on PATH
            cmd = str(self.root / cmd)
            if not Path(cmd).exists():
                raise ManifestError(f"{self.id}: {spec['cmd']} not found (did you build it?)")
        return [cmd, *spec.get("args", [])]
