import json
import os
import sys


def send(m):
    print(json.dumps(m), flush=True)


for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get("method")
    if method == "initialize":
        try:
            with open(os.path.join(msg["params"]["data_dir"], "probe"), "w") as f:
                f.write("x")
            writable = True
        except OSError:
            writable = False
        result = {
            "name": "t",
            "uid": os.getuid(),
            "gid": os.getgid(),
            "home": os.environ.get("HOME"),
            "data_writable": writable,
        }
        send({"jsonrpc": "2.0", "id": msg["id"], "result": result})
    elif method == "shutdown":
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {}})
        break
