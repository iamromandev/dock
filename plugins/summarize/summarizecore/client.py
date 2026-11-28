"""The Ollama client: one cancellable POST /api/chat, with errors in words the user can act on."""

import http.client
import json
import re
import secrets
import urllib.parse

from dock_plugin.cancel import Cancelled, Failure, open_socket
from dock_plugin.net import shown

from .prompts import SYSTEM_BASE

TIMEOUT = 600  # seconds to wait for the answer to one call (a 7B model can take minutes on a long text)
CONNECT_TIMEOUT = 10  # seconds to connect (a cancel closes the connecting socket, so it never waits for this)
MAX_REPLY = 8_000_000  # bytes accepted in one answer
ERROR_CHARS = 300  # characters of Ollama's own error that are quoted
NUM_CTX = 8192
THINK = re.compile(r"<think>.*?</think>", re.S | re.I)


def check_url(base):
    """Raises Failure unless base is an http URL with a host and a port in 1-65535 (if given)."""
    try:
        parts = urllib.parse.urlsplit(base)
        port = parts.port
        host = (parts.hostname or "").encode("utf-8", "replace")
        ok = (parts.scheme == "http" and bool(host) and (port is None or 1 <= port <= 65535)
              and len(host) <= 253 and all(len(label) <= 63 for label in host.split(b".")))
    except ValueError:
        ok = False
    if not ok:
        raise Failure("DOCK_SUMMARIZE_URL is not a valid http URL")


def parse_reply(status, data, where, model):
    """Returns (answer text, done_reason) or raises Failure with the message to show."""
    try:
        reply = json.loads(data)
        ok = isinstance(reply, dict)
    except ValueError:
        ok = False
    if not ok:
        reply = {}
    error = reply.get("error")
    text = str(error) if error else ""
    low = text.lower()
    if (status == 404 or "not found" in low) and "model" in low and model.lower() in low:
        raise Failure(f'model {model} is not installed; run "ollama pull {model}"')
    if status == 404:
        raise Failure(f"Ollama at {where} answered 404: {text[:ERROR_CHARS] or 'no error text'}; "
                       "check DOCK_SUMMARIZE_URL")
    if status != 200 or error:
        detail = f": {str(error)[:ERROR_CHARS]}" if error else ""
        raise Failure(f"Ollama at {where} answered HTTP {status}{detail}")
    message = reply.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str):
        raise Failure(f"Ollama at {where} sent a broken answer")
    content = THINK.sub("", content)
    if not content.strip():
        raise Failure("Ollama sent an empty answer")
    used = reply.get("prompt_eval_count")
    if isinstance(used, int) and not isinstance(used, bool) and used >= NUM_CTX - 512:
        raise Failure(f"a chunk did not fit the model's context ({used} tokens); the summary would be wrong")
    return content, reply.get("done_reason")


def post(base, model, instruction, text, call):
    """One POST /api/chat; returns (answer, done_reason). Raises Failure with a message to show, Cancelled after a
    cancel. The call's lock is never held while sending or waiting: a cancel closes the registered socket, which
    ends the send or the read at once. A request is never sent once the call is stopped."""
    where = shown(base)
    parts = urllib.parse.urlsplit(base)
    nonce = secrets.token_hex(8)  # a document cannot guess the markers that end it
    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM_BASE.format(nonce=nonce) + instruction},
                     {"role": "user", "content": f"<<<DOCUMENT-{nonce}\n{text}\nDOCUMENT-{nonce}>>>"}],
        "stream": False,
        "options": {"num_ctx": NUM_CTX},
    }).encode()
    conn = http.client.HTTPConnection(parts.hostname or "", parts.port)
    conn.auto_open = 0
    reaching = True  # still making the connection
    try:
        conn.sock = open_socket(parts.hostname, parts.port or 80, call, CONNECT_TIMEOUT, TIMEOUT)
        with call.lock:
            if call.cancelled():
                raise Cancelled()
        reaching = False
        conn.request("POST", f"{parts.path.rstrip('/')}/api/chat", body, {"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = resp.read(MAX_REPLY + 1)
        status = resp.status
    except (OSError, UnicodeError, http.client.HTTPException) as e:
        if call.cancelled():
            raise Cancelled() from None
        if reaching:
            raise Failure(f'cannot reach Ollama at {where}; start it with "ollama serve" '
                           "or set DOCK_SUMMARIZE_URL") from None
        if isinstance(e, TimeoutError):
            raise Failure(f"timed out after {TIMEOUT:g} s waiting for Ollama") from None
        raise Failure(f"Ollama at {where} stopped answering") from None
    finally:
        conn.close()
    if len(data) > MAX_REPLY:
        raise Failure(f"Ollama at {where} sent an answer that is too large (over {MAX_REPLY} bytes)")
    return parse_reply(status, data, where, model)
