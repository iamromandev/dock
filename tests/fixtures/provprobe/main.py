"""Test fixture plugin with provisioning, raw protocol (stdlib only).

PROVPROBE_PROVISION chooses what `provision` does: ok (default), fail, or slow
(sleeps up to a minute, but gives up on `cancel`). `ping stuck` hangs for a
minute and ignores `cancel`, so the host has to kill it. Every step is appended to
<data_dir>/events as "<pid> <event>".
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


def provision():
    """Returns an error tuple, or None on success."""
    cancelled.clear()
    event("provision-begin")
    mode = os.environ.get("PROVPROBE_PROVISION", "ok")
    if mode == "fail":
        event("provision-failed")
        return -32000, "provision failed on purpose"
    if mode == "slow":
        end = time.monotonic() + 60
        while time.monotonic() < end:
            if cancelled.is_set():
                event("provision-cancelled")
                return -32800, "cancelled"
            time.sleep(0.02)
    event("provision-end")
    return None


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
        send({"id": msg_id, "result": {"name": "provprobe"}})
    elif method == "provision":
        err = provision()
        if err:
            send({"id": msg_id, "error": {"code": err[0], "message": err[1]}})
        else:
            send({"id": msg_id, "result": {}})
    elif method == "unprovision":
        event("unprovision")
        send({"id": msg_id, "result": {}})
    elif method == "invoke":
        if params.get("args") == ["stuck"]:
            send({"method": "output", "params": {"text": "stuck\n"}})
            time.sleep(60)
        send({"method": "output", "params": {"text": "pong\n"}})
        send({"id": msg_id, "result": {"exit_code": 0}})
    elif method == "shutdown":
        send({"id": msg_id, "result": {}})
        break
    else:
        send({"id": msg_id, "error": {"code": -32601, "message": f"unknown method {method}"}})
