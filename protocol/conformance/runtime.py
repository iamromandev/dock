"""The container runtime, and building and running one plugin's image for the conformance checks.

  DOCK_TEST_RUNTIME   use this runtime instead of docker/podman from PATH (`/nonexistent` shows the no-runtime failure)
"""

from __future__ import annotations

import itertools
import os
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from shutil import which

NO_RUNTIME = "no container runtime found: start Docker (or install podman) to run these tests"
BUILD_TIMEOUT = 600  # the first build of an image may pull its base

_reachable: dict[str, bool] = {}
_built: set[tuple[str, str]] = set()  # (runtime, image tag): built once per process
_counter = itertools.count(1)


def runtime() -> str | None:
    """The container runtime the tests use: DOCK_TEST_RUNTIME, else docker, else podman; None when none is found."""
    override = os.environ.get("DOCK_TEST_RUNTIME")
    if override:
        return which(override)
    return which("docker") or which("podman")


def _reaches_daemon(rt: str) -> bool:
    if rt not in _reachable:
        try:
            _reachable[rt] = (
                subprocess.run([rt, "info"], capture_output=True, timeout=60, check=False).returncode == 0
            )
        except OSError, subprocess.TimeoutExpired:
            _reachable[rt] = False
    return _reachable[rt]


def require_runtime() -> str:
    """The runtime's path; raises (an error, never a skip) when there is none or its daemon is down."""
    rt = runtime()
    if rt is None or not _reaches_daemon(rt):
        raise RuntimeError(NO_RUNTIME)
    return rt


@dataclass(frozen=True)
class Manifest:
    id: str
    version: str
    cmd: str
    args: list[str]
    dockerfile: str


def load_manifest(plugin_dir: Path) -> Manifest:
    """The few manifest fields the checks need: id, version and [run].container."""
    data = tomllib.loads((Path(plugin_dir) / "plugin.toml").read_text())
    container = data.get("run", {}).get("container")
    if not data.get("id") or not data.get("version") or not isinstance(container, dict) or "cmd" not in container:
        raise ValueError(f"{plugin_dir}/plugin.toml: needs id, version and [run].container with a cmd")
    return Manifest(
        id=data["id"],
        version=data["version"],
        cmd=container["cmd"],
        args=list(container.get("args", [])),
        dockerfile=container.get("dockerfile", "Dockerfile"),
    )


def image_tag(m: Manifest) -> str:
    return f"dock-conformance/{m.id}:{m.version}"


def build(rt: str, plugin_dir: Path) -> str:
    """Build the plugin's image (once per process and tag); returns the tag."""
    plugin_dir = Path(plugin_dir)
    m = load_manifest(plugin_dir)
    tag = image_tag(m)
    if (rt, tag) not in _built:
        r = subprocess.run(
            [rt, "build", "-t", tag, "-f", str(plugin_dir / m.dockerfile), str(plugin_dir)],
            capture_output=True,
            text=True,
            timeout=BUILD_TIMEOUT,
            check=False,
        )
        if r.returncode != 0:
            raise RuntimeError(f"build of {tag} failed: {r.stderr[-2000:]}{r.stdout[-2000:]}")
        _built.add((rt, tag))
    return tag


def next_name() -> str:
    return f"dock-conformance-{os.getpid()}-{next(_counter)}"


def host_uid_gid() -> tuple[int, int] | None:
    """The user and group the container runs as, as dock does; None where the OS has no uids."""
    return (os.getuid(), os.getgid()) if hasattr(os, "getuid") else None


def run_argv(
    rt: str, m: Manifest, tag: str, name: str, *, data_dir: str, uid_gid: tuple[int, int] | None
) -> list[str]:
    """The `run` command line: the flags dock uses, minus the labels; the data dir is mounted at its own path."""
    user = ["--user", f"{uid_gid[0]}:{uid_gid[1]}"] if uid_gid else []
    return [
        rt, "run", "-i", "--rm", "--pull", "never", "--name", name,
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--read-only", "--tmpfs", "/tmp",
        "--network", "none", "--pids-limit", "256", "--workdir", "/plugin",
        *user, "--mount", f"type=bind,src={data_dir},dst={data_dir}",
        "-e", "HOME=/tmp", "-e", f"DOCK_PLUGIN_ID={m.id}", "-e", "DOCK_API_VERSION=2",
        tag, m.cmd, *m.args,
    ]  # fmt: skip
