"""Test fixture plugin for `dock serve`, raw protocol (stdlib only).

Every command prints "pid <n>" through `output`, so a test can tell which process served it. The invoke result
carries extra fields (`pid`, `cwd`), as a plugin may add to it. `sleep` and `work` stop on `cancel`; `stubborn`
ignores it, so the host has to kill the process. `unprovision` takes as many seconds as the file
<data_dir>/slow-unprovision says (when there is one), so a test can hold a shutdown or a close in its cleanup.
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


def notify(method, **params):
    send({"method": method, "params": params})


def output(text):
    notify("output", text=text + "\n")


def invoke(command, inputs, cwd):
    """Returns the result dict, or None when cancelled."""
    output(f"pid {os.getpid()}")
    if command == "crash":
        os._exit(7)
    if command == "work":
        n = inputs["n"]
        for i in range(1, n + 1):
            if cancelled.is_set():
                return None
            notify("progress", percent=i * 100 / n, message=f"{i}/{n}", done=i, total=n)
            time.sleep(float(inputs["delay"]))
    if command in ("sleep", "stubborn"):
        end = time.monotonic() + float(inputs["seconds"])
        while time.monotonic() < end:
            if command == "sleep" and cancelled.is_set():
                return None
            time.sleep(0.02)
    if command == "late-log":
        threading.Timer(0.3, lambda: notify("log", level="info", message="late message")).start()
    return {"exit_code": 0, "pid": os.getpid(), "cwd": cwd}


def read_stdin():
    for line in sys.stdin:
        if not line.strip():
            continue
        msg = json.loads(line)
        if msg.get("method") == "invoke":
            cancelled.clear()  # here, in stream order: a `cancel` that follows is never lost to a late clear
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
        send({"id": msg_id, "result": {"name": "pidprobe"}})
    elif method == "unprovision":
        slow = os.path.join(data_dir, "slow-unprovision")
        if os.path.exists(slow):
            time.sleep(float(open(slow).read()))
        send({"id": msg_id, "result": {}})
    elif method == "provision":
        send({"id": msg_id, "result": {}})
    elif method == "invoke":
        result = invoke(params["command"], params.get("inputs") or {}, params.get("cwd"))
        if result is None:
            send({"id": msg_id, "error": {"code": -32800, "message": "cancelled"}})
        else:
            send({"id": msg_id, "result": result})
    elif method == "shutdown":
        send({"id": msg_id, "result": {}})
        break
    else:
        send({"id": msg_id, "error": {"code": -32601, "message": f"unknown method {method}"}})
