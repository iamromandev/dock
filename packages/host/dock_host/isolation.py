"""Running a plugin in a container: the only way dock starts one.

The runtime (docker, podman, ...) is driven only through its command line. The container gets what the
plugin's declared permissions allow and nothing else; the stdio protocol is plain, because `<runtime> run -i`
forwards stdin/stdout.
"""

from __future__ import annotations

import difflib
import itertools
import os
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path

from dock_host import API_VERSION
from dock_host.manifest import Manifest, ManifestError

LABEL = "dev.dock.owner"            # value: <hostname>:<pid> of the dock process that owns the container
ENV_ALLOW = ("LANG", "TZ")  # the only host variables a container sees (never PATH: the image has its own)
REMOVE_TIMEOUT = 10
SWEEP_TIMEOUT = 5   # all of the startup sweep, so a stuck runtime cannot hold up every dock command

_names = itertools.count(1)


@dataclass(frozen=True)
class Config:
    runtime: str | None = None  # as written in [container] runtime; None = docker, else podman, from PATH


KNOWN_KEYS = ("runtime",)
_NAME = "container"
NO_RUNTIME = ("no container runtime found (install docker or podman, "
              "or set [container] runtime in {config_path})")


def _looks_like_container(name: str) -> bool:
    """`container`, however it is cased, or a close misspelling of it."""
    n = name.strip().lower()
    return n == _NAME or bool(difflib.get_close_matches(n, [_NAME], n=1, cutoff=0.75))


def config(store) -> Config:
    """The [container] table of config.toml; Config() when there is none (the runtime is then found on PATH).

    Fails closed: a config.toml that does not parse, a table of a removed feature, an unknown key or a
    misspelt table is an error, never a quiet fallback to some other runtime.
    """
    path = store.home / "config.toml"
    data = store.config()  # a file that does not parse, or a removed table, raises here
    where = f"{path}: [container]"
    table = data.get(_NAME)
    if table is None:
        _reject_lookalikes(data, path)
        return Config()
    if not isinstance(table, dict):
        raise ManifestError(f"{where} must be a table")
    unknown = sorted(set(table) - set(KNOWN_KEYS))
    if unknown:
        names = ", ".join(repr(k) for k in unknown)
        raise ManifestError(
            f"{where} has unknown key{'s' if len(unknown) > 1 else ''} {names} "
            f"(known: {', '.join(KNOWN_KEYS)})"
        )
    runtime = table.get("runtime")
    if runtime is not None and (not isinstance(runtime, str) or not runtime):
        raise ManifestError(f"{where} runtime must be a command name or path")
    return Config(runtime)


def _reject_lookalikes(data: dict, path) -> None:
    """No [container] table: refuse what is plainly meant as one (a typo must not mean 'the default runtime')."""
    for key in data:
        if key.lower() in KNOWN_KEYS:
            raise ManifestError(f"{path}: '{key}' is at the top level; container settings belong in a [container] table")
        if key != _NAME and _looks_like_container(key):
            raise ManifestError(f"{path}: [{key}] is not a known table; did you mean [container]?")


@dataclass
class Launch:
    """What to spawn for a plugin: argv, the environment of that process, and the container to clean up."""
    argv: list[str]
    env: dict[str, str]
    runtime: str     # the container runtime
    container: str   # the container's name, for `rm -f`


def image_tag(m: Manifest) -> str:
    return f"dock/{m.id}:{m.version}"


def container_name(m: Manifest) -> str:
    return f"dock-{m.id.replace('.', '-')}-{os.getpid()}-{next(_names)}"


def container_env(m: Manifest) -> dict[str, str]:
    """The plugin's whole environment inside the container: the allowlist, never the rest of the host's."""
    env = {k: os.environ[k] for k in ENV_ALLOW if k in os.environ}
    return env | {"HOME": "/tmp", "DOCK_PLUGIN_ID": m.id, "DOCK_API_VERSION": API_VERSION}


def passthrough_env(m: Manifest) -> list[str]:
    """The declared variables that are set on the host: handed to the runtime by name, never by value."""
    return [n for n in m.env if n in os.environ]


def owner_label() -> str:
    """Who owns a container: this dock process, on this machine (a pid means nothing on another host)."""
    return f"{socket.gethostname()}:{os.getpid()}"


MOUNT_UNSAFE = (":", ",", '"')  # they would end or split the fields of `--mount type=bind,src=...,dst=...`


def _mountable(m: Manifest, path, what: str) -> str:
    """The path as a bind-mount source; refused when a character of it would be read as part of the mount option."""
    text = str(path)
    bad = next((c for c in MOUNT_UNSAFE if c in text), None)
    if bad:
        raise ManifestError(f"{m.id}: cannot mount the {what} {text}: its path contains '{bad}'; use a path without it")
    return text


