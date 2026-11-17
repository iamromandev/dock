"""hello plugin for dock, written against the raw protocol (stdlib only).
JSON-RPC 2.0, one JSON object per line on stdin/stdout. See protocol/PROTOCOL.md.
"""

import json
import queue
import sys
import threading
import time


class Cancelled(Exception):
    pass


class Failure(Exception):
    def __init__(self, message, code=-32000):
        super().__init__(message)
        self.code = code


inbox: queue.Queue = queue.Queue()
deferred: list = []
cancelled = threading.Event()
next_id = 0


def send(msg):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", **msg}) + "\n")
    sys.stdout.flush()


def notify(method, **params):
    send({"method": method, "params": params})


def output(text):
    notify("output", text=text + "\n")


def prompt(text):
    """Ask the host to read a line from the user. None means input ended."""
    global next_id
    next_id += 1
    req_id = f"hello-{next_id}"
    send({"id": req_id, "method": "prompt_user", "params": {"prompt": text}})
    while True:
        msg = inbox.get()
        if msg is None or cancelled.is_set():
            raise Cancelled()
        if msg.get("id") == req_id and "method" not in msg:
            result = msg.get("result") or {}
            return None if result.get("eof") else result.get("text", "")
        deferred.append(msg)


# ---- commands -------------------------------------------------------------
def hello(inputs):
    name = inputs["name"] if "name" in inputs else prompt("what's your name? ")
    output(f"hello, {name or 'stranger'}!")


def count(inputs):
    n = inputs["n"]
    for i in range(1, n + 1):
        if cancelled.is_set():
            raise Cancelled()
        notify("progress", percent=i * 100 / n, message=f"{i}/{n}")
        time.sleep(0.05)
    output(f"counted to {n}")


COMMANDS = {"hello": hello, "count": count}


# ---- dispatch -------------------------------------------------------------
def handle(msg):
    method, msg_id, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if method is None or msg_id is None:
        return False
    try:
        if method == "initialize":
            result = {"name": "hello"}
        elif method == "invoke":
            fn = COMMANDS.get(params.get("command"))
            if fn is None:
                raise Failure(f"unknown command {params.get('command')}", -32601)
            cancelled.clear()
            fn(params.get("inputs") or {})
            result = {"exit_code": 0}
        elif method == "shutdown":
            send({"id": msg_id, "result": {}})
            return True
        else:
            raise Failure(f"unknown method {method}", -32601)
        send({"id": msg_id, "result": result})
    except Cancelled:
        send({"id": msg_id, "error": {"code": -32800, "message": "cancelled"}})
    except Failure as e:
        send({"id": msg_id, "error": {"code": e.code, "message": str(e)}})
    return False


def read_stdin():
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("method") == "cancel":
            cancelled.set()  # seen at once, even while a command is busy
        inbox.put(msg)
    inbox.put(None)


threading.Thread(target=read_stdin, daemon=True).start()
while True:
    message = deferred.pop(0) if deferred else inbox.get()
    if message is None or handle(message):
        break
