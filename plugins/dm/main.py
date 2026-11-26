"""dm plugin for dock: download one http(s) URL or .torrent file into the current folder (stdlib only).
JSON-RPC 2.0, one JSON object per line on stdin/stdout. See protocol/PROTOCOL.md.
"""

import json
import os
import queue
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import dmcore


class Failure(Exception):
    def __init__(self, message, code=-32000):
        super().__init__(message)
        self.code = code


inbox: queue.Queue = queue.Queue()
cancelled = threading.Event()
data_dir = None  # the plugin's private folder, from initialize
send_lock = threading.Lock()


def send(msg):
    line = json.dumps({"jsonrpc": "2.0", **msg}) + "\n"
    with send_lock:  # the worker's progress and the main thread's replies share stdout
        sys.stdout.write(line)
        sys.stdout.flush()


def notify(method, **params):
    send({"method": method, "params": params})


def handle(msg):
    global data_dir
    method, msg_id, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if method is None or msg_id is None:
        return False
    try:
        if method == "initialize":
            given = params.get("data_dir")
            data_dir = given if isinstance(given, str) and given else None
            result = {"name": "dm"}
        elif method == "invoke":
            if params.get("command") != "dm":
                raise Failure(f"unknown command {params.get('command')}", -32601)
            cancelled.clear()
            outputs = {}
            code = dmcore.run(params.get("inputs") or {}, params.get("cwd") or os.getcwd(), notify, cancelled,
                              data_dir=data_dir, outputs=outputs,
                              work_dir=params.get("work") if isinstance(params.get("work"), str) else None)
            result = {"exit_code": code, "outputs": {"files": outputs.get("files", [])}}
        elif method == "shutdown":
            send({"id": msg_id, "result": {}})
            return True
        else:
            raise Failure(f"unknown method {method}", -32601)
        send({"id": msg_id, "result": result})
    except dmcore.Cancelled:
        send({"id": msg_id, "error": {"code": -32800, "message": "cancelled"}})
    except Failure as e:
        send({"id": msg_id, "error": {"code": e.code, "message": str(e)}})
    return False


def read_stdin():
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("method") == "cancel":
            cancelled.set()  # seen at once, even while a command is busy
        inbox.put(msg)
    inbox.put(None)


threading.Thread(target=read_stdin, daemon=True).start()
while True:
    message = inbox.get()
    if message is None or handle(message):
        break