def _bind(path: str, readonly: bool = False) -> list[str]:
    return ["--mount", f"type=bind,src={path},dst={path}{',readonly' if readonly else ''}"]


def _folder_mounts(m: Manifest, cwd, in_dir) -> list[str]:
    """The cwd (read-write, fs:write:cwd) and in folder (read-only, fs:read:in) binds.

    The in folder is skipped when it is the cwd and the cwd is already mounted read-write. Docker sorts mounts
    by destination itself, so a nested mount shadows its parent whatever the order; the depth sort here is
    defensive, for runtimes that apply them in the order given, so each folder keeps its own mode.
    """
    mounts: list[tuple[str, bool]] = []
    if "fs:write:cwd" in m.permissions:
        mounts.append((_mountable(m, cwd, "current directory (fs:write:cwd)"), False))
    if in_dir is not None and "fs:read:in" in m.permissions:
        path = _mountable(m, in_dir, "in folder (fs:read:in)")
        if not (mounts and _same_path(path, mounts[0][0])):
            mounts.append((path, True))
    mounts.sort(key=lambda x: len(Path(x[0]).parts))  # stable: equal depth keeps cwd first
    return [a for path, ro in mounts for a in _bind(path, ro)]


def _same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def run_argv(runtime: str, m: Manifest, *, name: str, data_dir, cwd, uid_gid, env: dict[str, str],
             passthrough=(), work_dir=None, in_dir=None) -> list[str]:
    """The `run` command line. uid_gid is None where there is no POSIX user (Windows).

    `env` is set by value; `passthrough` names are given as a bare `-e NAME`, so the runtime client reads
    the value from its own environment and a secret never shows up in the process list.
    `work_dir` (a flow's work folder) is mounted read-write at the same path for any plugin.
    `in_dir` is mounted read-only at the same path for a plugin with fs:read:in.
    """
    return [
        runtime, "run", "-i", "--rm", "--pull", "never", "--name", name, "--label", f"{LABEL}={owner_label()}",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--read-only", "--tmpfs", "/tmp",
        *(["--user", f"{uid_gid[0]}:{uid_gid[1]}"] if uid_gid else []),
        *(["--network", "none"] if "network" not in m.permissions else []),
        *_bind(_mountable(m, data_dir, "data directory")),
        *_folder_mounts(m, cwd, in_dir),
        *(_bind(_mountable(m, work_dir, "work folder")) if work_dir is not None else []),
        "--workdir", "/plugin",
        *[x for k, v in env.items() for x in ("-e", f"{k}={v}")],
        *[x for n in passthrough for x in ("-e", n)],
        image_tag(m), m.container.cmd, *m.container.args,
    ]


def resolve_runtime(cfg: Config, store=None) -> str:
    """The container runtime to call: the configured one (an absolute path, or a name looked up on PATH), else docker, else podman.

    Always an absolute path. A relative one would be resolved by Popen against whatever folder it runs in, and a
    plugin folder could then ship its own `docker`. Asks the runtime nothing. `store` only names the config file
    in the message for a missing runtime.
    """
    if cfg.runtime:
        if not os.path.isabs(cfg.runtime) and os.path.basename(cfg.runtime) != cfg.runtime:
            raise ManifestError(
                f"container runtime '{cfg.runtime}' is a relative path: use an absolute path or a command name"
            )
        found = shutil.which(cfg.runtime)
        if found is None:
            raise ManifestError(f"container runtime '{cfg.runtime}' not found")
        return os.path.abspath(found)  # PATH may hold `.` or an empty entry: which() then answers relatively
    for name in ("docker", "podman"):
        found = shutil.which(name)
        if found:
            return os.path.abspath(found)
    config_path = store.home / "config.toml" if store is not None else "~/.dock/config.toml"
    raise ManifestError(NO_RUNTIME.format(config_path=config_path))


def _resolved(store) -> Config:
    """The [container] settings with the runtime resolved to a path."""
    cfg = config(store)
    return replace(cfg, runtime=resolve_runtime(cfg, store))


def ensure_image(cfg: Config, store, m: Manifest, force: bool = False) -> None:
    """Build the plugin's image unless it exists (or always, with force). Output goes to the plugin log."""
    tag = image_tag(m)
    if not force:
        try:
            probe = subprocess.run([cfg.runtime, "image", "inspect", tag], capture_output=True, check=False)
        except OSError as e:
            raise ManifestError(f"container runtime '{cfg.runtime}' failed: {e}") from e
        if probe.returncode == 0:
            return

    log_path = store.log_path(m.id)
    store.notice(f"dock: building image {tag} …")
    with open(log_path, "a", encoding="utf-8") as log:
        log.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} build {tag}\n")
        log.flush()
        try:
            code = subprocess.run(
                [cfg.runtime, "build", "-t", tag, "-f", str(m.root / m.container.dockerfile), str(m.root)],
                stdout=log, stderr=subprocess.STDOUT, check=False,
            ).returncode
        except OSError as e:
            log.write(f"cannot run {cfg.runtime}: {e}\n")
            code = 1
    if code != 0:
        raise ManifestError(f"{m.id}: image build failed (see {log_path})")


