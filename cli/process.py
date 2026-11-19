"""One running plugin: a child process spoken to with JSON-RPC over stdio."""

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

from cli import isolation
from cli.manifest import Manifest


class PluginError(Exception):
    def __init__(self, message: str, code: int = -32000):
        super().__init__(message)
        self.code = code


class SendAborted(PluginError):
    """A sender that was waiting for a plugin to read gave up (see PluginProcess.notify, `abort`)."""


CANCELLED = -32800


class Interrupted(PluginError):
    """A wait ended because its `stop` callback said so (see PluginProcess.wait_for), not because of the plugin."""

    def __init__(self, message: str = "cancelled"):
        super().__init__(message, CANCELLED)


STOP_SLICE = 0.1  # seconds a wait with a `stop` callback pumps at most before it asks again
OUTBOX_BULK = 32       # streamed body events queued for a plugin before their sender has to wait for it to read
OUTBOX_CONTROL = 1024  # every other message queued for a plugin that reads nothing; past this it is stuck
OUTBOX_CONTROL_BYTES = 8 * 2**20  # ... or past this many bytes of them
DEFERRED = object()  # an id handler's answer: "I reply myself, later" (see PluginProcess.reply)


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
        # Requests whose handler needs the request id and may answer later: (params, msg_id) -> result | DEFERRED.
        self.id_handlers: dict[str, Callable[[dict, Any], Any]] = {}
        self.http_active: Any = None  # the http_relay.Relay of this plugin's one relayed request, while it runs
        self.hub = hub or Hub()
        self.exited = False
        self.provisioned = False        # provision was requested; unprovision is owed
        self.forgotten = False          # the session dropped it: its late messages are ignored
        self.pending: set[int] = set()  # ids of requests sent and not yet answered
        self._ids = itertools.count(1)
        # Everything for the plugin's stdin goes through this queue to one writer thread, so a plugin that stops
        # reading can block that thread only: the hub (cancel, shutdown, replies) just queues and carries on.
        self._out: collections.deque = collections.deque()  # (line, bulk)
        self._out_bulk = 0              # bulk entries in _out
        self._out_bytes = 0             # bytes of the other entries
        self._out_closed = False        # no more messages are accepted (stopped, or the pipe broke)
        self._out_cv = threading.Condition()
        self._writer: threading.Thread | None = None
        self.proc: subprocess.Popen | None = None
        # stop() and kill() may come from two threads (a call that is closing, the server shutting down): one
        # at a time, and whichever comes second finds everything done.
        self._end_lock = threading.RLock()
        self._ended = False

    # process -------------------------------------------------------------
    def start(self) -> None:
        launch = self.launch
        cmd = launch.argv
        self._ended = False
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
                line, bulk = self._out.popleft()
                if not bulk:
                    self._out_bytes -= len(line)
            try:
                self.proc.stdin.write(line)
                self.proc.stdin.flush()
            except (OSError, ValueError):  # the plugin is gone, or stdin was closed under us
                with self._out_cv:
                    self._out_closed = True
                    self._out.clear()
                    self._out_bulk = self._out_bytes = 0
                    self._out_cv.notify_all()
                return
            if bulk:
                with self._out_cv:
                    self._out_bulk -= 1
                    self._out_cv.notify_all()

    def _close_outbox(self, discard: bool = False) -> None:
        """Accept no more messages; the writer finishes what is queued, unless `discard`."""
        with self._out_cv:
            self._out_closed = True
            if discard:
                self._out.clear()
                self._out_bulk = self._out_bytes = 0
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
        if self.launch and self.launch.container and not self._container_removed:
            self._container_removed = True
            isolation.remove(self.launch, self._log)

    def stop(self, timeout: float = 3.0) -> None:
        if not self.proc:
            return
        with self._end_lock:
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
            self.proc.kill()  # not under the lock: it also ends a stop() that is waiting for the plugin
        with self._end_lock:
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
    def _send(self, msg: dict, bulk: bool = False, abort: Callable[[], bool] | None = None) -> None:
        """Queue a message for the plugin; it never waits for the plugin to read, except `bulk` when `abort` is given.

        A bulk message (a piece of a streamed body) waits while OUTBOX_BULK of them are already queued, and gives up
        with SendAborted as soon as `abort()` says so. Any other message is queued at once.
        """
        assert self.proc and self.proc.stdin
        line = json.dumps({"jsonrpc": "2.0", **msg}) + "\n"
        with self._out_cv:
            while True:
                if self._out_closed:
                    raise PluginError(f"{self.manifest.id} is not running")
                if not bulk:
                    if len(self._out) - self._out_bulk >= OUTBOX_CONTROL or self._out_bytes >= OUTBOX_CONTROL_BYTES:
                        raise PluginError(f"{self.manifest.id} is not reading its input")
                    break
                if self._out_bulk < OUTBOX_BULK:
                    break
                if abort is not None and abort():
                    raise SendAborted(f"gave up sending to {self.manifest.id}: it is not reading its input")
                self._out_cv.wait(0.05)
            self._out.append((line, bulk))
            if bulk:
                self._out_bulk += 1
            else:
                self._out_bytes += len(line)
            self._out_cv.notify_all()

    def notify(self, method: str, params: dict | None = None, bulk: bool = False,
               abort: Callable[[], bool] | None = None) -> None:
        self._send({"method": method, "params": params or {}}, bulk, abort)

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

    def reply(self, msg_id, result: Any = None, error: dict | None = None) -> None:
        """Answer a plugin's request from another thread. Raises PluginError if the plugin is gone."""
        if self.forgotten:
            raise PluginError(f"{self.manifest.id} is not running")
        self._send({"id": msg_id, **({"error": error} if error is not None else {"result": result})})

    def request(self, method: str, params: dict | None = None, timeout: float | None = None,
                stop: Callable[[], bool] | None = None) -> Any:
        return self.wait_for(self.send_request(method, params), method, timeout, stop)

    def wait_for(self, req_id: int, method: str, timeout: float | None,
                 stop: Callable[[], bool] | None = None) -> Any:
        """Wait for our reply to req_id, serving every plugin's messages meanwhile.

        stop: asked at least every STOP_SLICE seconds; once it is true (and the reply is not already here) the
        wait ends with Interrupted. That is how another thread cancels a wait that Ctrl-C cannot reach.
        """
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

            if stop is not None and stop():
                raise Interrupted()
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            if stop is not None:
                remaining = STOP_SLICE if remaining is None else min(remaining, STOP_SLICE)
            if not self.hub.pump(remaining):
                if stop is not None and (deadline is None or time.monotonic() < deadline):
                    continue  # only the slice ended: ask `stop` again
                raise PluginError(f"{self.manifest.id} did not answer '{method}' in {timeout}s") from None

    def _handle_incoming(self, msg: dict) -> None:
        method, params, msg_id = msg["method"], msg.get("params") or {}, msg.get("id")
        handler = self.handlers.get(method)
        with_id = self.id_handlers.get(method) if msg_id is not None else None
        if msg_id is None:  # notification
            if handler:
                handler(params)
            return
        if handler is None and with_id is None:
            self._send({"id": msg_id, "error": {"code": -32601, "message": f"host has no '{method}'"}})
            return
        try:
            result = with_id(params, msg_id) if with_id is not None else handler(params)
            if result is DEFERRED:
                return
            self._send({"id": msg_id, "result": result})
        except Exception as e:  # noqa: BLE001 - report any host-side failure to the plugin
            code = e.code if isinstance(e, PluginError) else -32000
            self._send({"id": msg_id, "error": {"code": code, "message": str(e)}})
