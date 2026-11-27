import json
import sys


def send(m):
    print(json.dumps(m), flush=True)


for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get("method")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {"name": "t"}})
    elif method == "invoke":
        field = {"name": "x", "label": "X", "type": "text"}
        send({"jsonrpc": "2.0", "id": 100, "method": "form", "params": {"fields": [field, field]}})
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {"exit_code": 0}})
    elif method == "shutdown":
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {}})
        break
