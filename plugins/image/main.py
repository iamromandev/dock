"""image plugin for dock: makes a PNG from a prompt with the host image server (stdlib only).
JSON-RPC 2.0, one JSON object per line on stdin/stdout. See protocol/PROTOCOL.md.
"""

import base64
import binascii
import http.client
import json
import os
import queue
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

DEFAULT_URL = "http://host.docker.internal:7860"
TIMEOUT = 300  # seconds without an answer from the server
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
MODELS = ("z-image-turbo",)


class Cancelled(Exception):
    pass


class Failure(Exception):
    def __init__(self, message, code=-32000):
        super().__init__(message)
        self.code = code


class BadInput(Exception):
    """An input the user can fix; the message is told to the user."""


inbox: queue.Queue = queue.Queue()
cancelled = threading.Event()
state_lock = threading.Lock()
in_flight = False  # an invoke is running; guarded by state_lock. A cancel outside one is ignored.


class Call:
    """The server call of one invoke: lets the main thread close the connection on cancel."""

    def __init__(self):
        self.resp = None
        self.stopped = False
        self.lock = threading.Lock()

    def attach(self, resp):
        with self.lock:
            self.resp = resp
            if self.stopped:
                self.hang_up(resp)

    def hang_up(self, resp):
        # close() alone leaves the socket open while the worker is blocked reading it; shut it down first
        sock = getattr(getattr(resp.fp, "raw", None), "_sock", None)
        try:
            if sock is not None:
                sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        resp.close()

    def stop(self):
        with self.lock:
            self.stopped = True
            if self.resp is not None:
                self.hang_up(self.resp)


def send(msg):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", **msg}) + "\n")
    sys.stdout.flush()


def notify(method, **params):
    send({"method": method, "params": params})


def say(level, text):
    notify("log", level=level, message=text)


def int_in(inputs, key, low, high, default, multiple=1):
    value = inputs.get(key)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high or value % multiple:
        step = f", a multiple of {multiple}" if multiple > 1 else ""
        raise BadInput(f"{key} must be an integer from {low} to {high}{step}")
    return value


