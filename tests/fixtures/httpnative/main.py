"""Test fixture plugin for host-relayed HTTP: raw protocol, one thread, stdlib only.

`fetch` sends one `http` request and prints the outcome; `fetch2` sends two at once; `fetch_deaf` sends one and
then never reads again. The others read the `http_event` notifications of the requests in flight until each has
its reply.
"""

import base64
import hashlib
import json
import sys
import time


def send(msg):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", **msg}) + "\n")
    sys.stdout.flush()


def output(text):
    send({"method": "output", "params": {"text": text + "\n"}})


def relay(requests, digest=False):
    """Send every request, then read until each is answered. {key: params} -> {key: outcome}."""
    state = {}
    for key, params in requests.items():
        state[key] = {"status": None, "sha": hashlib.sha256(), "size": 0, "parts": []}
        send({"id": key, "method": "http", "params": params})
    outcomes = {}
    while len(outcomes) < len(requests):
        line = sys.stdin.readline()
        if not line:
            raise SystemExit(1)
        if not line.strip():
            continue
        msg = json.loads(line)
        if msg.get("method") == "http_event":
            p = msg["params"]
            st = state[p["id"]]
            if "status" in p:
                st["status"] = p["status"]
            if "chunk" in p:
                data = base64.b64decode(p["chunk"])
                st["sha"].update(data)
                st["size"] += len(data)
                if not digest:
                    st["parts"].append(data)
        elif "id" in msg and "method" not in msg and msg["id"] in state:
            st = state[msg["id"]]
            if "error" in msg:
                outcomes[msg["id"]] = {"error": msg["error"], "bytes": st["size"]}
            else:
                outcomes[msg["id"]] = {"status": msg["result"]["status"], "reported": msg["result"]["bytes"],
                                       "bytes": st["size"], "sha": st["sha"].hexdigest(), "body": b"".join(st["parts"])}
    return outcomes


def short(o):
    if "error" in o:
        return f"error:{o['error']['code']}:{o['error']['message']}"
    return f"status:{o['status']} bytes:{o['bytes']}"


def fetch(inputs):
    params = {"method": inputs["method"], "url": inputs["url"]}
    if "header" in inputs:
        name, _, value = inputs["header"].partition(":")
        params["headers"] = {name.strip(): value.strip()}
    if "body" in inputs:
        params["body"] = base64.b64encode(inputs["body"].encode()).decode()
    o = relay({"r1": params}, digest=inputs["digest"])["r1"]
    if "error" in o:
        output(short(o))
        return o["error"]["code"]
    output(f"status:{o['status']}")
    if o["reported"] != o["bytes"]:
        output(f"mismatch:{o['reported']}!={o['bytes']}")
    if inputs["digest"]:
        output(f"bytes:{o['bytes']}")
        output(f"sha256:{o['sha']}")
    else:
        output("body:" + o["body"].decode("utf-8", "replace"))
    return 0


def fetch2(inputs):
    a, b = {"method": "GET", "url": inputs["first"]}, {"method": "GET", "url": inputs["second"]}
    if inputs["sequential"]:
        got = {**relay({"a": a}), **relay({"b": b})}
    else:
        got = relay({"a": a, "b": b})
    for key in ("a", "b"):
        output(f"{key}:{short(got[key])}")
    return 0


def fetch_deaf(inputs):
    """Send a request and never read stdin again: what a stuck or malicious plugin does."""
    send({"id": "deaf", "method": "http", "params": {"method": "GET", "url": inputs["url"]}})
    time.sleep(3600)
    return 0


COMMANDS = {"fetch": fetch, "fetch2": fetch2, "fetch_deaf": fetch_deaf}


for line in sys.stdin:
    if not line.strip():
        continue
    msg = json.loads(line)
    method, msg_id, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if method is None or msg_id is None:
        continue
    if method == "initialize":
        send({"id": msg_id, "result": {"name": "httpnative"}})
    elif method == "invoke":
        code = COMMANDS[params["command"]](params.get("inputs") or {})
        if code == -32800:
            send({"id": msg_id, "error": {"code": -32800, "message": "cancelled"}})
        else:
            send({"id": msg_id, "result": {"exit_code": 1 if code else 0}})
    elif method == "shutdown":
        send({"id": msg_id, "result": {}})
        break
    else:
        send({"id": msg_id, "error": {"code": -32601, "message": f"unknown method {method}"}})
