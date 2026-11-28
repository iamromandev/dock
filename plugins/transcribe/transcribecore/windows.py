"""Cutting the decoded PCM into overlapping windows and telling silent ones from speech."""

import math

from dock_plugin.cancel import Cancelled, Failure

from .render import clock

RATE_BYTES = 32000  # bytes of 16 kHz int16 mono PCM per second
WINDOW_SECONDS = 600
OVERLAP_SECONDS = 10
WINDOW = WINDOW_SECONDS * RATE_BYTES  # new bytes per window
OVERLAP = OVERLAP_SECONDS * RATE_BYTES  # bytes each later window repeats from the one before
CHUNK = 1 << 20  # bytes read from ffmpeg at a time
SILENCE_RMS = 98  # int16 scale: 0.003 of full scale, the same threshold the server applies to a segment


def windows(stream, limit_bytes, call, name):
    """Yields (index, start second, first new second, PCM, last) for 600 s windows that each repeat the last 10 s
    of the one before. The PCM is one bytearray reused between windows: only one window is ever in memory."""
    buf = bytearray()
    offset = 0  # bytes before buf[0]
    total = 0
    index = 0
    while True:
        fresh = len(buf)
        new = 0
        while new < WINDOW:
            chunk = stream.read(min(CHUNK, WINDOW - new))
            if call.cancelled():
                raise Cancelled()
            if not chunk:
                break
            buf += chunk
            new += len(chunk)
            total += len(chunk)
            if total > limit_bytes:
                raise Failure(f"input is longer than {clock(limit_bytes // RATE_BYTES)}")
        last = new < WINDOW
        if len(buf) % 2:
            del buf[-1:]
            new -= 1
        if new <= 0:
            if index == 0:
                raise Failure(f"not a media file with audio: {name}")
            return
        yield index, offset / RATE_BYTES, (offset + fresh) / RATE_BYTES, buf, last
        if last:
            return
        offset += len(buf) - OVERLAP
        del buf[:-OVERLAP]
        index += 1


def rms(pcm):
    samples = memoryview(pcm).cast("h")
    return math.sqrt(math.sumprod(samples, samples) / len(samples)) if len(samples) else 0.0


def loud(pcm):
    """True if any whole second of the PCM reaches the silence threshold."""
    with memoryview(pcm) as view:
        return any(rms(view[i:i + RATE_BYTES]) >= SILENCE_RMS for i in range(0, len(view) - 1, RATE_BYTES))
