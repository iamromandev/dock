"""Test fixture plugin, raw protocol (stdlib only), for the v2 UI messages.

`show <script>` plays a named script of messages:
  basic    2 bars, a table and a panel, updated 100 times
  invalid  every kind of invalid message, a v1 `progress` and an unknown notification; then a valid bar
  hostile  escape sequences and control bytes in every kind of text
  slow     prints `slow started`, then updates one bar ten times a second for two minutes, or until `cancel`
`ask` sends a form (text, secret text, confirm, choice), and prints what came back as JSON.
Every `form` error code the host answered with is printed through `output`.
"""

import json
import sys
import time


def send(msg):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", **msg}) + "\n")
    sys.stdout.flush()


def notify(method, **params):
    send({"method": method, "params": params})


def output(text):
    notify("output", text=text + "\n")


def request(req_id, method, **params):
    """Send a request and wait for its answer; other messages (cancel) are ignored."""
    send({"id": req_id, "method": method, "params": params})
    for line in sys.stdin:
        msg = json.loads(line)
        if msg.get("id") == req_id and "method" not in msg:
            return msg
    raise SystemExit(0)


def basic():
    for i in range(1, 101):
        notify("bar", id="a", label="first", percent=i, message=f"{i} of 100", done=i == 100)
        notify("bar", id="b", label="second", percent=i / 2)
        notify("table", id="t", title="peers", columns=["peer", "down"],
               rows=[["1.2.3.4", i], ["5.6.7.8", 2 * i]])
        notify("panel", id="p", title="stats", items=[["speed", f"{i} MB/s"], ["peers", 2]])
        time.sleep(0.005)  # spread over ~0.5 s, so the host draws several frames
    output("basic done")


def invalid():
    notify("progress", percent=5, message="a v1 message")
    notify("mystery", x=1)
    notify("bar", id="bad-percent", percent="lots")
    notify("bar", id="x" * 65, percent=1)
    notify("bar", label="no id")
    notify("table", id="t1", columns=["a"], rows=[["x"]] * 10_001)
    notify("table", id="t2", columns="a", rows=[])
    notify("panel", id="p1", items=[["only-a-key"]])
    notify("clear", id="a", kind="form")
    for i in range(32):
        notify("bar", id=f"area-{i}", label=f"area {i}")
    notify("bar", id="area-32", label="the 33rd")
    for i in range(32):
        notify("clear", id=f"area-{i}", kind="bar")
    fields = [{"name": f"f{i}", "label": "L", "type": "text"} for i in range(33)]
    reply = request("form-1", "form", fields=fields)
    output(f"form error {reply.get('error', {}).get('code')}")
    notify("bar", id="ok", label="still works", percent=100, done=True)
    output("invalid done")


def hostile():
    evil = ["\x1b[31mred\x1b[0m", "\x1b]0;pwned\x07", "\x9b2J", "\x07bell", "back\x08space", "nul\x00", "\x1bc"]
    for i, e in enumerate(evil):
        notify("output", text=f"out {e}\n")
        notify("log", level="info", message=f"log {e}")
        notify("bar", id=f"b{i}", label=f"label {e}", percent=50, message=f"msg {e}")
    notify("table", id="t", title=f"title {evil[0]}", columns=[f"col {evil[1]}"], rows=[[f"cell {evil[2]}"]])
    notify("panel", id="p", title=f"panel {evil[3]}", items=[[f"key {evil[4]}", f"value {evil[5]}"]])
    output("hostile done")


def slow():
    import select

    output("slow started")
    for i in range(1200):
        notify("bar", id="slow", label="slow", percent=i % 100, message="working")
        if select.select([sys.stdin], [], [], 0.1)[0] and '"cancel"' in sys.stdin.readline():
            output("slow cancelled")
            return 0
    return 0


def ask():
    reply = request("form-1", "form", title="Settings", fields=[
        {"name": "name", "label": "Your name", "type": "text", "default": "Ada"},
        {"name": "token", "label": "Token", "type": "text", "secret": True},
        {"name": "ok", "label": "Overwrite?", "type": "confirm", "default": False},
        {"name": "mirror", "label": "Mirror", "type": "choice", "choices": ["eu", "us", "asia"], "default": "eu"},
    ])
    if "error" in reply:
        output(f"error {reply['error']['code']}")
        return 1
    output(json.dumps(reply["result"], sort_keys=True))
    return 0


SCRIPTS = {"basic": basic, "invalid": invalid, "hostile": hostile, "slow": slow}


def invoke(command, inputs):
    if command == "ask":
        return ask()
    script = SCRIPTS.get(inputs.get("script"))
    if script is None:
        output(f"unknown script {inputs.get('script')}")
        return 2
    script()
    return 0


for line in sys.stdin:
    if not line.strip():
        continue
    msg = json.loads(line)
    method, msg_id, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if method is None or msg_id is None:
        continue
    if method == "initialize":
        send({"id": msg_id, "result": {"name": "uiprobe"}})
    elif method == "invoke":
        code = invoke(params["command"], params.get("inputs") or {})
        send({"id": msg_id, "result": {"exit_code": code}})
    elif method == "shutdown":
        send({"id": msg_id, "result": {}})
        break
    else:
        send({"id": msg_id, "error": {"code": -32601, "message": f"unknown method {method}"}})
