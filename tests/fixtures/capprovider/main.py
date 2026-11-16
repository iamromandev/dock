"""Test fixture provider, raw protocol (stdlib only).

Serves capability `probecap`. A `call` emits an `event` every 0.1 s for up to
30 s; on `cancel` it waits CAPPROVIDER_CANCEL_DELAY seconds (default 0.3) and
then replies with -32800, like a well-behaved plugin that needs a moment to
stop. Every step is appended to <data_dir>/events as "<pid> <event>".
"""

import json
import os
import queue
import sys
import threading
import time

inbox: queue.Queue = queue.Queue()
cancelled = threading.Event()
data_dir = "."


def send(msg):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", **msg}) + "\n")
    sys.stdout.flush()


def event(name):
    with open(os.path.join(data_dir, "events"), "a") as f:
        f.write(f"{os.getpid()} {name}\n")


def call(msg_id):
    cancelled.clear()
    event("call-begin")
    end = time.monotonic() + 30
    while time.monotonic() < end:
        if cancelled.wait(0.1):
            time.sleep(float(os.environ.get("CAPPROVIDER_CANCEL_DELAY", "0.3")))
            event("call-cancelled")
            send({"id": msg_id, "error": {"code": -32800, "message": "cancelled"}})
            return
        send({"method": "event", "params": {"data": "tick"}})
    send({"id": msg_id, "result": {"text": "done"}})


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
        event("initialize")
        send({"id": msg_id, "result": {"name": "capprovider"}})
    elif method == "provision":
        event("provision")
        send({"id": msg_id, "result": {}})
    elif method == "unprovision":
        event("unprovision")
        send({"id": msg_id, "result": {}})
    elif method == "call":
        call(msg_id)
    elif method == "shutdown":
        send({"id": msg_id, "result": {}})
        break
    else:
        send({"id": msg_id, "error": {"code": -32601, "message": f"unknown method {method}"}})
