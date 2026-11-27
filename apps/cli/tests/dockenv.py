"""Helpers for the tests that start a plugin: they all run in a real container runtime.

No test fakes the runtime. `require_runtime()` never skips; a machine without Docker (or podman) fails the
Docker test classes with one clear message, and the classes that start no plugin do not call it.

  DOCK_TEST_RUNTIME   use this runtime instead of docker/podman from PATH (`/nonexistent` shows the no-runtime
                      failure of `make test`)
"""

from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path
from shutil import which

ROOT = Path(__file__).resolve().parent.parent
NO_RUNTIME = "no container runtime found: start Docker (or install podman) to run these tests"
BUILD_TIMEOUT = 600  # the first build of an image may pull its base

_reachable: dict[str, bool] = {}
_built: set[tuple[str, str]] = set()  # (runtime, image tag): built once per process


def _default_sigint() -> None:
    signal.signal(signal.SIGINT, signal.SIG_DFL)


# Popen/run keyword arguments for every `python -m src` child the tests start. A harness may start the suite with
# SIGINT ignored (a backgrounded job does), and a child inherits that: dock would then never see a Ctrl-C.
SIGINT_DEFAULT: dict = {"preexec_fn": _default_sigint} if os.name == "posix" else {}


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


def recording_runtime(tmp: Path) -> tuple[Path, Path]:
    """(wrapper, calls_log): a runtime that appends each call's argv to the log as a JSON list, then runs the real one.

    Point `[container] runtime` at the wrapper to see exactly what dock asked the runtime to do.
    """
    tmp = Path(tmp)
    real = require_runtime()
    wrapper, calls = tmp / Path(real).name, tmp / "runtime-calls"  # named like the runtime: it labels it
    script = tmp / "record-runtime.py"
    script.write_text(
        "import json, os, sys\n"
        f"with open({str(calls)!r}, 'a') as f:\n"
        "    f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        f"os.execv({real!r}, [{real!r}, *sys.argv[1:]])\n"
    )
    # A fixed interpreter line, never `#!<python>`: a path with a space (or a long one) breaks a shebang.
    wrapper.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(script))} "$@"\n')
    wrapper.chmod(0o755)
    return wrapper, calls


def run_runtime(*args: str, rt: str | None = None) -> subprocess.CompletedProcess:
    """One call of the real runtime, for tests that set up or inspect containers and images directly."""
    return subprocess.run([_rt(rt), *args], capture_output=True, text=True, timeout=120, check=False)


def container_exists(name: str) -> bool:
    """Is there a container (running or not) with exactly this name?"""
    r = run_runtime("ps", "-a", "-q", "--filter", f"name=^{name}$")
    return bool(r.stdout.strip())


def calls_in(log: Path) -> list[list[str]]:
    """The argv lists a recording runtime saw (without the runtime itself)."""
    if not Path(log).exists():
        return []
    return [json.loads(line) for line in Path(log).read_text().splitlines() if line]


def write_config(home: Path, rt: str | Path) -> None:
    """A throwaway DOCK_HOME's config.toml with just the runtime setting; `add_config` adds the rest after it."""
    (Path(home) / "config.toml").write_text(f'[container]\nruntime = "{rt}"\n')


def _rt(rt: str | None) -> str:
    return rt or require_runtime()


def container_running(cid: str, rt: str | None = None) -> bool:
    """Is a container with this id (a prefix is enough: Docker's hostname is the short id) running?"""
    r = subprocess.run(
        [_rt(rt), "ps", "-q", "--no-trunc", "--filter", f"id={cid}"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return bool(r.stdout.strip())


def wait_container_gone(cid: str, timeout: float = 15.0, rt: str | None = None) -> bool:
    end = time.monotonic() + timeout
    while container_running(cid, rt):
        if time.monotonic() >= end:
            return False
        time.sleep(0.1)
    return True


def prebuild(home: Path, plugin_ids: list[str]) -> None:
    """`dock plugin build <id>` for each installed plugin, once per process and image: tests then never wait for a build."""
    env = {**os.environ, "DOCK_HOME": str(home), "PYTHONPATH": str(ROOT)}
    for plugin_id in plugin_ids:
        entry = json.loads((Path(home) / "index.json").read_text())[plugin_id]
        tag = f"dock/{plugin_id}:{entry['version']}"
        key = (require_runtime(), tag)
        if key in _built:
            continue
        r = subprocess.run(
            [sys.executable, "-m", "src", "plugin", "build", plugin_id],
            env=env,
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=BUILD_TIMEOUT,
            check=False,
        )
        if r.returncode != 0:
            raise RuntimeError(f"plugin build {plugin_id} failed: {r.stderr}{r.stdout}")
        _built.add(key)


def add_config(home: Path, text: str) -> None:
    """Add settings to a home's config.toml without losing the runtime that `write_config` put there."""
    with open(Path(home) / "config.toml", "a") as f:
        f.write("\n" + text)


def make_home(prefix: str, *plugin_dirs: Path) -> Path:
    """A throwaway DOCK_HOME with these plugin folders installed and the runtime set in its config.

    The config is written after the installs. A first install asks the runtime nothing, so the order does not
    matter for these homes; a test that reinstalls or removes a plugin must use a copy under its own id
    (`plugin_copy`), because that drops the image and the shared fixture images must never be touched.
    """
    import tempfile

    sys.path.insert(0, str(ROOT))
    from dock_host.store import Store

    home = Path(tempfile.mkdtemp(prefix=prefix))
    store = Store(home)
    for path in plugin_dirs:
        store.install(path)
    rt = runtime()
    if (
        rt
    ):  # a class that starts a plugin has called require_runtime() already, which fails clearly without one
        write_config(home, rt)
    return home


def prebuild_plugins(*plugin_dirs: Path) -> None:
    """For setUpClass: the runtime is there and these plugins' images are built (once per process)."""
    import shutil

    require_runtime()
    home = make_home("dock-prebuild-", *plugin_dirs)
    try:
        sys.path.insert(0, str(ROOT))
        from dock_host.manifest import Manifest

        prebuild(home, [Manifest.load(p).id for p in plugin_dirs])
    finally:
        shutil.rmtree(home, ignore_errors=True)


def plugin_copy(src: Path, dest_parent: Path, plugin_id: str) -> Path:
    """A copy of a plugin folder under another id, for tests that reinstall or remove (and so drop the image of) a plugin.

    The shared fixture images are never deleted by a test: only the images of such copies are.
    """
    import re
    import shutil

    dest = Path(dest_parent) / plugin_id
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__"))
    manifest = dest / "plugin.toml"
    text = manifest.read_text()
    manifest.write_text(re.sub(r'(?m)^id = ".*"$', f'id = "{plugin_id}"', text, count=1))
    return dest


def remove_image(tag: str, rt: str | None = None) -> None:
    """Remove one image by tag; only ever for the images of a test's own plugin copies."""
    subprocess.run([_rt(rt), "image", "rm", "-f", tag], capture_output=True, timeout=60, check=False)
