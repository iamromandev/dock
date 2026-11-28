"""ffprobe and ffmpeg: the duration of the audio and the decoder that streams it as PCM."""

import json
import math
import subprocess

from dock_plugin.cancel import Cancelled, Failure

PROTOCOLS = "file,pipe"  # the only protocols ffmpeg and ffprobe may open: a crafted playlist cannot reach the network
PROBE_TIMEOUT = 60  # seconds


def probe(path, name, call):
    """Duration in seconds (None if unknown) of the first audio stream's file; raises Failure without audio."""
    cmd = ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_type:format=duration",
           "-of", "json", "-protocol_whitelist", PROTOCOLS, "-i", path]
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError as e:
        raise Failure(f"cannot run ffprobe: {e.strerror or e}") from None
    call.track(proc)
    try:
        out, _ = proc.communicate(timeout=PROBE_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise Failure("ffprobe took too long") from None
    if call.cancelled():
        raise Cancelled()
    try:
        info = json.loads(out)
        streams = info.get("streams")
        if proc.returncode != 0 or not isinstance(streams, list) or not streams:
            raise ValueError("no audio")
    except (ValueError, AttributeError):
        raise Failure(f"not a media file with audio: {name}") from None
    try:
        duration = float(str((info.get("format") or {}).get("duration")))
    except ValueError:
        return None
    return duration if math.isfinite(duration) and duration >= 0 else None


def decode(path, call):
    """Starts ffmpeg decoding the first audio stream to 16 kHz int16 mono PCM on its stdout; the call tracks it."""
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-protocol_whitelist", PROTOCOLS, "-i", path, "-map", "0:a:0",
           "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "pipe:1"]
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError as e:
        raise Failure(f"cannot run ffmpeg: {e.strerror or e}") from None
    call.track(proc)
    return proc
