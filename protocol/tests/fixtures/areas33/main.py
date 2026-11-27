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
        for i in range(33):
            send({"jsonrpc": "2.0", "method": "bar", "params": {"id": f"b{i}", "percent": 1}})
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {"exit_code": 0}})
    elif method == "shutdown":
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {}})
        break
