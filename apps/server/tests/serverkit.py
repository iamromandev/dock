"""A real server on an ephemeral port in a thread, and a small synchronous client."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("DOCK_CANCEL_GRACE", "1")  # read when dock_host is imported: a stuck plugin is killed after 1 s
import dockenv
from dock_host import isolation
from dock_host.store import Store
from websockets.sync.client import connect
from websockets.typing import Origin

from src import config, paths, server

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TOKEN = "s3cret-token"


def make_store(*names: str, prebuild: bool = False) -> Store:
    """A throwaway DOCK_HOME with these fixture plugins installed (images built when asked)."""
    dirs = [FIXTURES / n for n in names]
    if prebuild:
        dockenv.prebuild_plugins(*dirs)
    return Store(dockenv.make_home("dock-server-", *dirs))


def make_config(store: Store, **over) -> config.Config:
    out = Path(store.home) / "out"
    out.mkdir(exist_ok=True)
    dirs = paths.Dirs(None, out, "current folder", "current folder", out)
    return config.Config(token=TOKEN, dirs=dirs, port=0, **over)


def _containers() -> set[str]:
    # only this test process's containers: other projects' tests may run at the same time
    label = f"label={isolation.LABEL}={isolation.owner_label()}"
    return set(dockenv.run_runtime("ps", "-a", "-q", "--filter", label).stdout.split())


@contextmanager
def running(store: Store, **over):
    """Serve on port 0; yields (url, srv). Teardown stops the server and asserts no container was left behind."""
    before = _containers()
    srv = server.serve(make_config(store, **over), store)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    port = srv.socket.getsockname()[1]
    try:
        yield f"ws://127.0.0.1:{port}", srv
    finally:
        srv.shutdown()
        thread.join(10)
        end = time.monotonic() + 15
        while _containers() - before and time.monotonic() < end:
            time.sleep(0.2)
        leftover = _containers() - before
        assert not leftover, f"containers left behind: {leftover}"


class Client:
    def __init__(self, url: str, origin: str | None = None):
        self._cm = connect(url, origin=Origin(origin) if origin else None)
        self.ws = self._cm.__enter__()

    def send(self, **msg) -> None:
        self.ws.send(json.dumps(msg))

    def send_raw(self, text: str) -> None:
        self.ws.send(text)

    def recv(self, timeout: float = 60) -> dict:
        return json.loads(self.ws.recv(timeout=timeout))

    def hello(self, token: str = TOKEN, version: int = 2) -> dict:
        self.send(type="hello", version=version, token=token)
        return self.recv()

    def until_done(self, run: str) -> list[dict]:
        """Every frame up to and including `done` of this run."""
        frames = []
        while True:
            frames.append(self.recv())
            if frames[-1]["type"] == "done" and frames[-1]["run"] == run:
                return frames

    def close(self) -> None:
        self._cm.__exit__(None, None, None)
