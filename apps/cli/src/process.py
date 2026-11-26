"""One running plugin: a container (`<runtime> run -i`) spoken to with JSON-RPC over stdio."""

from __future__ import annotations

import collections
import itertools
import json
import queue
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

from src import isolation
from src.manifest import Manifest


class PluginError(Exception):
    def __init__(self, message: str, code: int = -32000):
        super().__init__(message)
        self.code = code


CANCELLED = -32800


LOG_TEXT_MAX = 120


def _printable(name) -> str:
    """A method name or reason from a plugin, fit for one line of the log: escaped when it holds anything that is
    not printable (line breaks, controls, separators), and cut short."""
    text = name if isinstance(name, str) and name.isprintable() else repr(name)
    return text if len(text) <= LOG_TEXT_MAX else text[:LOG_TEXT_MAX] + "…"


OUTBOX_CONTROL = 1024  # messages queued for a plugin that reads nothing; past this it is stuck
OUTBOX_CONTROL_BYTES = 8 * 2**20  # ... or past this many bytes of them


class Hub:
    """One inbox for every plugin in a session.

    Whatever the host is waiting for, it keeps serving all plugins: a message from one plugin (an `output`,
    a `log`) is handled now, even while the host waits for the reply of another.
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
            proc._handle_incoming(msg)  # may itself wait (e.g. prompt_user, for the user): re-entrant by design
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
        self.launch = launch  # the `<runtime> run` command to spawn: decided by isolation.prepare, never defaulted here
        self._container_removed = False
        self.log_path = log_path
        self.handlers = handlers  # plugin -> host methods (output, log, bar, table, panel, clear, prompt_user, form)
        self.hub = hub or Hub()
        self.exited = False
        self.forgotten = False          # the session dropped it: its late messages are ignored
        self.pending: set[int] = set()  # ids of requests sent and not yet answered
        self._ids = itertools.count(1)
        # Everything for the plugin's stdin goes through this queue to one writer thread, so a plugin that stops
        # reading can block that thread only: the hub (cancel, shutdown, replies) just queues and carries on.
        self._out: collections.deque = collections.deque()  # lines
        self._out_bytes = 0             # bytes queued
        self._out_closed = False        # no more messages are accepted (stopped, or the pipe broke)
        self._out_cv = threading.Condition()
        self._writer: threading.Thread | None = None
        self.proc: subprocess.Popen | None = None
        self._ended = False

    # process -------------------------------------------------------------
    def start(self) -> None:
        launch = self.launch
        cmd = launch.argv
        self._ended = False
        self._log = open(self.log_path, "a", encoding="utf-8")  # closed in stop()
        self._log.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} start {cmd}\n")
        self._log.flush()

        # New session/process group: Ctrl-C reaches dock only, and dock
        # decides how to tell the plugin (the `cancel` notification).
        group = (
            {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
            if sys.platform == "win32" else {"start_new_session": True}
        )
        try:
            self.proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self._log,
                text=True,
                encoding="utf-8",
                bufsize=1,
                env=launch.env,
                **group,
            )
        except BaseException:
            self._log.close()  # no process, so no stop() will ever close it
            raise
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self._writer = threading.Thread(target=self._write_loop, daemon=True)
        self._writer.start()

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

    def _write_loop(self) -> None:
        """The only thread that writes to the plugin's stdin; it may block there for as long as the plugin likes."""
        assert self.proc and self.proc.stdin
        while True:
            with self._out_cv:
                while not self._out and not self._out_closed:
                    self._out_cv.wait()
                if not self._out:
                    return  # closed and everything queued was written
                line = self._out.popleft()
                self._out_bytes -= len(line)
            try:
                self.proc.stdin.write(line)
                self.proc.stdin.flush()
            except (OSError, ValueError):  # the plugin is gone, or stdin was closed under us
                with self._out_cv:
                    self._out_closed = True
                    self._out.clear()
                    self._out_bytes = 0
                    self._out_cv.notify_all()
                return

    def _close_outbox(self, discard: bool = False) -> None:
        """Accept no more messages; the writer finishes what is queued, unless `discard`."""
        with self._out_cv:
            self._out_closed = True
            if discard:
                self._out.clear()
                self._out_bytes = 0
            self._out_cv.notify_all()

    def _close_pipes(self) -> None:
        """Close our ends once the child is gone, so nothing is left for the garbage collector."""
        assert self.proc
        self._reader.join(timeout=1)  # EOF arrives with the child's exit
        self._close_outbox(discard=True)
        if self._writer:
            self._writer.join(timeout=1)  # the child is gone: a blocked write fails at once
        pipes = []
        if not (self._writer and self._writer.is_alive()):  # closing stdin under a blocked write would block us
            pipes.append(self.proc.stdin)
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
        if not self._container_removed:
            self._container_removed = True
            isolation.remove(self.launch, self._log)

    def stop(self, timeout: float = 3.0) -> None:
        if not self.proc:
            return
        if self._ended:
            return
        try:
            if self.proc.poll() is None:
                try:
                    self.request("shutdown", timeout=timeout)
                except PluginError:
                    pass
                self._close_outbox()
                self._writer.join(timeout=min(timeout, 1.0))  # a plugin that reads nothing is killed below
                try:
                    if self._writer.is_alive():
                        raise OSError("the plugin does not read its input")
                    self.proc.stdin.close()
                    self.proc.wait(timeout=timeout)
                except (subprocess.TimeoutExpired, OSError):
                    self.proc.kill()
                    self.proc.wait()
        except BaseException:  # e.g. a second Ctrl-C: never leave the child running
            self.kill()
            raise
        self._finish()

    def kill(self) -> None:
        """Stop without asking: for a plugin that no longer answers."""
        if not self.proc:
            return
        if self.proc.poll() is None:
            self.proc.kill()
        if self._ended:
            return
        self.proc.wait()
        self._finish()

    def _finish(self) -> None:
        """The child is gone: release everything of ours, once."""
        self._close_pipes()
        self._remove_container()
        self._log.close()
        self._ended = True

    # messaging -----------------------------------------------------------
    def _send(self, msg: dict) -> None:
        """Queue a message for the plugin; it never waits for the plugin to read."""
        assert self.proc and self.proc.stdin
        line = json.dumps({"jsonrpc": "2.0", **msg}) + "\n"
        with self._out_cv:
            if self._out_closed:
                raise PluginError(f"{self.manifest.id} is not running")
            if len(self._out) >= OUTBOX_CONTROL or self._out_bytes >= OUTBOX_CONTROL_BYTES:
                raise PluginError(f"{self.manifest.id} is not reading its input")
            self._out.append(line)
            self._out_bytes += len(line)
            self._out_cv.notify_all()

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
                    err = msg["error"] if isinstance(msg["error"], dict) else {}
                    code = err.get("code")
                    if isinstance(code, bool) or not isinstance(code, int):
                        code = -32000  # PluginError.code reaches the caller as an error code: it must be a number
                    raise PluginError(str(err.get("message", "plugin error")), code)
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
            if handler is None:
                self._log.write(f"[unknown notification] {_printable(method)}\n")
                return
            if not isinstance(params, dict):
                self._log.write(f"[invalid {_printable(method)}] params must be an object\n")
                return
            try:
                handler(params)
            except PluginError as e:  # an invalid UI message: dropped, the run goes on
                self._log.write(f"[invalid {_printable(method)}] {_printable(str(e))}\n")
            except Exception as e:  # nothing a plugin sends may take the host down
                self._log.write(f"[invalid {_printable(method)}] {type(e).__name__}\n")  # not {e}: a plugin shapes it
            return
        if handler is None:
            self._send({"id": msg_id, "error": {"code": -32601, "message": f"host has no '{method}'"}})
            return
        if not isinstance(params, dict):
            self._send({"id": msg_id, "error": {"code": -32602, "message": "params must be an object"}})
            return
        try:
            self._send({"id": msg_id, "result": handler(params)})
        except Exception as e:  # report any host-side failure to the plugin
            code = e.code if isinstance(e, PluginError) else -32000
            self._send({"id": msg_id, "error": {"code": code, "message": str(e)}})
