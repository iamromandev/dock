"""Running a plugin as a WebAssembly (WASI) module under the `wasmtime` command line.

A plugin with `[run].wasm` gets only what its permissions allow: its own folder (read-only through host
file permissions, see `seal`), its data dir, the cwd with `fs:write:cwd`, and declared env names. There
is no network: dock never passes wasmtime a socket option. The stdio protocol is unchanged.
"""

from __future__ import annotations

import difflib
import hashlib
import http.client
import os
import re
import shutil
import stat
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from dataclasses import dataclass, replace

from cli import API_VERSION, isolation
from cli.manifest import Manifest, ManifestError


@dataclass(frozen=True)
class PythonPin:
    """A CPython-for-WASI build dock trusts: fetched once, accepted only if the SHA-256 matches."""
    version: str
    url: str
    sha256: str
    lib: str  # the standard library inside the unpacked archive


PYTHONS = {
    "3.15": PythonPin(
        version="3.15.2",
        url="https://github.com/brettcannon/cpython-wasi-build/releases/download/v3.15.2/python-3.15.2-wasi_sdk-33.zip",
        sha256="b8a63559c56760bbdc310dedb74fd69d10bcbafae26b705bea0ad353ad256888",
        lib="lib/python3.15",
    ),
}

KNOWN_KEYS = ("runtime",)
DEFAULT_RUNTIME = "wasmtime"


def config(store) -> str:
    """The runtime command: `[wasm] runtime` of config.toml, else `wasmtime`.

    An unknown key is an error, like in [isolation]. So is a config.toml that does not parse: unlike
    isolation there is nothing safe to fall back to, as the setting may be the one that did not parse.
    """
    path = store.home / "config.toml"
    where = f"{path}: [wasm]"
    data = store.config()
    if "wasm" not in data:
        _reject_lookalikes(data, path)
    table = data.get("wasm", {})
    if not isinstance(table, dict):
        raise ManifestError(f"{where} must be a table")
    unknown = sorted(set(table) - set(KNOWN_KEYS))
    if unknown:
        names = ", ".join(repr(k) for k in unknown)
        raise ManifestError(
            f"{where} has unknown key{'s' if len(unknown) > 1 else ''} {names} (known: {', '.join(KNOWN_KEYS)})"
        )
    runtime = table.get("runtime", DEFAULT_RUNTIME)
    if not isinstance(runtime, str) or not runtime:
        raise ManifestError(f"{where} runtime must be a command name or path")
    return runtime


def _reject_lookalikes(data: dict, path) -> None:
    """No [wasm] table: refuse what is plainly meant as one (a typo must not mean 'use wasmtime from PATH')."""
    for key in data:
        if key.lower() in KNOWN_KEYS:
            raise ManifestError(f"{path}: '{key}' is at the top level; WASM settings belong in a [wasm] table")
        near = key.strip().lower() == "wasm" or difflib.get_close_matches(key.strip().lower(), ["wasm"], n=1, cutoff=0.75)
        if near:
            raise ManifestError(f"{path}: [{key}] is not a known table; did you mean [wasm]?")


def find_runtime(runtime: str) -> str:
    found = shutil.which(runtime)
    if found is None:
        raise ManifestError(f"WASM runtime '{runtime}' not found (install wasmtime)")
    return found


def guest_env(m: Manifest, home) -> dict[str, str]:
    """The variables dock gives the guest by value: nothing of the host's environment."""
    return {"HOME": str(home), "DOCK_PLUGIN_ID": m.id, "DOCK_API_VERSION": API_VERSION}


def _check_path(m: Manifest, what: str, path) -> str:
    """`::` separates host and guest in --dir, so a path holding it, or ending in ':' (which makes ':::'), cannot be passed."""
    if "::" in str(path):
        raise ManifestError(f"{m.id}: the {what} path contains '::', which a WASM runtime cannot be given: {path}")
    if str(path).endswith(":"):
        raise ManifestError(f"{m.id}: the {what} path ends with ':', which a WASM runtime cannot be given: {path}")
    return str(path)


