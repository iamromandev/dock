"""story plugin for dock: streams a text story from a local Ollama model into a Markdown file (stdlib only).
JSON-RPC 2.0, one JSON object line on stdin/stdout. See protocol/PROTOCOL.md.
"""

import http.client
import json
import os
import queue
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_URL = "http://host.docker.internal:11434"
DEFAULT_MODEL = "qwen2.5:7b"
TIMEOUT = float(os.environ.get("DOCK_STORY_TIMEOUT") or 300)  # seconds without data from Ollama (tests shorten it)
BAR_EVERY = 0.2  # seconds between progress bars
MAX_PROMPT = 2000
LENGTHS = {"short": (300, 150, 600), "medium": (800, 400, 1500), "long": (1500, 800, 3000)}  # target, min, max words
AUDIENCES = {
    "kids": "The readers are young children. Keep the story gentle and age-appropriate, with a warm tone "
            "and a happy ending: no violence, no gore, no sexual content, nothing frightening.",
    "adult": "The readers are adults. Mature themes are allowed, but no explicit sexual content.",
}


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
pending = 0  # invokes read from stdin and not yet finished; guarded by state_lock. A cancel with none is ignored.


class Call:
    """The Ollama call of one invoke: lets the main thread close the connection on cancel."""

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
    try:
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", **msg}) + "\n")
        sys.stdout.flush()
    except OSError:  # the host is gone (broken pipe); nobody to tell
        pass


def shown(base):
    """The server address without any user:password part, safe to log."""
    parts = urllib.parse.urlsplit(base)
    return f"{parts.scheme}://{parts.netloc.rpartition('@')[2]}" if parts.netloc else base.rpartition("@")[2]


def notify(method, **params):
    send({"method": method, "params": params})


def say(level, text):
    notify("log", level=level, message=text)


def check(inputs, cwd):
    """Returns (audience, length, output path) or raises BadInput."""
    audience = inputs.get("audience") or "kids"
    if audience not in AUDIENCES:
        raise BadInput(f"audience must be one of {', '.join(AUDIENCES)}")
    length = inputs.get("length") or "short"
    if length not in LENGTHS:
        raise BadInput(f"length must be one of {', '.join(LENGTHS)}")
    name = inputs.get("output") or time.strftime("story-%Y%m%d-%H%M%S.md")
    if (not isinstance(name, str) or "/" in name or "\\" in name or ".." in name
            or any(ord(c) < 32 or ord(c) == 127 for c in name)):
        raise BadInput("output must be a file name in the current folder")
    if len(name.encode()) > 240:
        raise BadInput("output name is too long")
    if not name.endswith(".md"):
        raise BadInput("output must end with .md")
    path = os.path.join(cwd, name)
    if os.path.lexists(path):
        raise BadInput(f"{name} already exists")
    return audience, length, path


def system_prompt(audience, length):
    target = LENGTHS[length][0]
    return (f"You are a storyteller. Write one complete story of about {target} words, in plain paragraphs "
            f"separated by blank lines, with no headings or commentary. {AUDIENCES[audience]} "
            "Start with a first line of the form `TITLE: <title>`, then a blank line, then the story.")


def error_text(e):
    try:
        return json.loads(e.read())["error"]
    except (OSError, ValueError, KeyError, TypeError):
        return f"HTTP {e.code}"


def call_model(base, body, out, call):
    """Runs in a thread: puts ("text", chunk)..., then ("done",) or ("error", text) into `out`.
    After `call.stop()` it says nothing more."""
    def put(item):
        if not call.stopped:
            out.put(item)

    try:
        fetch(base, body, put, call)
    except Exception as e:  # never let the thread die silently
        put(("error", f"the Ollama server at {shown(base)} failed: {e}"))


