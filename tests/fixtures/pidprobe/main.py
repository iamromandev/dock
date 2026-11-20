"""Test fixture plugin, raw protocol (stdlib only).

`pid` prints "pid <n> cid <id>" and "cwd <path>" (the folder the host said in `invoke`) through `output`, so a test
can tell which process, container and folder served it (`cid` is the hostname, which a container runtime sets to the
container's short id). `askhost` sends the host a `call` and an `http` request, which dock no longer has, and prints
the error code of each answer. Every method the host sends is appended to <data_dir>/methods.log, so a test can see
what the host asked for.
"""

import json
import os
import socket
import sys

data_dir = "."
CID = socket.gethostname()


def send(msg):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", **msg}) + "\n")
    sys.stdout.flush()


def output(text):
    send({"method": "output", "params": {"text": text + "\n"}})


def invoke(command, cwd):
    output(f"pid {os.getpid()} cid {CID}")
    output(f"cwd {cwd}")
    if command == "askhost":
        for n, method in enumerate(("call", "http"), 1):
            send({"id": f"ask-{n}", "method": method, "params": {}})
        codes = {}
        while len(codes) < 2:
            msg = json.loads(sys.stdin.readline())
            if str(msg.get("id", "")).startswith("ask-"):
                codes[msg["id"]] = msg.get("error", {}).get("code")
        output(f"call {codes['ask-1']} http {codes['ask-2']}")
    return {"exit_code": 0, "pid": os.getpid(), "cid": CID, "cwd": cwd}


for line in sys.stdin:
    if not line.strip():
        continue
    msg = json.loads(line)
    method, msg_id, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if method is None or msg_id is None:
        continue
    if method == "initialize":
        data_dir = params.get("data_dir", ".")
    with open(os.path.join(data_dir, "methods.log"), "a") as log:
        log.write(method + "\n")
    if method == "initialize":
        send({"id": msg_id, "result": {"name": "pidprobe"}})
    elif method == "invoke":
        send({"id": msg_id, "result": invoke(params["command"], params.get("cwd"))})
    elif method == "shutdown":
        send({"id": msg_id, "result": {}})
        break
    else:
        send({"id": msg_id, "error": {"code": -32601, "message": f"unknown method {method}"}})