def run_argv(runtime: str, m: Manifest, *, plugin_root, data_dir, cwd, home, env: dict[str, str],
             passthrough=(), python_root=None) -> list[str]:
    """The `wasmtime run` command line. Never a network option: sockets stay off.

    `env` is set by value; `passthrough` names are given as a bare `--env NAME`, so wasmtime reads the
    value from its own environment and a secret never shows up in the process list. `home` is only
    there for the caller's `env`; it is a path inside data_dir, so no extra grant is needed.
    """
    assert m.wasm is not None
    plugin_root = _check_path(m, "plugin folder", plugin_root)
    data_dir = _check_path(m, "data dir", data_dir)
    if python_root is not None:
        python_root = _check_path(m, "python folder", python_root)
    grant_cwd = "fs:write:cwd" in m.permissions
    if grant_cwd:
        cwd = _check_path(m, "cwd", cwd)
    if python_root is not None:
        program = [f"{python_root}/python.wasm", f"/plugin/{m.wasm.script}"]
    else:
        program = [f"{plugin_root}/{m.wasm.module}"]
    by_value = set(env) | ({"PYTHONHOME"} if python_root is not None else set())
    passthrough = [n for n in passthrough if n not in by_value]  # a bare name after a value would override it
    return [
        runtime, "run",
        "--dir", f"{plugin_root}::/plugin",
        *(["--dir", f"{python_root}::/python"] if python_root is not None else []),
        "--dir", f"{data_dir}::{data_dir}",
        *(["--dir", f"{cwd}::{cwd}"] if grant_cwd else []),
        *[x for k, v in env.items() for x in ("--env", f"{k}={v}")],
        *(["--env", "PYTHONHOME=/python"] if python_root is not None else []),
        *[x for n in passthrough for x in ("--env", n)],
        "--",
        *program,
        *m.wasm.args,
    ]


def _chmod_tree(path: Path, change) -> None:
    """Apply `change(mode) -> mode` to a folder and everything under it; symlinks are left alone."""
    path = Path(path)
    if not path.is_dir() or path.is_symlink():
        return
    for root, dirs, files in os.walk(path, topdown=False):
        for name in (*files, *dirs):
            entry = Path(root, name)
            if not entry.is_symlink():
                os.chmod(entry, change(entry.stat().st_mode))
    os.chmod(path, change(path.stat().st_mode))


def seal(path: Path) -> None:
    """Remove every write bit under `path`: WASI cannot change permissions, so a guest cannot write there."""
    _chmod_tree(path, lambda mode: mode & ~0o222)


def unseal(path: Path) -> None:
    """Give the owner write access again, so dock can delete or replace the folder."""
    _chmod_tree(path, lambda mode: mode | 0o200)


def _first_writable(path: Path) -> Path | None:
    """The first entry of the folder (the folder itself included) with a write bit set; symlinks are not followed."""
    path = Path(path)
    if os.lstat(path).st_mode & 0o222:
        return path
    pending = [path]
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                mode = entry.stat(follow_symlinks=False).st_mode
                if stat.S_ISLNK(mode):
                    continue
                if mode & 0o222:
                    return Path(entry.path)
                if stat.S_ISDIR(mode):
                    pending.append(Path(entry.path))
    return None


def check_sealed(m: Manifest, what: str, path: Path, fix: str) -> None:
    """Fail closed unless a folder the guest gets a read-write grant for is read-only on the host.

    wasmtime has no read-only --dir, so the permission bits are the only thing keeping the guest from
    changing its own code or the shared interpreter. They do not bind root, so root is refused too.
    """
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        raise ManifestError(
            f"{m.id}: refusing to start a WASM plugin as root: permission bits do not make the {what} "
            f"read-only for root, so the guest could change {path}"
        )
    found = _first_writable(path)
    if found is not None:
        raise ManifestError(
            f"{m.id}: the {what} {path} is writable by you ({found}), so the guest could change it; {fix}"
        )


