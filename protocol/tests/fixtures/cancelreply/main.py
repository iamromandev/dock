import json
import sys


def send(m):
    print(json.dumps(m), flush=True)


def ok(msg, result):
    send({"jsonrpc": "2.0", "id": msg["id"], "result": result})


def err(msg, code, text):
    send({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": code, "message": text}})


for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get("method")
    if method == "initialize":
        ok(msg, {"name": "t"})
    elif method == "invoke":
        err(msg, -32601, "unknown command")
    elif method == "shutdown":
        ok(msg, {})
        break
    elif method == "cancel":
        send({"jsonrpc": "2.0", "method": "output", "params": {"text": "cancelled\\n"}})
        pass
