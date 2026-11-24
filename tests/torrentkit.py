"""Helpers shared by the torrent tests and the fixtures: build torrents and payloads, run servers."""

from __future__ import annotations

import asyncio
import hashlib
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DM_PATH = ROOT / "plugin" / "dm"
if str(DM_PATH) not in sys.path:
    sys.path.insert(0, str(DM_PATH))

from dmcore.torrent import bencode  # noqa: E402


def content(size: int, tag: str = "") -> bytes:
    """Deterministic bytes: the sha256 of a counter, repeated. A different tag gives different bytes."""
    out, i, have = [], 0, 0
    while have < size:
        block = hashlib.sha256(f"{tag}:{i}".encode()).digest()
        out.append(block)
        have += len(block)
        i += 1
    return b"".join(out)[:size]


@dataclass(frozen=True)
class Torrent:
    data: bytes  # the .torrent file
    info_hash: bytes
    name: str
    files: tuple  # ((relative path as "a/b", bytes), ...)
    piece_length: int
    multi: bool

    @property
    def blob(self) -> bytes:
        return b"".join(d for _, d in self.files)

    def write_torrent(self, path) -> Path:
        path = Path(path)
        path.write_bytes(self.data)
        return path

    def write_payload(self, root) -> Path:
        """Write the content under root the way a seeder keeps it: root/<name> (a file or a folder)."""
        return write_files(root, self)


def make_torrent(files, piece_length: int = 16384, announce: str | None = None, name: str = "t",
                 multi: bool | None = None, announce_list=None, extra_info: dict | None = None) -> Torrent:
    """files: [(relative path, bytes)]. One file and multi=False/None: a single-file torrent named `name`."""
    files = tuple((p, bytes(d)) for p, d in files)
    if multi is None:
        multi = len(files) != 1
    blob = b"".join(d for _, d in files)
    pieces = b"".join(hashlib.sha1(blob[i:i + piece_length]).digest()
                      for i in range(0, len(blob), piece_length))
    info: dict = {"name": name, "piece length": piece_length, "pieces": pieces}
    if multi:
        info["files"] = [{"length": len(d), "path": p.split("/")} for p, d in files]
    else:
        info["length"] = len(blob)
    info.update(extra_info or {})
    top: dict = {"info": info}
    if announce:
        top["announce"] = announce
    if announce_list:
        top["announce-list"] = announce_list
    data = bencode.encode(top)
    return Torrent(data, hashlib.sha1(bencode.encode(info)).digest(), name, files, piece_length, multi)


def magnet_for(t: Torrent, trackers=(), peers=(), name: str | None = None) -> str:
    """A magnet link for t: its info-hash, `dn`, the tracker URLs (tr) and the (host, port) peers (x.pe)."""
    from urllib.parse import quote

    parts = [f"xt=urn:btih:{t.info_hash.hex()}", f"dn={quote(name if name is not None else t.name)}"]
    parts += [f"tr={quote(u, safe='')}" for u in trackers]
    parts += [f"x.pe={quote(f'{h}:{p}' if ':' not in h else f'[{h}]:{p}', safe='')}" for h, p in peers]
    return "magnet:?" + "&".join(parts)


def make_big_torrent(root, name: str, size: int, piece_length: int, announce: str | None = None):
    """A single-file torrent of `size` random bytes, written to root/<name> in a stream (not held in memory).

    Returns (Torrent, path of the payload). The Torrent's `files` is empty: read the payload from the path.
    """
    import os

    path = Path(root) / name
    pieces = []
    with open(path, "wb") as f:
        left = size
        while left:
            block = os.urandom(min(piece_length, left))
            pieces.append(hashlib.sha1(block).digest())
            f.write(block)
            left -= len(block)
    info = {"name": name, "piece length": piece_length, "pieces": b"".join(pieces), "length": size}
    top: dict = {"info": info}
    if announce:
        top["announce"] = announce
    data = bencode.encode(top)
    return Torrent(data, hashlib.sha1(bencode.encode(info)).digest(), name, (), piece_length, False), path


def file_digest(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(1 << 20):
            h.update(block)
    return h.hexdigest()


def write_files(root, t: Torrent) -> Path:
    """Write t's payload at root/<name>; returns that path."""
    root = Path(root)
    if not t.multi:
        out = root / t.name
        out.write_bytes(t.blob)
        return out
    for rel, data in t.files:
        p = root / t.name / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return root / t.name


def tree(root) -> dict:
    """{relative path: bytes} of every file under root (for comparing a download with its source)."""
    root = Path(root)
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


class Background:
    """An asyncio loop on its own thread, for fixtures that run next to code that blocks the test thread."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def call(self, coro, timeout: float = 30):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def submit(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def close(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(5)
        if not self.loop.is_running():
            self.loop.close()


def load_fixture(name: str):
    """Import tests/fixtures/torrent/<name>.py as a module."""
    import importlib.util

    path = ROOT / "tests" / "fixtures" / "torrent" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"torrentfix_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
