"""The Whisper server on the host: one POST per window and the reading of its answer."""

import json
import math
import re

from dock_plugin import net
from dock_plugin.cancel import Failure

from .windows import RATE_BYTES

TIMEOUT = 1200  # seconds to wait for the answer to one window (Whisper can take ~530 s for a hard 600 s window)
CONNECT_TIMEOUT = 10  # seconds to connect (a cancel closes the connecting socket, so it never waits for this)
MAX_REPLY = 8_000_000  # bytes accepted in one answer
ERROR_CHARS = 300  # characters of the server's own error that are quoted
START_SERVER = "uv run --project plugins/transcribe/server python plugins/transcribe/server/serve.py"
URL_VAR = "DOCK_TRANSCRIBE_URL"


def parse_reply(status, data, where, length):
    """Returns (language, [(start, end, text)]) of an answer or raises Failure with the message to show."""
    try:
        reply = json.loads(data)
        if not isinstance(reply, dict):
            raise TypeError("not an object")
    except (ValueError, TypeError):
        if status != 200:
            raise Failure(f"the transcribe server at {where} answered HTTP {status}") from None
        raise Failure(f"the transcribe server at {where} sent a broken answer") from None
    if status != 200 or "error" in reply:
        error = reply.get("error")
        detail = f": {str(error)[:ERROR_CHARS]}" if error else ""
        raise Failure(f"the transcribe server at {where} answered HTTP {status}{detail}")
    segments = reply.get("segments")
    language = reply.get("language")
    cues = []
    if isinstance(segments, list) and isinstance(language, str) and re.fullmatch(r"[a-z]{2,8}", language):
        for s in segments:
            start, end, text = (s.get(k) for k in ("start", "end", "text")) if isinstance(s, dict) else (None,) * 3
            if not (isinstance(start, int | float) and isinstance(end, int | float) and isinstance(text, str)):
                break
            if isinstance(start, bool) or isinstance(end, bool) or not 0 <= start <= end or math.isinf(end):
                break  # also false for nan
            cues.append((min(float(start), length), min(float(end), length), " ".join(text.split())))
        else:
            return language, cues
    raise Failure(f"the transcribe server at {where} sent a broken answer")


def post(base, pcm, language, call):
    """One POST /transcribe; returns (language, cues). Raises Failure with a message to show, Cancelled after a
    cancel."""
    query = f"?language={language}" if language else ""
    status, data = net.request(base, f"/transcribe{query}", pcm, call, service="transcribe", var=URL_VAR,
                               start=START_SERVER, read_timeout=TIMEOUT, max_reply=MAX_REPLY,
                               connect_timeout=CONNECT_TIMEOUT)
    return parse_reply(status, data, net.shown(base), len(pcm) / RATE_BYTES)
