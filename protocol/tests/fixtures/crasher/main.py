import json
import sys


def send(m):
    print(json.dumps(m), flush=True)


def ok(msg, result):
    send({"jsonrpc": "2.0", "id": msg["id"], "result": result})


for line in sys.stdin:
    msg = json.loads(line)
    if msg["method"] == "initialize":
        ok(msg, {"name": "t"})
    else:
        raise RuntimeError("KNOWN-CRASH-MARKER")
