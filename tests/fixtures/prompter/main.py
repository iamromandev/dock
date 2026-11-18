"""Test fixture plugin: one `prompt_user`, then `answer: <text>` (or `answer: <eof>`) through `output`.

Raw protocol, stdlib only. It does not stop on `cancel`; it ends when the host answers its question, which
is what a host must do when it withdraws a pending prompt (`{eof: true}`).
"""

import json
import queue
import sys
import threading

inbox: queue.Queue = queue.Queue()


def send(msg):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", **msg}) + "\n")
    sys.stdout.flush()


def ask(question):
    send({"id": "q1", "method": "prompt_user", "params": {"prompt": question}})
    while True:
        msg = inbox.get()
        if msg is None:
            return None
        if msg.get("id") == "q1" and "method" not in msg:
            result = msg.get("result") or {}
            return None if result.get("eof") else result.get("text", "")
        # anything else (a `cancel`, say) is ignored: the host's answer is what ends the wait


def read_stdin():
    for line in sys.stdin:
        if line.strip():
            inbox.put(json.loads(line))
    inbox.put(None)


threading.Thread(target=read_stdin, daemon=True).start()
while True:
    msg = inbox.get()
    if msg is None:
        break
    method, msg_id, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if method is None or msg_id is None:
        continue
    if method == "initialize":
        send({"id": msg_id, "result": {"name": "prompter"}})
    elif method == "invoke":
        answer = ask((params.get("inputs") or {}).get("question", "name? "))
        text = "<eof>" if answer is None else answer
        send({"method": "output", "params": {"text": f"answer: {text}\n"}})
        send({"id": msg_id, "result": {"exit_code": 0}})
    elif method == "shutdown":
        send({"id": msg_id, "result": {}})
        break
    else:
        send({"id": msg_id, "error": {"code": -32601, "message": f"unknown method {method}"}})
