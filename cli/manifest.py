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

from cli import API_VERSION, http_relay
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
class WasmRun:
    """How to run the plugin under a WASI runtime: its own module, or a script on the shared Python interpreter."""
    module: str | None = None  # path inside the plugin folder
    python: str | None = None  # a Python version dock pins, e.g. "3.15"
    script: str | None = None  # path inside the plugin folder, run by that interpreter
    args: list[str] = field(default_factory=list)


def _inside(path: Path, root: Path, where: str, key: str, value) -> None:
    if not isinstance(value, str) or not value:
        raise ManifestError(f"{where} '{key}' must be a path")
    target = (root / value).resolve()
    if not target.is_relative_to(root):
        raise ManifestError(f"{where} '{key}' must stay inside the plugin folder: {value}")
    if not target.is_file():
        raise ManifestError(f"{where} '{key}' {value} not found in the plugin folder")


def _wasm_run(path: Path, root: Path, spec) -> WasmRun:
    from cli.wasm import PYTHONS  # wasm imports isolation, which imports this module

    where = f"{path}: [run].wasm"
    if not isinstance(spec, dict):
        raise ManifestError(f"{where} must be a table like {{ module = \"...\" }} or {{ python = \"3.15\", script = \"...\" }}")
    unknown = sorted(set(spec) - {"module", "python", "script", "args"})
    if unknown:
        names = ", ".join(repr(k) for k in unknown)
        raise ManifestError(f"{where} has unknown key{'s' if len(unknown) > 1 else ''} {names} "
                            "(known: module, python, script, args)")
    args = spec.get("args", [])
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise ManifestError(f"{where} 'args' must be a list of strings")
    has_module, has_python = "module" in spec, "python" in spec or "script" in spec
    if has_module == has_python:
        raise ManifestError(
            f"{where} needs exactly one form: {{ module = \"...\" }} or {{ python = \"3.15\", script = \"...\" }}")
    if has_module:
        _inside(path, root, where, "module", spec["module"])
        return WasmRun(module=spec["module"], args=list(args))
    if "python" not in spec or "script" not in spec:
        raise ManifestError(f"{where} the python form needs both 'python' and 'script'")
    version = spec["python"]
    if not isinstance(version, str) or version not in PYTHONS:
        raise ManifestError(f"{where} python '{version}' is not one dock provides (known: {', '.join(PYTHONS)})")
    _inside(path, root, where, "script", spec["script"])
    return WasmRun(python=version, script=spec["script"], args=list(args))


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
    env: list[str] = field(default_factory=list)  # host variables passed into the container (not native runs)
    wasm: WasmRun | None = None  # [run].wasm: run it as a WASI module; wins over native and container entries
    http: list[http_relay.HttpRule] = field(default_factory=list)  # [capabilities].http: hosts dock may fetch for it

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

        caps = data.get("capabilities", {})
        if not data.get("commands") and not caps.get("provides"):
            raise ManifestError(f"{path}: a plugin needs at least one command or one provided capability")

        env = _env_names(path, caps)
        try:
            http = http_relay.parse_rules(caps.get("http", []))
        except ValueError as e:
            raise ManifestError(f"{data['id']}: [capabilities].http: {e}") from e

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

        wasm = None
        if "wasm" in data["run"]:
            wasm = _wasm_run(path, root, data["run"]["wasm"])
            if "network" in caps.get("permissions", []):
                raise ManifestError(f"{data['id']}: WASM plugins cannot use 'network'")
            for n in env:  # the guest gets these by value from dock; a declared name would override them
                if n == "PYTHONHOME":
                    raise ManifestError(f"{path}: env entry '{n}' is set by dock already")
                if wasm.python is not None and n.startswith("PYTHON"):
                    raise ManifestError(
                        f"{path}: env entry '{n}' is not allowed with the Python form: "
                        "Python variables would change how the shared interpreter starts"
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
            env=env,
            wasm=wasm,
            http=http,
        )

    def command(self, name: str) -> Command | None:
        return next((c for c in self.commands if c.name == name), None)

    def entrypoint(self) -> list[str]:
        """Pick the run entry for this machine: exact platform first, then 'any'."""
        key = platform_key()
        native = {k: v for k, v in self.run.items() if k not in ("container", "wasm")}  # neither is a native build
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
