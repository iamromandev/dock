"""Host-side transcribe server: wraps mlx-whisper (whisper-large-v3-turbo on Apple MLX) behind a small HTTP API.

MLX needs Metal, so it cannot run in a container. The transcribe plugin posts raw PCM windows here.
POST /transcribe[?language=xx] with int16 LE mono 16 kHz PCM (application/octet-stream) answers JSON
{"language", "segments": [{"start", "end", "text"}]} or {"error"}.
GET /health reports the model. Binds 127.0.0.1 only; port from DOCK_TRANSCRIBE_PORT (default 7862).
"""

import json
import os
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlsplit

MODEL = "whisper-large-v3-turbo"
REPO = "mlx-community/whisper-large-v3-turbo"
RATE = 16000  # Hz
MAX_BODY = 23_040_000  # bytes: 12 minutes of int16 mono at RATE
MAX_ERROR = 500  # characters of an error message
READ_TIMEOUT = 30  # seconds a client may stall on the socket
HOSTS = ("127.0.0.1", "localhost", "host.docker.internal")  # accepted Host header names
SILENCE_RMS = 0.003  # float scale: a segment whose own audio is quieter than this is a hallucination
NO_SPEECH = 0.6  # segments with a higher no_speech_prob are dropped
# Whisper's language codes (keys of mlx_whisper.tokenizer.LANGUAGES in 0.4.3), frozen so tests need no mlx.
# fmt: off
LANGUAGES = (
    "en", "zh", "de", "es", "ru", "ko", "fr", "ja", "pt", "tr", "pl", "ca", "nl", "ar", "sv", "it", "id",
    "hi", "fi", "vi", "he", "uk", "el", "ms", "cs", "ro", "da", "hu", "ta", "no", "th", "ur", "hr", "bg",
    "lt", "la", "mi", "ml", "cy", "sk", "te", "fa", "lv", "bn", "sr", "az", "sl", "kn", "et", "mk", "br",
    "eu", "is", "hy", "ne", "mn", "bs", "kk", "sq", "sw", "gl", "mr", "pa", "si", "km", "sn", "yo", "so",
    "af", "oc", "ka", "be", "tg", "sd", "gu", "am", "yi", "lo", "uz", "fo", "ht", "ps", "tk", "nn", "mt",
    "sa", "lb", "my", "bo", "tl", "mg", "as", "tt", "haw", "ln", "ha", "ba", "jw", "su", "yue",
)
# fmt: on
_gpu = threading.Lock()  # one transcription at a time: the GPU and the model are shared
_slots = threading.BoundedSemaphore(2)  # at most one running plus one waiting transcription


def _clear_cache():
    """Free MLX's buffer cache after a transcription so it does not pile up next to the model."""
    import mlx.core as mx  # imported here so tests run without mlx

    mx.clear_cache()


def _whisper(audio, language):
    """Run whisper on float32 mono audio at RATE Hz. The seam tests replace."""
    import mlx_whisper  # imported here so tests run without mlx

    return mlx_whisper.transcribe(
        audio, path_or_hf_repo=REPO, language=language, condition_on_previous_text=False, verbose=None
    )


def parse_query(path):
    """Validate the request path. Returns {"language": str | None}; raises ValueError."""
    parts = urlsplit(path)
    if parts.path != "/transcribe":
        raise ValueError("path must be /transcribe")
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    if any(k != "language" for k, _ in pairs):
        raise ValueError("the only query parameter is language")
    if len(pairs) > 1:
        raise ValueError("language may be given once")
    language = pairs[0][1] if pairs else None
    if pairs and language not in LANGUAGES:
        raise ValueError("language must be a Whisper language code")
    return {"language": language}


def transcribe(pcm, language):
    """Transcribe int16 LE mono PCM at RATE Hz. Drops segments that are silent, empty or out of range."""
    import numpy as np  # imported here so importing the module stays cheap

    audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
    duration = audio.size / RATE
    with _gpu:
        try:
            result = _whisper(audio, language)
        finally:
            _clear_cache()
    segments = []
    for s in result["segments"]:
        start = min(max(float(s["start"]), 0.0), duration)
        end = min(max(float(s["end"]), 0.0), duration)
        text = str(s["text"]).strip()
        if end <= start or not text or s.get("no_speech_prob", 0.0) > NO_SPEECH:
            continue
        piece = audio[int(start * RATE) : int(end * RATE)]
        if piece.size == 0 or float(np.sqrt(np.mean(np.square(piece)))) < SILENCE_RMS:
            continue
        segments.append({"start": round(start, 3), "end": round(end, 3), "text": text})
    return {"language": language or result["language"], "segments": segments}


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
            self._json(200, {"ok": True, "model": MODEL})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self._host_ok():
            return
        if urlsplit(self.path).path != "/transcribe":
            return self._json(404, {"error": "not found"})
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/octet-stream":
            return self._json(415, {"error": "Content-Type must be application/octet-stream"})
        value = self.headers.get("Content-Length", "")
        length = int(value) if value.isascii() and value.isdigit() else -1
        if length < 0:
            return self._json(400, {"error": "Content-Length must be a non-negative integer"})
        if length > MAX_BODY:
            return self._json(413, {"error": f"body must be at most {MAX_BODY} bytes"})
        try:
            query = parse_query(self.path)
        except ValueError as e:
            return self._json(400, {"error": str(e)})
        if not _slots.acquire(blocking=False):  # before the read: a busy server never buffers a body
            return self._json(503, {"error": "busy: a request is already waiting"})
        try:
            try:
                body = self.rfile.read(length)
            except OSError:  # includes the read timeout
                self.close_connection = True
                return
            if not body or len(body) % 2:
                return self._json(
                    400, {"error": "body must be non-empty int16 PCM (an even number of bytes)"}
                )
            try:
                out = transcribe(body, query["language"])
            except Exception as e:
                traceback.print_exc(file=sys.stderr)
                message = " ".join(f"{type(e).__name__}: {e}".split())
                return self._json(500, {"error": message[:MAX_ERROR]})
        finally:
            _slots.release()
        self._json(200, out)


def make_server(host, port):
    return ThreadingHTTPServer((host, port), Handler)


def main():
    port = int(os.environ.get("DOCK_TRANSCRIBE_PORT", "7862"))
    print(f"dock transcribe server on http://127.0.0.1:{port}", flush=True)
    make_server("127.0.0.1", port).serve_forever()


if __name__ == "__main__":
    main()
