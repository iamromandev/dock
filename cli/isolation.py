"""Running a plugin in a container instead of as a native process.

The runtime (docker, podman, kiri, ...) is driven only through its command line.
With isolation on, the container gets what the plugin's declared permissions
allow and nothing else; the stdio protocol is unchanged, because `<runtime> run -i`
forwards stdin/stdout.
"""

from __future__ import annotations

import codecs
import difflib
import itertools
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path

from cli import API_VERSION
from cli.manifest import Manifest, ManifestError

LABEL = "dev.dock.owner"            # value: <hostname>:<pid> of the dock process that owns the container
ENV_ALLOW = ("LANG", "TZ")  # the only host variables a container sees (never PATH: the image has its own)
REMOVE_TIMEOUT = 10
SWEEP_TIMEOUT = 5   # all of the startup sweep, so a stuck runtime cannot hold up every dock command

_names = itertools.count(1)


@dataclass
class Config:
    runtime: str = "docker"
    enabled: bool = False
    plugins: tuple[str, ...] = ()

    def isolates(self, plugin_id: str) -> bool:
        return self.enabled or plugin_id in self.plugins


KNOWN_KEYS = ("enabled", "plugins", "runtime")
_NAME = "isolation"


def _looks_like_isolation(name: str, partial: bool = False) -> bool:
    """`isolation`, however it is cased, a close misspelling of it, or (partial) the start of the word."""
    n = name.strip().lower()
    if n == _NAME or (partial and n and _NAME.startswith(n)):
        return True
    return bool(difflib.get_close_matches(n, [_NAME], n=1, cutoff=0.75))


def config(store) -> Config | None:
    """The [isolation] table of config.toml; None when there is none (nothing then calls a runtime).

    Fails closed: a mistake that could mean "isolation was wanted" (an unknown key, a misspelt table,
    a broken [isolation] section) is an error, never a quiet native run.
    """
    path = store.home / "config.toml"
    try:
        data = store.config()
    except ManifestError as e:
        # A config.toml that does not parse never stopped plugins from running, and still must not,
        # unless the damage is in (or may be) the isolation settings. Judge section by section.
        data = _salvage(path, e)
    where = f"{path}: [isolation]"
    table = data.get(_NAME)
    if table is None:
        _reject_lookalikes(data, path)
        return None
    if not isinstance(table, dict):
        raise ManifestError(f"{where} must be a table")
    unknown = sorted(set(table) - set(KNOWN_KEYS))
    if unknown:
        names = ", ".join(repr(k) for k in unknown)
        raise ManifestError(
            f"{where} has unknown key{'s' if len(unknown) > 1 else ''} {names} "
            f"(known: {', '.join(KNOWN_KEYS)})"
        )
    runtime = table.get("runtime", "docker")
    enabled = table.get("enabled", False)
    plugins = table.get("plugins", [])
    if not isinstance(runtime, str) or not runtime:
        raise ManifestError(f"{where} runtime must be a command name or path")
    if not isinstance(enabled, bool):
        raise ManifestError(f"{where} enabled must be true or false")
    if not isinstance(plugins, list) or not all(isinstance(p, str) for p in plugins):
        raise ManifestError(f"{where} plugins must be a list of plugin ids")
    _warn_unknown_plugins(store, plugins)
    return Config(runtime, enabled, tuple(plugins))


def _reject_lookalikes(data: dict, path) -> None:
    """No [isolation] table: refuse what is plainly meant as one (a typo must not mean 'off')."""
    for key in data:
        if key.lower() in KNOWN_KEYS:
            raise ManifestError(f"{path}: '{key}' is at the top level; isolation settings belong in an [isolation] table")
        if key != _NAME and _looks_like_isolation(key):
            raise ManifestError(f"{path}: [{key}] is not a known table; did you mean [isolation]?")


def _warn_unknown_plugins(store, plugins: list[str]) -> None:
    try:
        installed = store.index()
    except (OSError, ValueError):
        return
    for plugin_id in plugins:
        if plugin_id not in installed and plugin_id not in store.isolation_warned:
            store.isolation_warned.add(plugin_id)  # every command asks; say it once
            print(f"dock: [isolation] plugins lists '{plugin_id}', which is not an installed plugin", file=sys.stderr)


def _sections(text: str) -> list[tuple[str, str]]:
    """(header line, text) per top-level section; the first has no header (keys before any table)."""
    out: list[tuple[str, str]] = []
    header, lines = "", []
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith("["):
            out.append((header, "".join(lines)))
            header, lines = line, []
        lines.append(line)
    out.append((header, "".join(lines)))
    return out


