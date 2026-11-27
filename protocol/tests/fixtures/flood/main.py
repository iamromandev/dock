import json
import sys


def send(m):
    print(json.dumps(m), flush=True)


# Floods stdout faster than the checker may buffer, hides a non-JSON line in the burst, then replies.
for line in sys.stdin:
    msg = json.loads(line)
    for _ in range(5000):
        send({"jsonrpc": "2.0", "method": "output", "params": {"text": "x\n"}})
    print("not json", flush=True)
    send({"jsonrpc": "2.0", "id": msg["id"], "result": {"name": "flood"}})