def _require_dockerfile(m: Manifest) -> None:
    if not (m.root / m.container.dockerfile).is_file():
        raise ManifestError(f"{m.id}: the plugin has no Dockerfile ({m.container.dockerfile}): add one and reinstall it")


def forget_image(store, m: Manifest) -> None:
    """Best effort: drop the image of a plugin that was just (re)installed or removed, so a later run rebuilds it.

    The tag is id:version, so reinstalling the same version (new sources, same number) would otherwise
    keep running the stale image. A failure is not the install's problem; when no runtime can be asked at all,
    one line on stderr says the image may be stale and how to rebuild it.
    """
    tag = image_tag(m)
    stale = f"dock: old image {tag} may be stale ({{why}}); fix the runtime, then run: dock plugin build {m.id}"
    try:
        runtime = resolve_runtime(config(store), store)
    except ManifestError as e:
        store.notice(stale.format(why=str(e).replace("\n", " ")))
        return
    try:
        subprocess.run([runtime, "image", "rm", "-f", tag], capture_output=True, timeout=REMOVE_TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        store.notice(stale.format(why=f"{runtime_label(runtime)} did not answer: {e}"))


def runtime_label(runtime: str) -> str:
    """How the runtime is named to the user: `docker`, even when configured as /usr/bin/docker."""
    return Path(runtime).name


def build(store, m: Manifest) -> str:
    """`dock plugin build`: (re)build the plugin's image. Returns the tag."""
    cfg = _resolved(store)
    _require_dockerfile(m)
    ensure_image(cfg, store, m, force=True)
    return image_tag(m)


def describe(store, m: Manifest) -> str:
    """How a plugin would run, for `plugin info`; asks the runtime nothing."""
    try:
        cfg = config(store)
    except ManifestError as e:
        return f"unknown ({e})"
    try:
        runtime = resolve_runtime(cfg, store)
    except ManifestError:
        return "none found (install docker or podman, or set [container] runtime)"
    net = "network" if "network" in m.permissions else "no network"
    cwd = "cwd" if "fs:write:cwd" in m.permissions else "no cwd access"
    return f"{runtime_label(runtime)} ({net}, {cwd})"


def _same(a: Path, b: Path) -> bool:
    """Do these name one directory? `samefile` sees through case-insensitive filesystems (`/users/me` is `/Users/me`)."""
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.normcase(a) == os.path.normcase(b)


def _chain(path: Path) -> list[Path]:
    """The path and every folder above it, up to the root."""
    return [path, *path.parents]


def _mount_refusal(store, path) -> str | None:
    """Why `path` is too broad to mount read-write (symlinks and `..` resolved first), or None when it is fine."""
    real = Path(os.path.realpath(path))
    home = Path(os.path.realpath(Path.home()))
    dock_home = Path(os.path.realpath(store.home))
    if _same(real, Path(real.anchor)):
        return "the filesystem root"
    if _same(real, home):
        return "your home directory"
    if any(_same(real, d) for d in _chain(dock_home)) or any(_same(d, dock_home) for d in _chain(real)):
        return f"inside or above the dock home ({store.home}), which holds every plugin's data"
    if any(_same(real, d) for d in home.parents):
        return "above your home directory"
    return None


def check_mount(store, path, source: str, mode: str = "read-write") -> None:
    """Refuse a folder (named by `source`, e.g. `--out`) that would hand over more than a project directory.

    `mode` is how it would be mounted (`read-write` for the out folder, `read-only` for the in folder), for the message."""
    reason = _mount_refusal(store, path)
    if reason:
        raise ManifestError(f"{source} {path}: refusing to mount it {mode}: it is {reason}")


def _check_cwd_mount(store, m: Manifest, cwd: str) -> None:
    """The cwd is mounted read-write, so refuse one that would hand over more than a project directory."""
    reason = _mount_refusal(store, cwd)
    if reason:
        raise ManifestError(
            f"{m.id}: refusing to mount {cwd} read-write (fs:write:cwd): it is {reason}; "
            "run dock from a project directory"
        )


def in_folder(store, m: Manifest, cwd) -> Path | None:
    """The folder a plugin with fs:read:in reads: --in / [paths] in, else the out folder (cwd); None without the permission.

    Checked again here, at launch, against what the path is now (symlinks resolved): it may have changed since
    `paths.resolve` looked at it. The realpath checked here is the path that is mounted; the runtime resolves it
    once more when it starts the container (the same model as the cwd mount), so a swap in between is not caught.
    """
    if "fs:read:in" not in m.permissions:
        return None
    dirs = getattr(store, "dirs", None)
    chosen = dirs.in_dir if dirs is not None and dirs.in_dir is not None else None
    source = dirs.in_source if chosen is not None else "the current folder"
    real = Path(os.path.realpath(chosen if chosen is not None else cwd))
    if not real.is_dir():
        raise ManifestError(f"{m.id}: {source} {real}: not an existing folder (fs:read:in)")
    reason = _mount_refusal(store, real)
    if reason:
        raise ManifestError(
            f"{m.id}: {source} {real}: refusing to mount it read-only (fs:read:in): it is {reason}; "
            "choose a project folder with --in, or run dock from one"
        )
    return real


def prepare(store, m: Manifest, cwd: str | None = None, work_dir=None) -> Launch:
    """Decide how to spawn `m`: in a container, building its image first if needed."""
    cfg = _resolved(store)
    _require_dockerfile(m)
    cwd = cwd or os.getcwd()
    data_dir = store.data_dir(m.id)
    _mountable(m, data_dir, "data directory")  # refused before the image is built or anything is started
    if "fs:write:cwd" in m.permissions:
        _check_cwd_mount(store, m, cwd)
        _mountable(m, cwd, "current directory (fs:write:cwd)")
    in_dir = in_folder(store, m, cwd)
    if in_dir is not None:
        _mountable(m, in_dir, "in folder (fs:read:in)")
    if work_dir is not None:
        _mountable(m, work_dir, "work folder")
    ensure_image(cfg, store, m)

    name = container_name(m)
    argv = run_argv(
        cfg.runtime, m, name=name, data_dir=data_dir, cwd=cwd,
        uid_gid=(os.getuid(), os.getgid()) if hasattr(os, "getuid") else None,
        env=container_env(m), passthrough=passthrough_env(m), work_dir=work_dir, in_dir=in_dir,
    )
    # The runtime client keeps the host environment (it needs DOCKER_HOST and friends, and resolves the
    # value-free `-e NAME` entries from it); only the -e entries above reach the plugin.
    return Launch(argv, dict(os.environ), runtime=cfg.runtime, container=name)


def remove(launch: Launch, log=None, timeout: float = REMOVE_TIMEOUT) -> None:
    """Best effort `rm -f` of the launch's container, so it does not outlive its process."""
    try:
        r = subprocess.run(
            [launch.runtime, "rm", "-f", launch.container],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
        # With --rm the container is usually gone already; that is not worth reporting.
        if r.returncode != 0 and "No such container" not in r.stderr:
            _note(log, f"rm -f {launch.container} failed: {r.stderr.strip()}")
    except (OSError, subprocess.TimeoutExpired) as e:
        _note(log, f"rm -f {launch.container} failed: {e}")


def _note(log, text: str) -> None:
    if log is not None:
        try:
            log.write(f"dock: {text}\n")
            log.flush()
        except (OSError, ValueError):
            pass


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # e.g. not ours to signal, but it exists
    return True


def _owner_pid(owner: str) -> int | None:
    """The pid in a `<hostname>:<pid>` label of a container of this machine; None for anything else."""
    host, sep, pid = owner.strip().rpartition(":")
    if not sep or host != socket.gethostname() or not pid.isascii() or not pid.isdigit():
        return None  # another host, an older label format, or not ours at all: not ours to judge
    n = int(pid)
    return n if 0 < n < 2**31 else None  # os.kill(0, ...) hits a process group; huge values raise


def sweep(store) -> list[str]:
    """Remove containers owned by dock processes that no longer exist; returns their names.

    Runs at every start, so it is cheap and capped: no runtime to ask means no call at all (the runtime
    is the configured one, else docker or podman from PATH), and the whole sweep gets SWEEP_TIMEOUT seconds.
    """
    if sys.platform == "win32":
        return []  # os.kill(pid, 0) does not probe a process there; never judge liveness
    try:
        runtime = resolve_runtime(config(store), store)
    except ManifestError:
        return []  # a damaged config or no runtime: reported when a plugin is started, not here
    deadline = time.monotonic() + SWEEP_TIMEOUT
    try:
        listing = subprocess.run(
            [runtime, "ps", "-a", "--filter", f"label={LABEL}",
             "--format", f'{{{{.Names}}}} {{{{.Label "{LABEL}"}}}}'],
            capture_output=True, text=True, timeout=SWEEP_TIMEOUT, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    removed = []
    for line in listing.stdout.splitlines():
        name, _, owner = line.partition(" ")
        pid = _owner_pid(owner)
        if not name or pid is None or _pid_alive(pid):
            continue
        left = deadline - time.monotonic()
        if left <= 0:
            break  # the rest goes at the next start
        remove(Launch([], {}, runtime=runtime, container=name), timeout=left)
        removed.append(name)
    return removed