def _header_names(header: str) -> list[str]:
    """What table a header line names, even a broken one: the parsed key, else best guesses at it."""
    try:
        return list(tomllib.loads(header))
    except tomllib.TOMLDecodeError:
        pass
    head = header.strip().lstrip("[").split("]")[0].split("#")[0]
    names = [re.sub(r"""["'\s]""", "", head).split(".")[0]]
    try:  # ["i\u0073olation" and friends
        names.append(codecs.decode(names[0], "unicode_escape"))
    except (UnicodeDecodeError, ValueError):
        pass
    return names


def _salvage(path, error: ManifestError) -> dict:
    """What can be trusted of a config.toml that does not parse (or decode) as a whole.

    Each top-level section is parsed on its own. Returns the sections that parse; raises `error`
    when one that is, or looks like, the isolation settings does not.
    """
    try:
        text = path.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        raise error from None  # cannot even read it: cannot tell
    refuse = ManifestError(f"{error} (it may hold isolation settings, so no plugin is started unisolated)")
    data: dict = {}
    for header, chunk in _sections(text):
        try:
            parsed = tomllib.loads(chunk)
        except tomllib.TOMLDecodeError:
            if header:
                broken = any(_looks_like_isolation(n, partial=True) for n in _header_names(header))
            else:  # keys before the first table
                broken = bool(re.search(rf"""(?im)^\s*["']?({'|'.join(KNOWN_KEYS)}|{_NAME})\b""", chunk))
            if broken:
                raise refuse from None
            continue
        for key, value in parsed.items():
            if isinstance(value, dict) and isinstance(data.get(key), dict):
                value = {**data[key], **value}
            data[key] = value
    return data


@dataclass
class Launch:
    """What to spawn for a plugin: argv, the environment of that process, and the container to clean up."""
    argv: list[str]
    env: dict[str, str]
    runtime: str | None = None   # set when the plugin runs in a container
    container: str | None = None  # its name, for `rm -f`


def image_tag(m: Manifest) -> str:
    return f"dock/{m.id}:{m.version}"


def container_name(m: Manifest) -> str:
    return f"dock-{m.id.replace('.', '-')}-{os.getpid()}-{next(_names)}"


def container_env(m: Manifest) -> dict[str, str]:
    """The plugin's whole environment inside the container: the allowlist, never the rest of the host's."""
    env = {k: os.environ[k] for k in ENV_ALLOW if k in os.environ}
    return env | {"HOME": "/tmp", "DOCK_PLUGIN_ID": m.id, "DOCK_API_VERSION": API_VERSION}


def owner_label() -> str:
    """Who owns a container: this dock process, on this machine (a pid means nothing on another host)."""
    return f"{socket.gethostname()}:{os.getpid()}"


def run_argv(cfg: Config, m: Manifest, *, name: str, data_dir, cwd, uid_gid, env: dict[str, str]) -> list[str]:
    """The `run` command line. uid_gid is None where there is no POSIX user (Windows)."""
    assert m.container is not None
    return [
        cfg.runtime, "run", "-i", "--rm", "--pull", "never", "--name", name, "--label", f"{LABEL}={owner_label()}",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--read-only", "--tmpfs", "/tmp",
        *(["--user", f"{uid_gid[0]}:{uid_gid[1]}"] if uid_gid else []),
        *(["--network", "none"] if "network" not in m.permissions else []),
        "-v", f"{data_dir}:{data_dir}",
        *(["-v", f"{cwd}:{cwd}"] if "fs:write:cwd" in m.permissions else []),
        "--workdir", "/plugin",
        *[x for k, v in env.items() for x in ("-e", f"{k}={v}")],
        image_tag(m), m.container.cmd, *m.container.args,
    ]


def native_launch(m: Manifest) -> Launch:
    return Launch(m.entrypoint(), {**os.environ, "DOCK_PLUGIN_ID": m.id, "DOCK_API_VERSION": API_VERSION})


def _find_runtime(cfg: Config) -> Config:
    found = shutil.which(cfg.runtime)
    if found is None:
        raise ManifestError(f"isolation runtime '{cfg.runtime}' not found")
    return replace(cfg, runtime=found)


def ensure_image(cfg: Config, store, m: Manifest, force: bool = False) -> None:
    """Build the plugin's image unless it exists (or always, with force). Output goes to the plugin log."""
    tag = image_tag(m)
    if not force:
        try:
            probe = subprocess.run([cfg.runtime, "image", "inspect", tag], capture_output=True, check=False)
        except OSError as e:
            raise ManifestError(f"isolation runtime '{cfg.runtime}' failed: {e}") from e
        if probe.returncode == 0:
            return

    log_path = store.log_path(m.id)
    print(f"dock: building image {tag} …", file=sys.stderr, flush=True)
    assert m.container is not None
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


