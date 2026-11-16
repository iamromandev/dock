"""Minimal helper for writing dock plugins in Python (stdlib only).

The SDK is optional sugar over protocol/PROTOCOL.md. Copy this file next to
your plugin's main.py, then:

    from dock_sdk import Plugin
    plugin = Plugin("hello")

    @plugin.command("hello")
    def hello(p, args):
        p.output(f"hi {args[0] if args else 'there'}")

    plugin.run()
"""

from __future__ import annotations

import itertools
import json
import queue
import sys
import threading
import traceback
from collections.abc import Callable


class Cancelled(Exception):
    pass


class PluginFailure(Exception):
    def __init__(self, message: str, code: int = -32000):
        super().__init__(message)
        self.code = code


class Plugin:
    def __init__(self, name: str):
        self.name = name
        self.context: dict = {}
        self.cancelled = False
        self.cwd: str | None = None
        self._commands: dict[str, Callable] = {}
        self._on_provision: Callable | None = None
        self._on_unprovision: Callable | None = None
        self._inbox: queue.Queue = queue.Queue()
        self._deferred: list[dict] = []
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    # registration -------------------------------------------------------
    def command(self, name: str):
        def deco(fn):
            self._commands[name] = fn
            return fn
        return deco

    def provision(self, fn):
        self._on_provision = fn
        return fn

    def unprovision(self, fn):
        self._on_unprovision = fn
        return fn

    # talking to the host ------------------------------------------------
    def _send(self, msg: dict) -> None:
        with self._lock:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", **msg}) + "\n")
            sys.stdout.flush()

    def notify(self, method: str, **params) -> None:
        self._send({"method": method, "params": params})

    def output(self, text: str, end: str = "\n") -> None:
        self.notify("output", text=text + end)

    def log(self, message: str, level: str = "info") -> None:
        self.notify("log", level=level, message=message)

    def progress(self, percent: float | None, message: str = "") -> None:
        self.notify("progress", percent=percent, message=message)

    def prompt(self, text: str) -> str | None:
        """Ask the user for a line of input. None means input has ended."""
        req_id = f"{self.name}-{next(self._ids)}"
        self._send({"id": req_id, "method": "prompt_user", "params": {"prompt": text}})
        while True:
            msg = self._inbox.get()
            if msg is None:
                raise Cancelled()
            if msg.get("method") == "cancel":
                raise Cancelled()
            if msg.get("id") == req_id and "method" not in msg:
                result = msg.get("result") or {}
                return None if result.get("eof") else result.get("text", "")
            self._deferred.append(msg)  # not ours yet; handle after the command

    def check_cancelled(self) -> None:
        """Call this in long loops so Ctrl-C stops the command."""
        if self.cancelled:
            raise Cancelled()

    # main loop ----------------------------------------------------------
    def _read(self) -> None:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("method") == "cancel":
                self.cancelled = True  # visible immediately to a busy command
            self._inbox.put(msg)
        self._inbox.put(None)

    def run(self) -> None:
        threading.Thread(target=self._read, daemon=True).start()
        while True:
            msg = self._deferred.pop(0) if self._deferred else self._inbox.get()
            if msg is None:
                return
            if self._handle(msg) == "exit":
                return

    def _handle(self, msg: dict):
        method, msg_id, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
        if method is None or msg_id is None:  # stray reply or notification
            return None
        try:
            if method == "initialize":
                self.context = params
                result: dict = {"name": self.name}
            elif method == "provision":
                result = (self._on_provision(self) if self._on_provision else None) or {}
            elif method == "unprovision":
                result = (self._on_unprovision(self) if self._on_unprovision else None) or {}
            elif method == "invoke":
                fn = self._commands.get(params.get("command"))
                if fn is None:
                    raise PluginFailure(f"unknown command {params.get('command')}", -32601)
                self.cancelled = False
                self.cwd = params.get("cwd")  # the user's directory, for relative paths
                code = fn(self, list(params.get("args", [])))
                result = {"exit_code": int(code or 0)}
            elif method == "shutdown":
                self._send({"id": msg_id, "result": {}})
                return "exit"
            else:
                raise PluginFailure(f"unknown method {method}", -32601)
            self._send({"id": msg_id, "result": result})
        except Cancelled:
            self._send({"id": msg_id, "error": {"code": -32800, "message": "cancelled"}})
        except PluginFailure as e:
            self._send({"id": msg_id, "error": {"code": e.code, "message": str(e)}})
        except Exception as e:  # noqa: BLE001 - any bug becomes a clean error for the host
            traceback.print_exc(file=sys.stderr)
            self._send({"id": msg_id, "error": {"code": -32000, "message": f"{type(e).__name__}: {e}"}})
        return None
