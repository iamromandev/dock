"""One running plugin: a child process spoken to with JSON-RPC over stdio."""

from __future__ import annotations

import itertools
import json
import os
import queue
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

from cli import API_VERSION
from cli.manifest import Manifest


class PluginError(Exception):
    def __init__(self, message: str, code: int = -32000):
        super().__init__(message)
        self.code = code


CANCELLED = -32800


class Hub:
    """One inbox for every plugin in a session.

    Whatever the host is waiting for, it keeps serving all plugins. Example:
    while the host waits for the llm plugin to finish `generate`, chat is
    already sending `output` for the tokens it was forwarded; those must reach
    the terminal now, not after the reply completes.
    """

    def __init__(self):
        self.inbox: queue.Queue = queue.Queue()  # (process, kind, message)
        self.replies: dict = {}                  # (process, id) -> reply that arrived while waiting on something else


class PluginProcess:
    def __init__(self, manifest: Manifest, log_path, handlers: dict[str, Callable[[dict], Any]], hub: Hub | None = None):
        self.manifest = manifest
        self.log_path = log_path
        self.handlers = handlers  # plugin -> host methods (output, log, progress, prompt_user, call)
        self.hub = hub or Hub()
        self.exited = False
        self._ids = itertools.count(1)
        self._write_lock = threading.Lock()
        self.proc: subprocess.Popen | None = None

    # process -------------------------------------------------------------
    def start(self) -> None:
        cmd = self.manifest.entrypoint()
        self._log = open(self.log_path, "a", encoding="utf-8")  # noqa: SIM115 - closed in stop()
        self._log.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} start {cmd}\n")
        self._log.flush()

        # New session/process group: Ctrl-C reaches dock only, and dock
        # decides how to tell the plugin (the `cancel` notification).
        group = (
            {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
            if sys.platform == "win32" else {"start_new_session": True}
        )
        self.proc = subprocess.Popen(
            cmd,
            cwd=self.manifest.root,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._log,
            text=True,
            encoding="utf-8",
            bufsize=1,
            env={**os.environ, "DOCK_PLUGIN_ID": self.manifest.id, "DOCK_API_VERSION": API_VERSION},
            **group,
        )
        threading.Thread(target=self._read_loop, daemon=True).start()

    def _read_loop(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                self.hub.inbox.put((self, "msg", json.loads(line)))
            except json.JSONDecodeError:
                self.hub.inbox.put((self, "junk", line))
        self.hub.inbox.put((self, "eof", None))

    def stop(self, timeout: float = 3.0) -> None:
        if not self.proc:
            return
        if self.proc.poll() is None:
            try:
                self.request("shutdown", timeout=timeout)
            except PluginError:
                pass
            try:
                self.proc.stdin.close()
                self.proc.wait(timeout=timeout)
            except (subprocess.TimeoutExpired, OSError):
                self.proc.kill()
                self.proc.wait()
        self._log.close()

    # messaging -----------------------------------------------------------
    def _send(self, msg: dict) -> None:
        assert self.proc and self.proc.stdin
        with self._write_lock:
            try:
                self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", **msg}) + "\n")
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError) as e:
                raise PluginError(f"{self.manifest.id} is not running") from e

    def notify(self, method: str, params: dict | None = None) -> None:
        self._send({"method": method, "params": params or {}})

    def request(self, method: str, params: dict | None = None, timeout: float | None = None) -> Any:
        req_id = next(self._ids)
        self._send({"id": req_id, "method": method, "params": params or {}})
        return self.wait_for(req_id, method, timeout)

    def wait_for(self, req_id: int, method: str, timeout: float | None) -> Any:
        """Wait for our reply to req_id, serving every plugin's messages meanwhile."""
        key = (self, req_id)
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if key in self.hub.replies:
                msg = self.hub.replies.pop(key)
                if "error" in msg:
                    err = msg["error"]
                    raise PluginError(err.get("message", "plugin error"), err.get("code", -32000))
                return msg.get("result")
            if self.exited:
                raise PluginError(f"{self.manifest.id} exited during '{method}' (see {self.log_path})")

            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            try:
                proc, kind, msg = self.hub.inbox.get(timeout=remaining)
            except queue.Empty:
                raise PluginError(f"{self.manifest.id} did not answer '{method}' in {timeout}s") from None

            if kind == "eof":
                proc.exited = True
            elif kind == "junk":
                proc._log.write(f"[non-protocol stdout] {msg}\n")
            elif "method" in msg:
                proc._handle_incoming(msg)  # may itself wait (e.g. a relayed call): re-entrant by design
            else:
                self.hub.replies[(proc, msg.get("id"))] = msg  # ours, or someone else's: picked up by its waiter

    def _handle_incoming(self, msg: dict) -> None:
        method, params, msg_id = msg["method"], msg.get("params") or {}, msg.get("id")
        handler = self.handlers.get(method)
        if msg_id is None:  # notification
            if handler:
                handler(params)
            return
        if handler is None:
            self._send({"id": msg_id, "error": {"code": -32601, "message": f"host has no '{method}'"}})
            return
        try:
            self._send({"id": msg_id, "result": handler(params)})
        except Exception as e:  # noqa: BLE001 - report any host-side failure to the plugin
            self._send({"id": msg_id, "error": {"code": -32000, "message": str(e)}})