def fetch(base, body, put, call):
    model = body["model"]
    where = shown(base)
    req = urllib.request.Request(base + "/api/chat", data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            call.attach(resp)
            for raw in resp:
                if not raw.strip():
                    continue
                line = json.loads(raw)
                if "error" in line:
                    put(("error", f"the Ollama server at {where} says: {line['error']}"))
                    return
                text = line["message"]["content"]
                if not isinstance(text, str):
                    raise TypeError("content is not text")
                if text:
                    put(("text", text))
                if line.get("done"):
                    put(("done", line.get("done_reason")))
                    return
        put(("error", f"the Ollama server at {where} ended the answer before it was done"))
    except urllib.error.HTTPError as e:
        hint = f" (run `ollama pull {model}`)" if e.code == 404 else ""
        put(("error", f"the Ollama server at {where} refused the request: {error_text(e)}{hint}"))
    except (OSError, urllib.error.URLError) as e:
        put(("error", f"cannot reach the Ollama server at {where}: {getattr(e, 'reason', e)}"))
    except (http.client.HTTPException, ValueError, KeyError, TypeError) as e:
        put(("error", f"the Ollama server at {where} sent a broken answer: {e!r}"))


def to_markdown(text, prompt):
    """Returns (markdown, body words). The title is a `TITLE:` or `# ` first line, else the prompt's first words."""
    lines = text.strip().splitlines()
    title = None
    if lines:
        first = lines[0].strip()
        if first.upper().startswith("TITLE:"):
            title, lines = first[6:].strip().strip("*# ").strip(), lines[1:]
        elif first.startswith("# "):
            title, lines = first[2:].strip(), lines[1:]
    body = "\n".join(lines).strip()
    if not body:
        return None, 0
    if not title:
        title = " ".join(prompt.split()[:6]).title()
    return f"# {title}\n\n{body}\n", len(body.split())


def write_file(path, data, before_publish):
    """Writes `data` to a new file `path`, never replacing one. Only removes the .part it created itself."""
    name = os.path.basename(path)
    part = path + ".part"
    try:
        fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        raise Failure(f"{name}.part already exists", -32000) from None
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
        before_publish()
        try:
            os.link(part, path)
        except FileExistsError:
            raise Failure(f"{name} already exists", -32000) from None
    finally:
        try:
            os.remove(part)
        except OSError:
            pass


def write_story(inputs, cwd):
    prompt = inputs.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise Failure("write needs the input prompt", -32602)
    if len(prompt) > MAX_PROMPT:
        raise Failure(f"prompt must be at most {MAX_PROMPT} characters", -32602)
    if any(not isinstance(inputs.get(k), str | None) for k in ("audience", "length", "output")):
        raise Failure("audience, length and output must be strings", -32602)
    try:
        audience, length, path = check(inputs, os.path.abspath(cwd))
    except BadInput as e:
        say("error", str(e))
        return {"exit_code": 1}
    base = (os.environ.get("DOCK_STORY_URL") or DEFAULT_URL).rstrip("/")
    model = os.environ.get("DOCK_STORY_MODEL") or DEFAULT_MODEL
    target, low, high = LENGTHS[length]
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system_prompt(audience, length)},
                     {"role": "user", "content": prompt.strip()}],
        "stream": True,
        "options": {"num_predict": high * 2},
    }
    out: queue.Queue = queue.Queue()  # one per invoke, so a stale worker can never feed the next one
    call = Call()
    threading.Thread(target=call_model, args=(base, body, out, call), daemon=True).start()
    try:
        return wait_and_publish(out, path, prompt, target, (low, high))
    except BaseException:
        call.stop()  # Ollama sees a disconnect
        raise


def stop_if_cancelled():
    if cancelled.is_set():
        raise Cancelled()


def bar(percent, **extra):
    notify("bar", id="story", label="writing", percent=percent, **extra)


def wait_and_publish(out, path, prompt, target, limits):
    bar(None)
    text = ""
    last_bar = 0.0
    while True:
        stop_if_cancelled()
        try:
            item = out.get(timeout=0.1)
        except queue.Empty:
            continue
        if item[0] == "text":
            text += item[1]
            now = time.monotonic()
            if now - last_bar >= BAR_EVERY:
                last_bar = now
                bar(min(99, int(len(text.split()) / target * 100)))
        elif item[0] == "error":
            say("error", item[1])
            return {"exit_code": 1}
        else:
            if len(item) > 1 and item[1] == "length":
                say("warning", "the model hit its length limit, so the story may be cut off")
            break
    stop_if_cancelled()
    markdown, words = to_markdown(text, prompt)
    if markdown is None:
        say("error", "the model wrote no story text")
        return {"exit_code": 1}
    if not limits[0] <= words <= limits[1]:
        say("warning", f"the story has {words} words, outside the expected {limits[0]}-{limits[1]}")
    name = os.path.basename(path)
    try:
        # Once the file is linked into place the call counts as a success, even if a cancel lands after.
        write_file(path, markdown, stop_if_cancelled)
    except Failure as e:
        say("error", str(e))
        return {"exit_code": 1}
    except OSError as e:
        say("error", f"cannot write {name}: {e}")
        return {"exit_code": 1}
    bar(100, done=True)
    say("info", f"saved {name} ({words} words)")
    return {"exit_code": 0, "outputs": {"files": [path], "words": words}}


def invoke(params):
    if params.get("command") != "write":
        raise Failure(f"unknown command {params.get('command')}", -32601)
    inputs = params.get("inputs") or {}
    cwd = params.get("cwd") or os.getcwd()
    if not isinstance(inputs, dict) or not isinstance(cwd, str):
        raise Failure("inputs must be an object and cwd a string", -32602)
    return write_story(inputs, cwd)


def handle(msg):
    global pending
    method, msg_id, params = msg.get("method"), msg.get("id"), msg.get("params")
    if method is None or msg_id is None:
        return False
    counted = method == "invoke"  # the reader counted this invoke as pending
    try:
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise Failure("params must be an object", -32602)
        if method == "initialize":
            result = {"name": "story"}
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
        say("error", f"story failed: {e!r}")
        send({"id": msg_id, "error": {"code": -32603, "message": f"internal error: {e}"}})
    finally:
        if counted:
            with state_lock:
                pending -= 1
                cancelled.clear()
    return False


def read_stdin():
    global pending
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
                if pending:  # a cancel with nothing pending or running is ignored
                    cancelled.set()  # seen at once, even while the Ollama call is busy
            continue
        if msg.get("method") == "invoke" and msg.get("id") is not None:
            with state_lock:
                pending += 1
        inbox.put(msg)
    with state_lock:
        if pending:  # stdin closed during an invoke: stop generating
            cancelled.set()
    inbox.put(None)


threading.Thread(target=read_stdin, daemon=True).start()
while True:
    message = inbox.get()
    if message is None or handle(message):
        break
