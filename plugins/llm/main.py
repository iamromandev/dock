"""llm plugin for dock: provides the `llm` capability to other plugins.

Written against the raw protocol (stdlib only), see protocol/PROTOCOL.md.

Capability methods (called with the host's `call` request):
  generate {messages: [{role, content}], max_tokens?}
      streams `event {data: {token}}`, returns {text}
  info {}
      returns {backend, model}

Backend is chosen from the environment:
  DOCK_LLM_BASE_URL  OpenAI-compatible server, e.g. http://localhost:11434/v1 (Ollama),
                     http://localhost:1234/v1 (LM Studio), https://api.openai.com/v1
  DOCK_LLM_MODEL     model name, required with DOCK_LLM_BASE_URL
  DOCK_LLM_API_KEY   optional bearer token
Without DOCK_LLM_BASE_URL a small built-in toy model is used.
"""

import json
import os
import queue
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path


class Cancelled(Exception):
    pass


class Failure(Exception):
    def __init__(self, message, code=-32000):
        super().__init__(message)
        self.code = code


# ---- backends ---------------------------------------------------------------
class ToyBackend:
    name, model = "toy", "toy-1"

    def load(self):
        for pct in (25, 60, 100):
            progress(pct, "loading toy model")
            time.sleep(0.08)

    def generate(self, messages, max_tokens=None):
        last = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
        turns = sum(1 for m in messages if m.get("role") == "user")
        t = last.strip().lower()
        if t.startswith(("hi", "hello", "hey")):
            reply = "Hello! I'm a toy model served by the dock llm plugin."
        elif t.endswith("?"):
            reply = f'Good question. A real model would answer "{last.strip()}" here.'
        else:
            reply = f"This is message {turns} of our conversation. Set DOCK_LLM_BASE_URL for a real model."
        for word in reply.split(" "):
            time.sleep(0.015)
            yield word + " "


class OpenAICompatibleBackend:
    name = "openai-compatible"

    def __init__(self, base_url, model, api_key):
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.model = model
        self.api_key = api_key

    def load(self):
        progress(None, f"using {self.model} at {self.url}")

    def generate(self, messages, max_tokens=None):
        body = {"model": self.model, "messages": messages, "stream": True}
        if max_tokens:
            body["max_tokens"] = max_tokens
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(), headers=headers)
        try:
            resp = urllib.request.urlopen(req, timeout=120)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            raise Failure(f"LLM server returned {e.code}: {detail}") from None
        except urllib.error.URLError as e:
            raise Failure(f"cannot reach LLM server at {self.url}: {e.reason}") from None
        with resp:
            for raw in resp:  # server-sent events: "data: {...}" lines
                line = raw.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    return
                try:
                    choice = json.loads(data)["choices"][0]
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue
                token = (choice.get("delta") or {}).get("content")
                if token:
                    yield token


def make_backend():
    base_url = os.environ.get("DOCK_LLM_BASE_URL")
    if not base_url:
        return ToyBackend()
    model = os.environ.get("DOCK_LLM_MODEL")
    if not model:
        raise Failure("DOCK_LLM_BASE_URL is set, so DOCK_LLM_MODEL must be set too")
    return OpenAICompatibleBackend(base_url, model, os.environ.get("DOCK_LLM_API_KEY"))


# ---- protocol plumbing ------------------------------------------------------
inbox: queue.Queue = queue.Queue()
cancelled = threading.Event()
context: dict = {}
backend = None


def send(msg):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", **msg}) + "\n")
    sys.stdout.flush()


def notify(method, **params):
    send({"method": method, "params": params})


def progress(percent, message):
    notify("progress", percent=percent, message=message)


def lock_file():
    return Path(context["data_dir"]) / "model.lock"


# ---- lifecycle ----------------------------------------------------------------
def provision():
    global backend
    backend = make_backend()
    backend.load()
    lock_file().write_text(json.dumps({"pid": os.getpid(), "backend": backend.name}))
    notify("log", level="debug", message=f"{backend.name} backend ready ({backend.model})")


def unprovision():
    """Idempotent, and works in a fresh process: the host also calls it for crash recovery."""
    global backend
    backend = None
    lock_file().unlink(missing_ok=True)
    notify("log", level="debug", message="model released")


# ---- capability methods ---------------------------------------------------------
def generate(params):
    if backend is None:
        raise Failure("llm is not provisioned")
    messages = params.get("messages")
    if not isinstance(messages, list) or not messages:
        raise Failure("generate needs a non-empty 'messages' list", -32602)
    text = []
    for token in backend.generate(messages, params.get("max_tokens")):
        if cancelled.is_set():
            raise Cancelled()
        text.append(token)
        notify("event", data={"token": token})
    return {"text": "".join(text)}


def info(_params):
    if backend is None:
        raise Failure("llm is not provisioned")
    return {"backend": backend.name, "model": backend.model}


METHODS = {"generate": generate, "info": info}


def handle(msg):
    method, msg_id, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if method is None or msg_id is None:
        return False
    try:
        if method == "initialize":
            context.update(params)
            result = {"name": "llm"}
        elif method == "provision":
            provision()
            result = {}
        elif method == "unprovision":
            unprovision()
            result = {}
        elif method == "call":
            fn = METHODS.get(params.get("method"))
            if params.get("capability") != "llm" or fn is None:
                raise Failure(f"llm has no method {params.get('method')!r}", -32601)
            cancelled.clear()
            result = fn(params.get("params") or {})
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
            cancelled.set()  # seen at once, even mid-generation
        inbox.put(msg)
    inbox.put(None)


threading.Thread(target=read_stdin, daemon=True).start()
while True:
    message = inbox.get()
    if message is None or handle(message):
        break
