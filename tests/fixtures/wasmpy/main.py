"""Test fixture plugin for the WASM runner: raw protocol, single-threaded, stdlib only.

Runs on the WASI CPython interpreter, so it has no threads, no sockets and no subprocesses; it reports
what it can reach. It sees `cancel` only between messages.
"""

import json
import os
import socket
import sys

data_dir = "."
counter = 0


def send(msg):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", **msg}) + "\n")
    sys.stdout.flush()


def output(text):
    send({"method": "output", "params": {"text": text + "\n"}})


def read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return "unreadable"


def try_write(path, text="x"):
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return "written"
    except OSError:
        return "denied"


def invoke(command, args, cwd):
    global counter
    if command == "greet":
        output(f"hello, {args[0] if args else 'world'}!")
    elif command == "count":
        counter += 1
        output(str(counter))
    elif command == "writedata":
        path = os.path.join(data_dir, "note.txt")
        try_write(path, args[0])
        output(read(path))
    elif command == "readcwd":
        output(read(os.path.join(cwd, args[0])))
    elif command == "writecwd":
        output(try_write(os.path.join(cwd, args[0]), "from the plugin"))
    elif command == "env":
        value = os.environ.get(args[0])
        output("unset" if value is None else f"set:{value}")
    elif command == "net":
        try:  # WASI has no sockets: this fails whatever the runtime allows; the test pins that dock adds none
            with socket.socket() as s:
                s.settimeout(2)
                s.connect(("1.1.1.1", 53))
            output("open")
        except (OSError, AttributeError):
            output("blocked")
    elif command == "readhome":
        output(read(args[0]))
    elif command == "writeplugin":
        output(try_write("/plugin/injected.py"))
    elif command == "writepython":
        output(try_write("/python/lib/injected.py"))
    return 0


for line in sys.stdin:
    if not line.strip():
        continue
    msg = json.loads(line)
    method, msg_id, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if method is None or msg_id is None:
        continue
    if method == "initialize":
        data_dir = params.get("data_dir", ".")
        send({"id": msg_id, "result": {"name": "wasmpy"}})
    elif method == "invoke":
        code = invoke(params["command"], list(params.get("args", [])), params.get("cwd", "."))
        send({"id": msg_id, "result": {"exit_code": code}})
    elif method == "shutdown":
        send({"id": msg_id, "result": {}})
        break
    else:
        send({"id": msg_id, "error": {"code": -32601, "message": f"unknown method {method}"}})
