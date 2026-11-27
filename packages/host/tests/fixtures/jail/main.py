"""Test fixture plugin, raw protocol (stdlib only): reports what it can reach."""

import json
import os
import socket
import sys


def send(msg):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", **msg}) + "\n")
    sys.stdout.flush()


def output(text):
    send({"method": "output", "params": {"text": text + "\n"}})


def invoke(command, args, cwd, in_dir):
    if command == "net":
        try:
            socket.create_connection(("1.1.1.1", 53), timeout=2).close()
            output("open")
        except OSError:
            output("blocked")
    elif command == "readcwd":
        try:
            with open(os.path.join(cwd, args[0]), encoding="utf-8") as f:
                output(f.read())
        except OSError:
            output("unreadable")
    elif command == "readin":
        try:
            with open(os.path.join(in_dir, args[0]), encoding="utf-8") as f:
                output(f.read())
        except OSError, TypeError:
            output("unreadable")
    elif command == "writein":  # tries to overwrite, create and delete in the in folder; prints what worked
        done = []
        for what, act in (
            ("overwrite", lambda: open(os.path.join(in_dir, args[0]), "w").close()),
            ("create", lambda: open(os.path.join(in_dir, "new.txt"), "w").close()),
            ("delete", lambda: os.remove(os.path.join(in_dir, args[0]))),
        ):
            try:
                act()
                done.append(what)
            except OSError, TypeError:
                pass
        output("denied" if not done else "allowed:" + ",".join(done))
    elif command == "writecwd":  # creates a file in the cwd; prints whether it worked
        try:
            open(os.path.join(cwd, args[0]), "w").close()
            output("written")
        except OSError:
            output("denied")
    elif command == "env":
        value = os.environ.get(args[0])
        output("unset" if value is None else f"set:{value}")
    return 0


for line in sys.stdin:
    if not line.strip():
        continue
    msg = json.loads(line)
    method, msg_id, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if method is None or msg_id is None:
        continue
    if method == "initialize":
        send({"id": msg_id, "result": {"name": "jail"}})
    elif method == "invoke":
        code = invoke(
            params["command"], list(params.get("args", [])), params.get("cwd", "."), params.get("in_dir")
        )
        send({"id": msg_id, "result": {"exit_code": code}})
    elif method == "shutdown":
        send({"id": msg_id, "result": {}})
        break
    else:
        send({"id": msg_id, "error": {"code": -32601, "message": f"unknown method {method}"}})
