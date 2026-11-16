"""Test fixture command plugin, raw protocol (stdlib only).

`callcap` calls capability `probecap` through the host, prints "calling" and
one "tick" per streamed event, and on `cancel` replies to the invoke with
-32800 at once, without waiting for the call's reply (as chat does).
"""

import json
import sys


def send(msg):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", **msg}) + "\n")
    sys.stdout.flush()


def output(text):
    send({"method": "output", "params": {"text": text + "\n"}})


invoke_id = None
for line in sys.stdin:
    if not line.strip():
        continue
    msg = json.loads(line)
    method, msg_id, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if method == "initialize":
        send({"id": msg_id, "result": {"name": "capcaller"}})
    elif method == "invoke":
        if params["command"] == "quick":
            output("quick")
            send({"id": msg_id, "result": {"exit_code": 0}})
            continue
        invoke_id = msg_id
        output("calling")
        send({"id": "c1", "method": "call", "params": {"capability": "probecap", "method": "run", "params": {}}})
    elif method == "call_event":
        output("tick")
    elif method == "cancel":
        if invoke_id is not None:
            send({"id": invoke_id, "error": {"code": -32800, "message": "cancelled"}})
            invoke_id = None
    elif method is None and msg.get("id") == "c1":  # the call's reply
        if invoke_id is not None:
            send({"id": invoke_id, "result": {"exit_code": 0}})
            invoke_id = None
    elif method == "shutdown":
        send({"id": msg_id, "result": {}})
        break
    elif msg_id is not None:
        send({"id": msg_id, "error": {"code": -32601, "message": f"unknown method {method}"}})
