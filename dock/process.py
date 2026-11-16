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

from dock import API_VERSION
from dock.manifest import Manifest


class PluginError(Exception):
    def __init__(self, message: str, code: int = -32000):
        super().__init__(message)
        self.code = code


CANCELLED = -32800


class PluginProcess:
    def __init__(self, manifest: Manifest, log_path, handlers: dict[str, Callable[[dict], Any]]):
        self.manifest = manifest
        self.log_path = log_path
        self.handlers = handlers  # plugin -> host methods (output, log, progress, prompt_user)
        self._ids = itertools.count(1)
        self._inbox: queue.Queue = queue.Queue()
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
                self._inbox.put(("msg", json.loads(line)))
            except json.JSONDecodeError:
                self._inbox.put(("junk", line))
        self._inbox.put(("eof", None))

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
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            try:
                kind, msg = self._inbox.get(timeout=remaining)
            except queue.Empty:
                raise PluginError(f"{self.manifest.id} did not answer '{method}' in {timeout}s")

            if kind == "eof":
                self._inbox.put(("eof", None))  # keep it visible for later calls
                raise PluginError(f"{self.manifest.id} exited during '{method}' (see {self.log_path})")
            if kind == "junk":
                self._log.write(f"[non-protocol stdout] {msg}\n")
                continue
            if "method" in msg:
                self._handle_incoming(msg)
                continue
            if msg.get("id") == req_id:
                if "error" in msg:
                    err = msg["error"]
                    raise PluginError(err.get("message", "plugin error"), err.get("code", -32000))
                return msg.get("result")
            # a late reply to an earlier request (e.g. after cancel): ignore

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
