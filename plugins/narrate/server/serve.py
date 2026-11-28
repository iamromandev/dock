"""Host-side narrate server: wraps mlx-audio (Kokoro-82M on Apple MLX) behind a small HTTP API.

MLX needs Metal, so it cannot run in a container. The narrate plugin calls this server instead.
POST /speak answers JSON {"audio": base64 int16 LE mono PCM, "sample_rate": 24000} or {"error"}.
GET /health lists the models and voices. Binds 127.0.0.1 only; port from DOCK_NARRATE_PORT (default 7861).
"""

import base64
import json
import os
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODELS = ("kokoro",)
DEFAULT_MODEL = MODELS[0]
REPO = "mlx-community/Kokoro-82M-bf16"  # not gated
VOICES = ("af_heart", "af_bella", "am_michael", "am_fenrir")
MAX_TEXT = 1000  # characters
MIN_SPEED, MAX_SPEED = 0.5, 2.0
MAX_BODY = 65536  # bytes
MAX_ERROR = 500  # characters of an error message
RATE = 24000  # Hz
READ_TIMEOUT = 30  # seconds a client may stall on the socket
HOSTS = ("127.0.0.1", "localhost", "host.docker.internal")  # accepted Host header names
_model = None  # loaded lazily
_gpu = threading.Lock()  # one generation at a time: the GPU and the model are shared
_slots = threading.BoundedSemaphore(2)  # at most one running plus one waiting generation


def _clear_cache():
    """Free MLX's buffer cache after a generation so it does not pile up next to the model."""
    import mlx.core as mx  # imported here so tests run without mlx

    mx.clear_cache()


def _load():
    global _model
    if _model is None:
        from mlx_audio.tts.utils import load_model  # imported here so tests run without mlx

        _model = load_model(REPO)  # ty: ignore[invalid-argument-type]  # the str repo id is accepted at runtime
    return _model


def speak(req):
    """Synthesize req (already validated). Returns int16 little-endian mono PCM bytes at RATE Hz."""
    import numpy as np  # imported here so importing the module stays cheap

    with _gpu:
        model = _load()
        if model.sample_rate != RATE:
            raise RuntimeError(f"model sample rate is {model.sample_rate}, expected {RATE}")
        try:
            parts = [np.array(r.audio, dtype=np.float32) for r in model.generate(
                text=req["text"], voice=req["voice"], speed=req["speed"], lang_code="a")]
        finally:
            _clear_cache()
    audio = np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)
    if audio.size == 0:
        raise RuntimeError("model produced no audio")
    audio = np.nan_to_num(audio, nan=0.0, posinf=1.0, neginf=-1.0)
    return (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()


def parse(body):
    """Validate a request body. Returns the request dict with defaults filled in; raises ValueError."""
    try:
        req = json.loads(body)
    except ValueError:
        raise ValueError("body is not valid JSON") from None
    if not isinstance(req, dict):
        raise ValueError("body must be a JSON object")
    text = req.get("text")
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
        raise ValueError(f"text must be a non-blank string of at most {MAX_TEXT} characters")
    if req.get("voice") not in VOICES:
        raise ValueError(f"voice must be one of {', '.join(VOICES)}")
    speed = req.get("speed")
    if type(speed) not in (int, float) or not MIN_SPEED <= speed <= MAX_SPEED:
        raise ValueError(f"speed must be a number from {MIN_SPEED} to {MAX_SPEED}")
    model = req.get("model", DEFAULT_MODEL)
    if model not in MODELS:
        raise ValueError(f"model must be one of {', '.join(MODELS)}")
    return {"text": text, "voice": req["voice"], "speed": speed, "model": model}


class Handler(BaseHTTPRequestHandler):
    timeout = READ_TIMEOUT

    def log_message(self, format, *args):
        pass

    def _json(self, status, obj):
        data = json.dumps(obj).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except OSError:
            self.close_connection = True  # the client is gone; keep serving others

    def _host_ok(self):
        host = (self.headers.get("Host") or "").lower()
        name = host.rsplit(":", 1)[0] if ":" in host else host
        if name in HOSTS:
            return True
        self._json(403, {"error": "forbidden host"})
        return False

    def _not_allowed(self):
        if self._host_ok():
            self._json(405, {"error": "method not allowed"})

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _not_allowed

    def do_HEAD(self):
        self.send_response(405)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if not self._host_ok():
            return
        if self.path == "/health":
            self._json(200, {"ok": True, "models": list(MODELS), "voices": list(VOICES)})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self._host_ok():
            return
        if self.path != "/speak":
            return self._json(404, {"error": "not found"})
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            return self._json(415, {"error": "Content-Type must be application/json"})
        value = self.headers.get("Content-Length", "")
        length = int(value) if value.isascii() and value.isdigit() else -1
        if length < 0:
            return self._json(400, {"error": "Content-Length must be a non-negative integer"})
        if length > MAX_BODY:
            return self._json(413, {"error": f"body must be at most {MAX_BODY} bytes"})
        try:
            body = self.rfile.read(length)
        except OSError:  # includes the read timeout
            self.close_connection = True
            return
        try:
            req = parse(body)
        except ValueError as e:
            return self._json(400, {"error": str(e)})
        if not _slots.acquire(blocking=False):
            return self._json(503, {"error": "busy: a request is already waiting"})
        try:
            pcm = speak(req)
        except Exception as e:
            traceback.print_exc(file=sys.stderr)
            message = " ".join(f"{type(e).__name__}: {e}".split())
            return self._json(500, {"error": message[:MAX_ERROR]})
        finally:
            _slots.release()
        self._json(200, {"audio": base64.b64encode(pcm).decode(), "sample_rate": RATE})


def make_server(host, port):
    return ThreadingHTTPServer((host, port), Handler)


def main():
    port = int(os.environ.get("DOCK_NARRATE_PORT", "7861"))
    print(f"dock narrate server on http://127.0.0.1:{port}", flush=True)
    make_server("127.0.0.1", port).serve_forever()


if __name__ == "__main__":
    main()
