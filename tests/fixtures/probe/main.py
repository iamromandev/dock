"""Test fixture plugin, raw protocol (stdlib only)."""

import json
import os
import queue
import socket
import sys
import threading
import time

inbox: queue.Queue = queue.Queue()
cancelled = threading.Event()
data_dir = "."


def send(msg):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", **msg}) + "\n")
    sys.stdout.flush()


def output(text):
    send({"method": "output", "params": {"text": text + "\n"}})


def write_ids():
    """The pid (as the plugin sees it) and the container id (the hostname Docker sets), for tests to read."""
    with open(os.path.join(data_dir, "pid"), "w") as f:
        f.write(str(os.getpid()))
    with open(os.path.join(data_dir, "cid"), "w") as f:
        f.write(socket.gethostname())


def invoke(command, args, params):
    if command in ("fetch", "echoargs", "noargs"):
        output(json.dumps({"args": args, "inputs": params.get("inputs", "<absent>")}))
    if command == "code":
        return int(args[0])
    if command == "crash":
        os._exit(7)
    if command == "pid":
        write_ids()
        output(str(os.getpid()))
    if command == "stubborn":  # ignores cancel: keeps going until the time is up
        write_ids()
        output("stubborn started")
        time.sleep(float(args[0]))
        output("stubborn late")
    if command == "sleep":
        end = time.monotonic() + float(args[0])
        output("sleeping")
        while time.monotonic() < end:
            if cancelled.is_set():
                return None
            time.sleep(0.02)
    return 0


def read_stdin():
    for line in sys.stdin:
        if not line.strip():
            continue
        msg = json.loads(line)
        if msg.get("method") == "cancel":
            cancelled.set()
        inbox.put(msg)
    inbox.put(None)


threading.Thread(target=read_stdin, daemon=True).start()
while True:
    msg = inbox.get()
    if msg is None:
        break
    method, msg_id, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if method is None or msg_id is None:
        continue
    if method == "initialize":
        data_dir = params.get("data_dir", ".")
        send({"id": msg_id, "result": {"name": "probe"}})
    elif method == "invoke":
        cancelled.clear()
        code = invoke(params["command"], list(params.get("args", [])), params)
        if code is None:
            send({"id": msg_id, "error": {"code": -32800, "message": "cancelled"}})
        else:
            send({"id": msg_id, "result": {"exit_code": code}})
    elif method == "shutdown":
        send({"id": msg_id, "result": {}})
        break
    else:
        send({"id": msg_id, "error": {"code": -32601, "message": f"unknown method {method}"}})
