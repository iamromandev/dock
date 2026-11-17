"""One running plugin: a child process spoken to with JSON-RPC over stdio."""

from __future__ import annotations

import itertools
import json
import queue
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

from cli import isolation
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

    def pump(self, timeout: float | None) -> bool:
        """Take one message from the inbox and act on it; False if none came within `timeout`."""
        try:
            proc, kind, msg = self.inbox.get(timeout=timeout)
        except queue.Empty:
            return False
        if kind == "eof":
            proc.exited = True
        elif proc.forgotten:
            pass  # the host already stopped it; nobody waits for what it said
        elif kind == "junk":
            proc._log.write(f"[non-protocol stdout] {msg}\n")
        elif "method" in msg:
            proc._handle_incoming(msg)  # may itself wait (e.g. a relayed call): re-entrant by design
        else:
            proc.pending.discard(msg.get("id"))
            self.replies[(proc, msg.get("id"))] = msg  # ours, or someone else's: picked up by its waiter
        return True


class PluginProcess:
    def __init__(
        self, manifest: Manifest, log_path, handlers: dict[str, Callable[[dict], Any]],
        launch: isolation.Launch, hub: Hub | None = None,
    ):
        self.manifest = manifest
        self.launch = launch  # what to spawn: always decided by isolation.prepare, never defaulted here
        self._container_removed = False
        self.log_path = log_path
        self.handlers = handlers  # plugin -> host methods (output, log, progress, prompt_user, call)
        self.hub = hub or Hub()
        self.exited = False
        self.provisioned = False        # provision was requested; unprovision is owed
        self.forgotten = False          # the session dropped it: its late messages are ignored
        self.pending: set[int] = set()  # ids of requests sent and not yet answered
        self._ids = itertools.count(1)
        self._write_lock = threading.Lock()
        self.proc: subprocess.Popen | None = None

    # process -------------------------------------------------------------
    def start(self) -> None:
        launch = self.launch
        cmd = launch.argv
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
            env=launch.env,
            **group,
        )
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def alive(self) -> bool:
        return not self.exited and self.proc is not None and self.proc.poll() is None

    def _read_loop(self) -> None:
        assert self.proc and self.proc.stdout
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    self.hub.inbox.put((self, "msg", json.loads(line)))
                except json.JSONDecodeError:
                    self.hub.inbox.put((self, "junk", line))
        except (ValueError, OSError):
            pass  # stdout was closed under us by stop()/kill()
        self.hub.inbox.put((self, "eof", None))

    def _close_pipes(self) -> None:
        """Close our ends once the child is gone, so nothing is left for the garbage collector."""
        assert self.proc
        self._reader.join(timeout=1)  # EOF arrives with the child's exit
        pipes = [self.proc.stdin]
        if not self._reader.is_alive():  # closing stdout under a blocked reader would block us
            pipes.append(self.proc.stdout)
        for pipe in pipes:
            try:
                if pipe:
                    pipe.close()
            except OSError:
                pass  # e.g. a flush into a broken stdin

    def _remove_container(self) -> None:
        """The child is gone, but killing a container's runtime client may leave the container: remove it."""
        if self.launch and self.launch.container and not self._container_removed:
            self._container_removed = True
            isolation.remove(self.launch, self._log)

    def stop(self, timeout: float = 3.0) -> None:
        if not self.proc:
            return
        try:
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
        except BaseException:  # e.g. a second Ctrl-C: never leave the child running
            self.kill()
            raise
        self._close_pipes()
        self._remove_container()
        self._log.close()

    def kill(self) -> None:
        """Stop without asking: for a plugin that no longer answers."""
        if not self.proc:
            return
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()
        self._close_pipes()
        self._remove_container()
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

    def send_request(self, method: str, params: dict | None = None) -> int:
        """Send a request without waiting; wait_for(id, ...) collects the reply."""
        req_id = next(self._ids)
        self.pending.add(req_id)
        try:
            self._send({"id": req_id, "method": method, "params": params or {}})
        except PluginError:
            self.pending.discard(req_id)
            raise
        return req_id

    def request(self, method: str, params: dict | None = None, timeout: float | None = None) -> Any:
        return self.wait_for(self.send_request(method, params), method, timeout)

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
            if not self.hub.pump(remaining):
                raise PluginError(f"{self.manifest.id} did not answer '{method}' in {timeout}s") from None

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