def python_home(store, key: str) -> Path:
    """Where the pinned interpreter for `key` lives once fetched: $DOCK_HOME/wasm/python-<version>/."""
    return store.home / "wasm" / f"python-{PYTHONS[key].version}"


# Test-only: both must be set for them to take effect. They let a test serve a small archive from a local
# server in place of the pinned release; without both, the pinned URL and hash are the only ones used.
TEST_URL_ENV = "DOCK_TEST_WASM_PYTHON_URL"
TEST_SHA_ENV = "DOCK_TEST_WASM_PYTHON_SHA256"

MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024  # the real archive is about 15.5 MB; anything bigger is refused


def effective_pin(key: str) -> PythonPin:
    pin = PYTHONS[key]
    url, sha = os.environ.get(TEST_URL_ENV), os.environ.get(TEST_SHA_ENV)
    if url and sha:
        return replace(pin, url=url, sha256=sha.lower())
    return pin


def effective_root(store, key: str) -> Path:
    """The folder for the interpreter `effective_pin` names: the production one for the pinned hash only.

    Whatever a test override installs goes into a folder of its own, so it can never be taken for the
    pinned interpreter.
    """
    home = python_home(store, key)
    sha = effective_pin(key).sha256
    return home if sha == PYTHONS[key].sha256 else home.with_name(f"{home.name}-test-{sha[:12]}")


def _verified(root: Path, pin: PythonPin) -> bool:
    """A folder is trusted only when its marker records the hash this dock expects (an empty one is not)."""
    try:
        return (root / ".verified").read_text().strip() == pin.sha256
    except OSError:
        return False


def _download(pin: PythonPin, dest: Path) -> None:
    digest = hashlib.sha256()
    size = 0
    cap = MAX_DOWNLOAD_BYTES
    limit = f"{cap // (1 << 20)} MiB" if cap >= 1 << 20 else f"{cap} bytes"
    too_big = ManifestError(f"python {pin.version} for WASI: the download is larger than {limit}")
    with urllib.request.urlopen(pin.url, timeout=60) as response, open(dest, "wb") as out:
        declared = response.headers.get("Content-Length") if hasattr(response, "headers") else None
        if declared and declared.isdigit() and int(declared) > MAX_DOWNLOAD_BYTES:
            raise too_big
        while chunk := response.read(1 << 16):
            size += len(chunk)
            if size > MAX_DOWNLOAD_BYTES:
                raise too_big
            digest.update(chunk)
            out.write(chunk)
    if digest.hexdigest() != pin.sha256:
        raise ManifestError(f"python {pin.version} for WASI: checksum mismatch")


def _extract(archive: Path, dest: Path, version: str) -> None:
    """Unpack `archive` into `dest`; any absolute path, `..` component or symlink aborts before anything is written."""
    with zipfile.ZipFile(archive) as z:
        members = z.infolist()
        for info in members:
            parts = PurePosixPath(info.filename.replace("\\", "/")).parts
            link = stat.S_ISLNK(info.external_attr >> 16)
            if info.filename.startswith(("/", "\\")) or ".." in parts or link or (parts and ":" in parts[0]):
                raise ManifestError(f"python {version} for WASI: unsafe member in the archive: {info.filename!r}")
        z.extractall(dest, members)


_LEFTOVER = re.compile(r".+\.(?:part|tmp)-(\d+)")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:  # PermissionError: it exists, it is someone else's
        return True
    return True


def _sweep_stale(base: Path) -> None:
    """Remove `*.part-<pid>` and `*.tmp-<pid>` that a killed dock left behind (their process is gone)."""
    if not base.is_dir():
        return
    for entry in base.iterdir():
        found = _LEFTOVER.fullmatch(entry.name)
        if not found or int(found.group(1)) == os.getpid() or _pid_alive(int(found.group(1))):
            continue
        if entry.is_dir() and not entry.is_symlink():
            unseal(entry)
            shutil.rmtree(entry, ignore_errors=True)
        else:
            entry.unlink(missing_ok=True)


