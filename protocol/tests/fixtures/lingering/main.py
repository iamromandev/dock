import json
import sys


def send(m):
    print(json.dumps(m), flush=True)


def ok(msg, result):
    send({"jsonrpc": "2.0", "id": msg["id"], "result": result})


for line in sys.stdin:
    msg = json.loads(line)
    method = msg["method"]
    if method == "initialize":
        ok(msg, {"name": "t"})
    elif method == "invoke":
        send({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "unknown command"}})
    elif method == "shutdown":
        ok(msg, {})
        for _ in sys.stdin:
            pass

