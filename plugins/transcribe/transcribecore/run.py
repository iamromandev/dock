"""The transcribe command: checks the inputs, then decodes, sends windows to the server and publishes the files."""

import os
import re

from dock_plugin import cancel, env, files, net, rpc, ui
from dock_plugin.cancel import Cancelled, Failure

from .client import URL_VAR, post
from .media import decode, probe
from .render import clock, render
from .seams import splice
from .windows import RATE_BYTES, loud, windows

DEFAULT_URL = "http://host.docker.internal:7862"
DEFAULT_MAX_SECONDS = 14400  # 4 hours
MAX_VAR = "DOCK_TRANSCRIBE_MAX_SECONDS"
FORMATS = ("txt", "srt", "vtt")


def check(inputs, source_name, cwd):
    """Returns (formats, language, [target paths in format order]) or raises Failure."""
    wanted = {f.strip() for f in (inputs.get("format") or "txt").split(",")}
    if not wanted <= set(FORMATS):
        raise Failure(f"format must be a comma list of {', '.join(FORMATS)}")
    formats = [f for f in FORMATS if f in wanted]
    language = inputs.get("language") or None
    if language is not None and not re.fullmatch(r"[a-z]{2,3}", language):
        raise Failure("language must be a language code such as en, de or yue")
    stem = inputs.get("output")
    if stem is None or stem == "":
        stem = os.path.splitext(source_name)[0]
        if not stem or stem in (".", "..") or len(stem.encode()) > 200:
            stem = "transcript"
    else:
        stem = files.plain_name(stem, "output")
    targets = [os.path.join(cwd, f"{stem}.{f}") for f in formats]
    files.Outputs.check(targets)
    return formats, language, targets


def transcribe_media(path, name, base, language, formats, targets, call, outputs):
    """Runs in a thread: probes, decodes and sends windows, publishes the files. Returns the outputs dict.
    Raises Failure (told to the user) or Cancelled."""
    limit = env.positive_int(MAX_VAR, DEFAULT_MAX_SECONDS)
    duration = probe(path, name, call)
    if duration is not None and duration > limit:
        raise Failure(f"input is longer than {clock(limit)}")
    ui.bar(0 if duration else None)
    proc = decode(path, call)
    cues: list[tuple[float, float, str]] = []
    prev_end = 0.0  # where the previous window's audio ended
    decoded = False
    seconds = 0.0
    try:
        for index, start, _fresh, pcm, last in windows(proc.stdout, limit * RATE_BYTES, call, name):
            decoded = True
            seconds = start + len(pcm) / RATE_BYTES
            if loud(pcm):
                found, got = post(base, pcm, language, call)
                language = language or found  # later windows follow the language heard first
                new = [(start + s, start + e, text) for s, e, text in sorted(got, key=lambda c: c[0]) if text]
                if index == 0:
                    cues += new
                else:
                    tail = [c for c in cues if c[1] > start]
                    cues = [c for c in cues if c[1] <= start]
                    cues += splice(tail, new, start, prev_end)
            prev_end = seconds
            if duration and not last:
                ui.bar(min(99, int(seconds / duration * 100)))
        proc.wait()
        if call.cancelled():
            raise Cancelled()
        if proc.returncode != 0:
            raise Failure(f"could not decode all of {name}" if decoded else f"not a media file with audio: {name}")
    finally:
        proc.kill()
        proc.wait()
        if proc.stdout:
            proc.stdout.close()
    cues.sort(key=lambda c: c[0])
    if not cues:
        ui.say("warn", "no speech found")
    outputs.publish([(t, render(f, cues)) for f, t in zip(formats, targets, strict=True)], call)
    ui.bar(100, done=True)
    names = ", ".join(os.path.basename(t) for t in targets)
    ui.say("info", f"transcribed {clock(round(seconds))} ({language or 'unknown'}) into {names}")
    return {"files": targets, "language": language}


def run(inputs: dict, ctx: rpc.Context) -> dict:
    source = rpc.require_str(inputs, "file")
    if any(not isinstance(inputs.get(k), str | None) for k in ("format", "language", "output")):
        raise Failure("format, language and output must be strings", -32602)
    base = (os.environ.get(URL_VAR) or DEFAULT_URL).rstrip("/")
    name = os.path.basename(source.rstrip("/")) or "media"
    net.check_url(base, URL_VAR)
    formats, language, targets = check(inputs, name, ctx.cwd)
    env.positive_int(MAX_VAR, DEFAULT_MAX_SECONDS)
    path = files.resolve_source(source, ctx.in_dir, ctx.work, "transcribe")
    with files.Outputs() as outputs:  # a failure or a cancel leaves no file of this run behind
        args = (path, name, base, language, formats, targets, ctx.call, outputs)
        return cancel.run_worker(transcribe_media, args, ctx.call, "transcribe")