def ensure_python(store, key: str) -> Path:
    """The verified interpreter folder for `key`, fetched on first use.

    Downloads the pinned archive (at most MAX_DOWNLOAD_BYTES), checks its SHA-256 while streaming, unpacks
    it beside the final folder, seals it and renames it into place; the `.verified` marker holds the hash,
    so a folder is trusted only when it was verified against the hash this dock expects. Whatever goes
    wrong (including Ctrl-C), the temporary files are removed and nothing is installed.
    """
    pin = effective_pin(key)
    root = effective_root(store, key)
    if _verified(root, pin):
        return root
    base = root.parent
    part = base / f"{root.name}.zip.part-{os.getpid()}"
    tmp = base / f"{root.name}.tmp-{os.getpid()}"
    try:
        try:
            base.mkdir(parents=True, exist_ok=True)
            _sweep_stale(base)
            print(f"dock: fetching python {pin.version} for WASI (15.5 MB) \u2026", file=sys.stderr)
            _download(pin, part)
            if tmp.exists():
                unseal(tmp)
                shutil.rmtree(tmp)
            _extract(part, tmp, pin.version)
            for need in ("python.wasm", pin.lib):
                if not (tmp / need).exists():
                    raise ManifestError(f"python {pin.version} for WASI: the archive has no {need}")
            (tmp / ".verified").write_text(pin.sha256)
            seal(tmp)
            if not _verified(root, pin):
                if root.exists():  # not verified for this hash (an older marker, or other content): replace it
                    unseal(root)
                    shutil.rmtree(root)
                try:
                    os.replace(tmp, root)
                except OSError:
                    if not _verified(root, pin):
                        raise
                    # another dock process installed it first: keep theirs, discard ours (cleaned below)
        except ManifestError:
            raise
        except (OSError, http.client.HTTPException, zipfile.BadZipFile) as e:
            if isinstance(e, urllib.error.HTTPError):
                e.close()  # the response body is held open until then
            raise ManifestError(f"python {pin.version} for WASI: {e}") from e
    finally:
        part.unlink(missing_ok=True)
        if tmp.exists():
            unseal(tmp)
            shutil.rmtree(tmp, ignore_errors=True)
    return root


def prepare(store, m: Manifest, cwd: str | None = None) -> isolation.Launch:
    """Decide how to spawn the WASM plugin `m`; refuses (and starts nothing) when it cannot be started safely."""
    assert m.wasm is not None
    runtime = find_runtime(config(store))
    python_root = ensure_python(store, m.wasm.python) if m.wasm.python else None
    check_sealed(m, "plugin folder", m.root, "reinstall the plugin (dock plugin install <folder>) to make it read-only again")
    if python_root is not None:
        check_sealed(m, "interpreter folder", python_root,
                     f"delete {python_root} (make it writable first: chmod -R u+w) and start the plugin again to fetch it anew")
    cwd = cwd or os.getcwd()
    if "fs:write:cwd" in m.permissions:
        isolation._check_cwd_mount(store, m, cwd)
    data_dir = store.data_dir(m.id)
    home = data_dir / ".home"
    home.mkdir(exist_ok=True)
    argv = run_argv(
        runtime, m, plugin_root=m.root, data_dir=data_dir, cwd=cwd, home=home,
        env=guest_env(m, home), passthrough=isolation.passthrough_env(m), python_root=python_root,
    )
    # wasmtime keeps the host environment (it resolves the value-free `--env NAME` entries from it);
    # only the --env entries above reach the guest.
    return isolation.Launch(argv, dict(os.environ), runner="wasm")


def runtime_label(runtime: str) -> str:
    """How the runtime is named to the user: `wasmtime`, even when configured as /opt/wt/bin/wasmtime."""
    return Path(runtime).name


def runner_line(store, m: Manifest) -> str:
    """The `runner:` value of `plugin info` for a WASM plugin; asks nothing and starts nothing."""
    assert m.wasm is not None
    try:
        runtime = runtime_label(config(store))
    except ManifestError as e:
        return f"wasm (unknown: {e})"
    return f"wasm ({runtime}, python {m.wasm.python})" if m.wasm.python else f"wasm ({runtime})"
