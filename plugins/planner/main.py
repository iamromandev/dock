"""planner plugin for dock: turns a request into a flow with a local Ollama model (stdlib only).
JSON-RPC 2.0, one JSON object per line on stdin/stdout. See protocol/PROTOCOL.md.
"""

import json
import os
import queue
import sys
import threading
import urllib.error
import urllib.request

DEFAULT_URL = "http://host.docker.internal:11434"
DEFAULT_MODEL = "qwen2.5:7b"
TIMEOUT = 120  # seconds for the model call
MAX_STEPS = 16


class Cancelled(Exception):
    pass


class Failure(Exception):
    def __init__(self, message, code=-32000):
        super().__init__(message)
        self.code = code


class PlanError(Exception):
    """The model could not give a flow; the message is told to the user."""


inbox: queue.Queue = queue.Queue()
cancelled = threading.Event()

VALUE = {"anyOf": [{"type": "string"}, {"type": "integer"}, {"type": "boolean"}]}
FLOW_SCHEMA = {
    "type": "object",
    "properties": {
        "params": {
            "type": "object",
            "additionalProperties": {
                "type": "object",
                "properties": {"type": {"enum": ["string", "int", "bool"]}, "required": {"type": "boolean"}},
                "required": ["type"],
            },
        },
        "steps": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_STEPS,
            "items": {
                "type": "object",
                "properties": {
                    "plugin": {"type": "string"},
                    "command": {"type": "string"},
                    "inputs": {"type": "object", "additionalProperties": VALUE},
                    "label": {"type": "string"},
                },
                "required": ["plugin", "command"],
            },
        },
    },
    "required": ["steps"],
}

SYSTEM = f"""You turn a request into a flow: an ordered list of steps, each running one command of one plugin.
Answer with one JSON object only, shaped like:
{{"params": {{}}, "steps": [{{"plugin": "<plugin id>", "command": "<command name>", "inputs": {{"<input>": "<value>"}}, "label": "<short text>"}}]}}

Rules:
- Use only the plugins, commands and inputs in the catalog below. Never invent any. Give every required input.
- Do not add steps the request does not ask for: use the fewest steps that do what it asks.
- Do not declare params: write the concrete values from the request into the inputs.
- At most {MAX_STEPS} steps. They run strictly in order; a failing step stops the flow.
- Input values are strings, integers or booleans.
- References inside string values: {{work}} is a private folder every step can write to, {{steps.N.field}} is the
  output field `field` of step N (1-based, an earlier step), {{steps.N.field[i]}} is item i of a list output.
- {{work}} is scratch space, deleted after a successful run. Put a file there only when a later step reads it.
  Files the user asked for go to the current folder: leave out folder and file name inputs (such as `dir` or
  `output`) unless the request names one.
- The `label` is optional.

Catalog:
"""


def send(msg):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", **msg}) + "\n")
    sys.stdout.flush()


def notify(method, **params):
    send({"method": method, "params": params})


def say_error(text):
    notify("log", level="error", message=text)


def read_catalog(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as e:  # UnicodeDecodeError and JSONDecodeError are ValueErrors
        raise PlanError(f"cannot read the catalog {path}: {e}") from e


def ask_model(prompt, catalog, result):
    """Runs in a thread: puts ("flow", obj) or ("error", text) into `result`."""
    base = (os.environ.get("DOCK_PLANNER_URL") or DEFAULT_URL).rstrip("/")
    model = os.environ.get("DOCK_PLANNER_MODEL") or DEFAULT_MODEL
    body = {
        "model": model,
        "stream": False,
        "format": FLOW_SCHEMA,
        "messages": [
            {"role": "system", "content": SYSTEM + json.dumps(catalog, indent=1)},
            {"role": "user", "content": prompt},
        ],
    }
    req = urllib.request.Request(base + "/api/chat", data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            reply = json.loads(resp.read())
        content = reply["message"]["content"]
    except urllib.error.HTTPError as e:
        result.put(("error", f"the model at {base} answered HTTP {e.code} for model {model}"))
        return
    except (OSError, urllib.error.URLError) as e:
        result.put(("error", f"cannot reach the model at {base}: {getattr(e, 'reason', e)}"))
        return
    except (ValueError, KeyError, TypeError):
        result.put(("error", f"the model at {base} sent an answer that is not a chat reply"))
        return
    try:
        flow = json.loads(content)
    except (ValueError, TypeError):
        result.put(("error", "the model's answer is not JSON"))
        return
    result.put(("flow", flow))


def plan(inputs):
    prompt, catalog_path = inputs.get("prompt"), inputs.get("catalog")
    if not isinstance(prompt, str) or not isinstance(catalog_path, str):
        raise Failure("plan needs the inputs prompt and catalog", -32602)
    try:
        catalog = read_catalog(catalog_path)
    except PlanError as e:
        say_error(str(e))
        return {"exit_code": 1}
    result: queue.Queue = queue.Queue()
    threading.Thread(target=ask_model, args=(prompt, catalog, result), daemon=True).start()
    while True:
        if cancelled.is_set():
            raise Cancelled()
        try:
            kind, value = result.get(timeout=0.1)
            break
        except queue.Empty:
            pass
    if kind == "error":
        say_error(value)
        return {"exit_code": 1}
    return {"exit_code": 0, "outputs": {}, "flow": value}


def handle(msg):
    method, msg_id, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if method is None or msg_id is None:
        return False
    try:
        if method == "initialize":
            result = {"name": "planner"}
        elif method == "invoke":
            if params.get("command") != "plan":
                raise Failure(f"unknown command {params.get('command')}", -32601)
            cancelled.clear()
            result = plan(params.get("inputs") or {})
        elif method == "shutdown":
            send({"id": msg_id, "result": {}})
            return True
        else:
            raise Failure(f"unknown method {method}", -32601)
        send({"id": msg_id, "result": result})
    except Cancelled:
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
            cancelled.set()  # seen at once, even while the model call is busy
            continue
        inbox.put(msg)
    inbox.put(None)


threading.Thread(target=read_stdin, daemon=True).start()
while True:
    message = inbox.get()
    if message is None or handle(message):
        break
