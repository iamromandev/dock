"""Host-side image server: wraps mflux (Z-Image-Turbo 4-bit on Apple MLX) behind a small HTTP API.

mflux needs Metal, so it cannot run in a container. The image plugin calls this server instead.
POST /generate streams NDJSON ({"step","steps"} lines, then {"png","seed"} or {"error"}).
GET /health lists the models. Binds 127.0.0.1 only; port from DOCK_IMAGE_PORT (default 7860).
"""

import base64
import io
import json
import os
import random
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODELS = ("z-image-turbo",)
DEFAULT_MODEL = MODELS[0]
REPO = "filipstrand/Z-Image-Turbo-mflux-4bit"  # pre-quantized, not gated
DEFAULT_STEPS = 9
MIN_SIZE, MAX_SIZE, SIZE_MULTIPLE = 256, 2048, 16
MIN_STEPS, MAX_STEPS = 1, 50
MAX_PROMPT = 2000
MAX_BODY = 65536  # bytes
MAX_ERROR = 500  # characters of an error line
_models = {}  # model name -> loaded mflux ZImage, filled lazily
_gpu = threading.Lock()  # one generation at a time: the GPU and the model cache are shared


def _load(name):
    if name not in _models:
        from mflux.models.common.config import ModelConfig  # imported here so tests run without mflux
        from mflux.models.z_image.variants.z_image import ZImage

        _models[name] = ZImage(model_config=ModelConfig.z_image_turbo(), model_path=REPO)
    return _models[name]


def _clear_cache():
    """Free MLX's buffer cache. A render leaves about 4 GB cached next to the 5.4 GB model; kept, it pushes the next
    render into swap on a 16 GB Mac and doubles its time (measured on an M1: 27 s cleared, 58 s kept)."""
    import mlx.core as mx  # imported here so tests run without mlx

    mx.clear_cache()


class _ClientGone(Exception):
    """Writing to the client failed: it disconnected."""


class _Progress:
    """mflux in-loop callback: reports 1-based steps."""

    def __init__(self, on_step):
        self.on_step = on_step

    def call_in_loop(self, t, seed, prompt, latents, config, time_steps):
        self.on_step(t + 1, config.num_inference_steps)


def generate(req, on_step):
    """Render req (already validated, steps and seed filled in). Returns (png_bytes, seed)."""
    with _gpu:
        model = _load(req["model"])
        model.callbacks.register(progress := _Progress(on_step))
        try:
            image = model.generate_image(
                seed=req["seed"],
                prompt=req["prompt"],
                num_inference_steps=req["steps"],
                width=req["width"],
                height=req["height"],
            )
        finally:
            model.callbacks.in_loop.remove(progress)
            _clear_cache()
    buf = io.BytesIO()
    image.image.save(buf, format="PNG")
    return buf.getvalue(), req["seed"]


def parse(body):
    """Validate a request body. Returns the request dict with defaults filled in; raises ValueError."""
    try:
        req = json.loads(body)
    except ValueError:
        raise ValueError("body is not valid JSON") from None
    if not isinstance(req, dict):
        raise ValueError("body must be a JSON object")
    if not isinstance(req.get("prompt"), str) or not req["prompt"] or len(req["prompt"]) > MAX_PROMPT:
        raise ValueError(f"prompt must be a non-empty string of at most {MAX_PROMPT} characters")
    for key in ("width", "height"):
        v = req.get(key)
        if type(v) is not int or not MIN_SIZE <= v <= MAX_SIZE or v % SIZE_MULTIPLE:
            raise ValueError(f"{key} must be an integer multiple of {SIZE_MULTIPLE} from {MIN_SIZE} to {MAX_SIZE}")
    model = req.get("model", DEFAULT_MODEL)
    if model not in MODELS:
        raise ValueError(f"model must be one of {', '.join(MODELS)}")
    steps, seed = req.get("steps"), req.get("seed")
    if steps is not None and (type(steps) is not int or not MIN_STEPS <= steps <= MAX_STEPS):
        raise ValueError(f"steps must be an integer from {MIN_STEPS} to {MAX_STEPS} or null")
    if seed is not None and type(seed) is not int:
        raise ValueError("seed must be an integer or null")
    return {
        "prompt": req["prompt"],
        "width": req["width"],
        "height": req["height"],
        "model": model,
        "steps": steps if steps is not None else DEFAULT_STEPS,
        "seed": seed if seed is not None else random.randrange(2**32),
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _json(self, status, obj):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _line(self, obj):
        self.wfile.write(json.dumps(obj).encode() + b"\n")
        self.wfile.flush()

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"ok": True, "models": list(MODELS)})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/generate":
            return self._json(404, {"error": "not found"})
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            length = -1
        if length < 0:
            return self._json(400, {"error": "Content-Length must be a non-negative integer"})
        if length > MAX_BODY:
            return self._json(413, {"error": f"body must be at most {MAX_BODY} bytes"})
        try:
            req = parse(self.rfile.read(length))
        except ValueError as e:
            return self._json(400, {"error": str(e)})
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            try:
                png, seed = generate(req, lambda n, total: self._write({"step": n, "steps": total}))
            except _ClientGone:
                raise
            except Exception as e:
                traceback.print_exc(file=sys.stderr)
                message = " ".join(f"{type(e).__name__}: {e}".split())
                self._write({"error": message[:MAX_ERROR]})
                return
            self._write({"png": base64.b64encode(png).decode(), "seed": seed})
        except _ClientGone:
            pass  # a failed write means the client disconnected: abandon the render

    def _write(self, obj):
        """Write one NDJSON line; only a failed socket write counts as the client leaving."""
        try:
            self._line(obj)
        except OSError:
            raise _ClientGone from None


def make_server(host, port):
    return ThreadingHTTPServer((host, port), Handler)


if __name__ == "__main__":
    port = int(os.environ.get("DOCK_IMAGE_PORT", "7860"))
    print(f"dock image server on http://127.0.0.1:{port}", flush=True)
    make_server("127.0.0.1", port).serve_forever()