def size_in(inputs, key, low=256, high=2048, default=512, multiple=16):
    """A side in pixels, rounded to the nearest multiple and clamped to [low, high]: a planner's 1080 becomes 1088."""
    value = inputs.get(key)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise BadInput(f"{key} must be an integer")
    return min(high, max(low, (value + multiple // 2) // multiple * multiple))


def check(inputs, cwd):
    """Returns (request body, output path) or raises BadInput."""
    prompt = inputs["prompt"]
    model = inputs.get("model") or os.environ.get("DOCK_IMAGE_MODEL") or MODELS[0]
    if model not in MODELS:
        raise BadInput(f"model must be one of {', '.join(MODELS)}")
    body = {
        "prompt": prompt,
        "width": size_in(inputs, "width"),
        "height": size_in(inputs, "height"),
        "steps": int_in(inputs, "steps", 1, 50, None),
        "seed": int_in(inputs, "seed", 0, 2**32 - 1, None),
        "model": model,
    }
    name = inputs.get("output") or time.strftime("image-%Y%m%d-%H%M%S.png")
    if (not isinstance(name, str) or "/" in name or "\\" in name or ".." in name
            or any(ord(c) < 32 or ord(c) == 127 for c in name)):
        raise BadInput("output must be a file name in the current folder")
    if not name.endswith(".png"):
        raise BadInput("output must end with .png")
    path = os.path.join(cwd, name)
    if os.path.lexists(path):
        raise BadInput(f"{name} already exists")
    return body, path


def error_text(e):
    """The message of an HTTP error answer: the server's {"error"} when it sent one."""
    try:
        return json.loads(e.read())["error"]
    except (OSError, ValueError, KeyError, TypeError):
        return f"HTTP {e.code}"


def valid_progress(step, steps):
    ints = all(isinstance(v, int) and not isinstance(v, bool) for v in (step, steps))
    return ints and steps > 0 and 0 <= step <= steps


def call_server(base, body, out, call):
    """Runs in a thread: puts ("step", n, N), then ("png", bytes, seed) or ("error", text) into `out`.
    After `call.stop()` it says nothing more."""
    def put(item):
        if not call.stopped:
            out.put(item)

    try:
        fetch(base, body, put, call)
    except Exception as e:  # never let the thread die silently
        put(("error", f"the image server at {base} failed: {e}"))


def fetch(base, body, put, call):
    req = urllib.request.Request(base + "/generate", data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            call.attach(resp)
            for raw in resp:
                if not raw.strip():
                    continue
                line = json.loads(raw)
                if "error" in line:
                    put(("error", f"the image server says: {line['error']}"))
                    return
                if "png" in line:
                    data = base64.b64decode(line["png"], validate=True)
                    if not data.startswith(PNG_MAGIC):
                        put(("error", "the image server sent data that is not a PNG"))
                        return
                    put(("png", data, line.get("seed")))
                    return
                if not valid_progress(line["step"], line["steps"]):
                    put(("error", "the image server sent a bad progress line"))
                    return
                put(("step", line["step"], line["steps"]))
        put(("error", "the image server ended the answer without an image"))
    except urllib.error.HTTPError as e:
        put(("error", f"the image server at {base} refused the request: {error_text(e)}"))
    except (OSError, urllib.error.URLError) as e:
        put(("error", f"cannot reach the image server at {base}: {getattr(e, 'reason', e)}"))
    except (http.client.HTTPException, ValueError, KeyError, TypeError, binascii.Error) as e:
        put(("error", f"the image server at {base} sent a broken answer: {e}"))


def write_png(path, data, before_publish):
    """Writes `data` to a new file `path`, never replacing one. Only removes the .part it created itself."""
    name = os.path.basename(path)
    part = path + ".part"
    try:
        fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        raise Failure(f"{name}.part already exists", -32000) from None
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        before_publish()
        try:
            os.link(part, path)
        except FileExistsError:
            raise Failure(f"{name} already exists", -32000) from None
    finally:
        os.remove(part)


def generate(inputs, cwd):
    if not isinstance(inputs.get("prompt"), str) or not inputs["prompt"].strip():
        raise Failure("image needs the input prompt", -32602)
    try:
        body, path = check(inputs, os.path.abspath(cwd))
    except BadInput as e:
        say("error", str(e))
        return {"exit_code": 1}
    base = (os.environ.get("DOCK_IMAGE_URL") or DEFAULT_URL).rstrip("/")
    out: queue.Queue = queue.Queue()  # one per invoke, so a stale worker can never feed the next one
    call = Call()
    threading.Thread(target=call_server, args=(base, body, out, call), daemon=True).start()
    try:
        return wait_and_publish(out, path)
    except BaseException:
        call.stop()  # the server sees a disconnect
        raise


def stop_if_cancelled():
    if cancelled.is_set():
        raise Cancelled()


def wait_and_publish(out, path):
    notify("bar", id="gen", label="generating", percent=None)
    while True:
        stop_if_cancelled()
        try:
            item = out.get(timeout=0.1)
        except queue.Empty:
            continue
        if item[0] == "step":
            _, n, total = item
            notify("bar", id="gen", label="generating", percent=min(100, max(0, n / total * 100)))
        elif item[0] == "error":
            say("error", item[1])
            return {"exit_code": 1}
        else:
            break
    stop_if_cancelled()
    _, data, seed = item
    try:
        # Once the file is linked into place the call counts as a success, even if a cancel lands after.
        write_png(path, data, stop_if_cancelled)
    except Failure as e:
        say("error", str(e))
        return {"exit_code": 1}
    except OSError as e:
        say("error", f"cannot write {os.path.basename(path)}: {e}")
        return {"exit_code": 1}
    notify("bar", id="gen", label="generating", percent=100, done=True)
    say("info", f"seed {seed}")
    return {"exit_code": 0, "outputs": {"files": [path]}}


def invoke(params):
    if params.get("command") != "image":
        raise Failure(f"unknown command {params.get('command')}", -32601)
    inputs = params.get("inputs") or {}
    cwd = params.get("cwd") or os.getcwd()
    if not isinstance(inputs, dict) or not isinstance(cwd, str):
        raise Failure("inputs must be an object and cwd a string", -32602)
    global in_flight
    with state_lock:
        in_flight = True
    try:
        return generate(inputs, cwd)
    finally:
        with state_lock:
            in_flight = False
            cancelled.clear()


def handle(msg):
    method, msg_id, params = msg.get("method"), msg.get("id"), msg.get("params")
    if method is None or msg_id is None:
        return False
    try:
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise Failure("params must be an object", -32602)
        if method == "initialize":
            result = {"name": "image"}
        elif method == "invoke":
            result = invoke(params)
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
    except Exception as e:  # the plugin must always answer
        say("error", f"image failed: {e!r}")
        send({"id": msg_id, "error": {"code": -32603, "message": f"internal error: {e}"}})
    return False


def read_stdin():
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(msg, dict):
            continue
        if msg.get("method") == "cancel":
            with state_lock:
                if in_flight:  # a cancel with nothing running is ignored
                    cancelled.set()  # seen at once, even while the server call is busy
            continue
        inbox.put(msg)
    inbox.put(None)


threading.Thread(target=read_stdin, daemon=True).start()
while True:
    message = inbox.get()
    if message is None or handle(message):
        break
