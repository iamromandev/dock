"""Server settings: flags, DOCK_SERVER_TOKEN / [server] token in config.toml, and the in/out folders."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dock_host.manifest import ManifestError

from src import paths

DEFAULT_PORT = 7464
DEFAULT_MAX_RUNS = 4
HOST = "127.0.0.1"  # no flag: the server is for this machine only


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    token: str
    dirs: paths.Dirs
    host: str = HOST
    port: int = DEFAULT_PORT
    max_runs: int = DEFAULT_MAX_RUNS


def _token(store) -> str:
    token = os.environ.pop("DOCK_SERVER_TOKEN", None)  # popped: no plugin env passthrough can hand it on
    if not token:
        try:
            table = store.config().get("server", {})
        except ManifestError as e:
            raise ConfigError(str(e)) from e
        value = table.get("token") if isinstance(table, dict) else None
        token = value if isinstance(value, str) else None
    if not token:
        raise ConfigError(
            f"no token: set DOCK_SERVER_TOKEN or [server] token in {store.home / 'config.toml'}"
        )
    return token


def _int(flag: str, value: str, minimum: int, maximum: int | None = None) -> int:
    try:
        number = int(value)
    except ValueError:
        raise ConfigError(f"{flag} needs a whole number, got '{value}'") from None
    if number < minimum:
        raise ConfigError(f"{flag} must be at least {minimum}")
    if maximum is not None and number > maximum:
        raise ConfigError(f"{flag} must be at most {maximum}")
    return number


def _flags(argv: list[str]) -> dict[str, str]:
    known = {"--port", "--max-runs", "--in", "--out"}
    found: dict[str, str] = {}
    rest = list(argv)
    while rest:
        flag, eq, value = rest.pop(0).partition("=")
        if flag not in known:
            raise ConfigError(f"unknown argument '{flag}' (known: {', '.join(sorted(known))})")
        if not eq:
            if not rest:
                raise ConfigError(f"{flag} needs a value")
            value = rest.pop(0)
        found[flag] = value
    return found


def load(store, argv: list[str]) -> Config:
    flags = _flags(argv)
    token = _token(store)
    try:
        dirs = paths.resolve(store, flags.get("--in"), flags.get("--out"))
    except ManifestError as e:
        raise ConfigError(str(e)) from e
    return Config(
        token=token,
        dirs=dirs,
        port=_int("--port", flags["--port"], 0, 65535) if "--port" in flags else DEFAULT_PORT,
        max_runs=_int("--max-runs", flags["--max-runs"], 1) if "--max-runs" in flags else DEFAULT_MAX_RUNS,
    )
