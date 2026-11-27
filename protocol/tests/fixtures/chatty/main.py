import json
import sys
import time


def send(m):
    print(json.dumps(m), flush=True)


def ok(msg, result):
    send({"jsonrpc": "2.0", "id": msg["id"], "result": result})


for _line in sys.stdin:
    while True:
        send({"jsonrpc": "2.0", "method": "output", "params": {"text": "x\n"}})
        time.sleep(0.001)  # slow enough that the checker keeps up: the queue never empties, nothing is dropped