def has_container_build(m: Manifest) -> bool:
    return m.container is not None and (m.root / m.container.dockerfile).is_file()


def forget_image(store, m: Manifest) -> None:
    """Best effort: drop the image of a plugin that was just (re)installed, so the next run rebuilds it.

    The tag is id:version, so reinstalling the same version (new sources, same number) would otherwise
    keep running the stale image. Only asks a runtime the user configured; a failure is not the install's.
    """
    if m.container is None:
        return
    try:
        cfg = config(store)
    except ManifestError:
        return
    runtime = shutil.which(cfg.runtime) if cfg else None
    if runtime is None:
        return
    try:
        subprocess.run([runtime, "image", "rm", "-f", image_tag(m)],
                       capture_output=True, timeout=REMOVE_TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired):
        pass


def runtime_label(runtime: str) -> str:
    """How the runtime is named to the user: `docker`, even when configured as /usr/bin/docker."""
    return Path(runtime).name


def build(store, m: Manifest) -> str:
    """`dock plugin build`: (re)build the image whether or not isolation is on. Returns the tag."""
    cfg = _find_runtime(config(store) or Config())
    if not has_container_build(m):
        raise ManifestError(f"{m.id}: the plugin has no container build (add [run].container and a Dockerfile)")
    ensure_image(cfg, store, m, force=True)
    return image_tag(m)


def describe(store, m: Manifest) -> str:
    """The `isolation:` line of `plugin info`; asks the runtime nothing."""
    try:
        cfg = config(store)
    except ManifestError as e:
        return f"unknown ({e})"
    if cfg is None or not cfg.isolates(m.id):
        return "off"
    if not has_container_build(m):
        return "on, but not supported by this plugin"
    net = "network" if "network" in m.permissions else "no network"
    cwd = "cwd" if "fs:write:cwd" in m.permissions else "no cwd access"
    return f"{runtime_label(cfg.runtime)} ({net}, {cwd})"


def _check_cwd_mount(store, m: Manifest, cwd: str) -> None:
    """The cwd is mounted read-write, so refuse one that would hand over more than a project directory."""
    real = Path(os.path.realpath(cwd))
    reason = None
    if real == Path(real.anchor):
        reason = "the filesystem root"
    elif real == Path(os.path.realpath(Path.home())):
        reason = "your home directory"
    else:
        dock_home = Path(os.path.realpath(store.home))
        if dock_home.is_relative_to(real) or real.is_relative_to(dock_home):
            reason = f"inside or above the dock home ({store.home}), which holds every plugin's data"
    if reason:
        raise ManifestError(
            f"{m.id}: refusing to mount {cwd} read-write (fs:write:cwd): it is {reason}; "
            "run dock from a project directory"
        )


def prepare(store, m: Manifest, cwd: str | None = None) -> Launch:
    """Decide how to spawn `m`: natively, or in a container (building its image first if needed)."""
    cfg = config(store)
    if cfg is None or not cfg.isolates(m.id):
        return native_launch(m)

    cfg = _find_runtime(cfg)
    if not has_container_build(m):
        raise ManifestError(
            f"{m.id}: isolation is on but the plugin has no container build "
            "(add [run].container and a Dockerfile, or exclude it in [isolation])"
        )
    cwd = cwd or os.getcwd()
    if "fs:write:cwd" in m.permissions:
        _check_cwd_mount(store, m, cwd)
    ensure_image(cfg, store, m)

    name = container_name(m)
    argv = run_argv(
        cfg, m, name=name, data_dir=store.data_dir(m.id), cwd=cwd,
        uid_gid=(os.getuid(), os.getgid()) if hasattr(os, "getuid") else None,
        env=container_env(m),
    )
    # The runtime client keeps the host environment (it needs DOCKER_HOST and friends);
    # only the -e entries above reach the plugin.
    return Launch(argv, dict(os.environ), runtime=cfg.runtime, container=name)


def remove(launch: Launch, log=None, timeout: float = REMOVE_TIMEOUT) -> None:
    """Best effort `rm -f` of the launch's container, so it does not outlive its process."""
    if not launch.container or not launch.runtime:
        return
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
    is the configured one, else `docker` if it is on PATH), and the whole sweep gets SWEEP_TIMEOUT seconds.
    """
    if sys.platform == "win32":
        return []  # os.kill(pid, 0) does not probe a process there; never judge liveness
    try:
        cfg = config(store) or Config()
    except ManifestError:
        return []  # reported when a plugin is started
    runtime = shutil.which(cfg.runtime)
    if runtime is None:
        return []  # reported when a plugin is started, not here
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
