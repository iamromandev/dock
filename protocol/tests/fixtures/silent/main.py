import json
import sys


def send(m):
    print(json.dumps(m), flush=True)


def ok(msg, result):
    send({"jsonrpc": "2.0", "id": msg["id"], "result": result})


def err(msg, code, text):
    send({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": code, "message": text}})


import time

for _ in sys.stdin:
    time.sleep(0)
