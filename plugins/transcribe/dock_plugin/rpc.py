"""The stdio JSON-RPC 2.0 loop of a plugin: one JSON object per line on stdin and stdout. See protocol/PROTOCOL.md.

    rpc.run({"name": command}, "name")   with   command(inputs: dict, ctx: Context) -> dict of outputs

A reader thread takes lines from stdin so a cancel is seen at once, even while a command is busy. Closing stdin
during an invoke is a cancel."""

import json
import os
import queue
import sys
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from . import ui
from .cancel import Call, Cancelled, Failure, cancelled, stop_if_cancelled
from .ui import say, send

__all__ = ["Cancelled", "Context", "Failure", "handle", "optional_str", "require_str", "run"]

Command = Callable[[dict, "Context"], dict]

inbox: queue.Queue = queue.Queue()
state_lock = threading.Lock()
pending = 0  # invokes read from stdin and not yet finished; guarded by state_lock. A cancel with none is ignored.
replies: dict[str, queue.Queue] = {}  # request id -> where the reader puts the host's answer; guarded by state_lock
request_ids = iter(range(1, 1 << 62))  # ids of our requests ("x1", ...), apart from the host's own ids
commands: Mapping[str, Command] = {}
plugin_name = "plugin"


@dataclass
class Context:
    """What a command gets besides its inputs."""

    cwd: str
    in_dir: str | None
    work: str | None
    call: Call  # this invoke's: register sockets and processes here so a cancel can stop them

    def ask(self, method: str, params: dict | None = None) -> dict:
        """A request to the host; waits for its result. {"eof": True} when stdin closes first."""
        return ask(method, params or {})


def require_str(inputs: dict, name: str) -> str:
    value = inputs.get(name)
    if not isinstance(value, str) or not value:
        raise Failure(f"{plugin_name} needs the input {name}", -32602)
    return value


def optional_str(inputs: dict, name: str) -> str | None:
    value = inputs.get(name)
    if not isinstance(value, str | None):
        raise Failure(f"{name} must be a string", -32602)
    return value


def ask(method: str, params: dict) -> dict:
    """A request to the host; waits for its answer (the result), checking for a cancel every 0.1 s."""
    with state_lock:
        req_id = f"x{next(request_ids)}"
        box: queue.Queue = queue.Queue()
        replies[req_id] = box
    try:
        send({"id": req_id, "method": method, "params": params})
        while True:
            try:
                reply = box.get(timeout=0.1)
                break
            except queue.Empty:
                if box.empty():
                    stop_if_cancelled()
    finally:
        with state_lock:
            replies.pop(req_id, None)  # an answer that comes later finds nobody and is dropped
    if reply.get("eof"):
        return {"eof": True}
    stop_if_cancelled()
    if "error" in reply:
        error = reply["error"]
        raise Failure(str(error.get("message") if isinstance(error, dict) else error)[:200])
    result = reply.get("result")
    return result if isinstance(result, dict) else {}


def invoke(params: dict) -> dict:
    name = params.get("command")
    if not isinstance(name, str):
        raise Failure("command must be a string", -32602)
    command = commands.get(name)
    if command is None:
        raise Failure(f"unknown command {name[:100]}", -32601)
    inputs = params.get("inputs") or {}
    cwd = params.get("cwd") or os.getcwd()
    in_dir, work = params.get("in_dir"), params.get("work")
    if not isinstance(inputs, dict) or not isinstance(cwd, str):
        raise Failure("inputs must be an object and cwd a string", -32602)
    if any(v is not None and not isinstance(v, str) for v in (in_dir, work)):
        raise Failure("in_dir and work must be strings", -32602)
    try:
        outputs: Any = command(inputs, Context(os.path.abspath(cwd), in_dir, work, Call()))
    except Failure as e:
        if e.code is not None:
            raise
        say("error", str(e))
        return {"exit_code": 1}
    return {"exit_code": 0, "outputs": outputs}


def handle(msg: dict) -> bool:
    """Answers one request from the host; True after shutdown."""
    global pending
    method, msg_id, params = msg.get("method"), msg.get("id"), msg.get("params")
    if method is None or msg_id is None:
        return False
    counted = method == "invoke"  # the reader counted this invoke as pending
    try:
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise Failure("params must be an object", -32602)
        if method == "initialize":
            result = {"name": plugin_name}
        elif method == "invoke":
            result = invoke(params)
        elif method == "shutdown":
            send({"id": msg_id, "result": {}})
            return True
        else:
            raise Failure(f"unknown method {method}", -32601)
        send({"id": msg_id, "result": result})
    except Cancelled:
        send({"id": msg_id, "error": {"code": -32800, "message": "cancelled"}})
    except Failure as e:
        send({"id": msg_id, "error": {"code": -32000 if e.code is None else e.code, "message": str(e)}})
    except Exception as e:  # the plugin must always answer; only the type, since a message may quote the input
        say("error", f"{plugin_name} failed: {type(e).__name__}")
        send({"id": msg_id, "error": {"code": -32603, "message": f"internal error: {type(e).__name__}"}})
    finally:
        if counted:
            with state_lock:
                pending -= 1
                cancelled.clear()
    return False


def read_stdin():
    global pending
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(msg, dict):
            continue
        if "method" not in msg and isinstance(msg.get("id"), str) and ("result" in msg or "error" in msg):
            with state_lock:  # the answer to one of our requests; one nobody waits for (any more) is dropped
                box = replies.get(msg["id"])
            if box:
                box.put(msg)
            continue
        if msg.get("method") == "cancel":
            with state_lock:
                if pending:  # a cancel with nothing pending or running is ignored
                    cancelled.set()  # seen at once, even while a request is busy
            continue
        if msg.get("method") == "invoke" and msg.get("id") is not None:
            with state_lock:
                pending += 1
        inbox.put(msg)
    with state_lock:
        for box in replies.values():  # whoever waits for the host is told it is gone
            box.put({"eof": True})
        if pending:  # stdin closed during an invoke: stop
            cancelled.set()
    inbox.put(None)


def setup(table: Mapping[str, Command], name: str, label: str | None = None):
    """Sets the commands, the plugin name (answered to initialize) and the bar's id and label (default: name)."""
    global commands, plugin_name
    commands, plugin_name = table, name
    ui.configure(name, label or name)


def run(table: Mapping[str, Command], name: str, label: str | None = None):
    """Serves the commands over stdio until shutdown or the end of stdin."""
    setup(table, name, label)
    threading.Thread(target=read_stdin, daemon=True).start()
    while True:
        message = inbox.get()
        if message is None or handle(message):
            break
