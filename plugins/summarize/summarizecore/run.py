"""The summarize command: checks the inputs, reads and cleans the file, asks the model and publishes the summary."""

import os
import stat

from dock_plugin import cancel, env, files, ui
from dock_plugin.cancel import Cancelled, Failure

from . import client
from .prompts import INSTRUCTIONS
from .rounds import summarize_text
from .text import clean, decode

DEFAULT_URL = "http://host.docker.internal:11434"
DEFAULT_MODEL = "qwen2.5:7b"
DEFAULT_MAX_BYTES = 5_000_000
MAX_VAR = "DOCK_SUMMARIZE_MAX_BYTES"
SUFFIXES = (".txt", ".md", ".srt", ".vtt")


def resolve_source(name, in_dir, work):
    """The absolute path of the file: inside the in folder (or the flow's work folder). read_file checks it is a
    regular file, never through a final symlink that leaves them or a FIFO. Raises Failure. Unlike
    files.resolve_source, a file that cannot be read is named by its base name, as before the move to the kit."""
    base = in_dir or work
    if not base:
        raise Failure("summarize needs the in folder (the host sent no in_dir)")
    if "\0" in name or "\0" in base or (work and "\0" in work):
        raise Failure("file must be a file name without NUL characters")
    real = os.path.realpath(os.path.join(base, name))
    roots = [os.path.realpath(r) for r in (in_dir, work) if r]
    if not any(files.inside(real, r) for r in roots):
        raise Failure("file must be inside the in folder")
    return real


def read_file(path, name, limit):
    """The bytes of a regular file of at most `limit` bytes. Raises Failure."""
    try:
        # O_NONBLOCK: opening a FIFO must not wait for a writer; O_NOFOLLOW: no symlink swapped in after the check
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except OSError as e:
        raise Failure(f"cannot read {name}: {e.strerror or e}") from None
    with os.fdopen(fd, "rb") as fh:
        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            raise Failure(f"{name} is not a file in the in folder")
        raw = fh.read(limit + 1)
    if len(raw) > limit:
        raise Failure(f"file is larger than {limit} bytes")
    return raw


def target_path(output, source_name, cwd):
    """The path of the summary file, which must not exist yet. Raises Failure."""
    if output is None or output == "":
        stem = os.path.splitext(source_name)[0]
        if not stem or stem in (".", "..") or len(stem.encode()) > 200:
            stem = "summary"
        filename = f"{stem}.summary.md"
    else:
        output = files.plain_name(output, "output")
        filename = output if output.endswith(".md") else f"{output}.md"
    path = os.path.join(cwd, filename)
    files.Outputs.check([path])
    return path


def summarize_file(text, name, base, model, style, target, call, outputs):
    """Runs in a thread: asks the model and publishes the file. Returns the outputs dict.
    Raises Failure (told to the user) or Cancelled."""

    def ask(instruction, part):
        answer, reason = client.post(base, model, instruction, part, call)
        if reason == "length":
            ui.say("warn", "Ollama stopped an answer at its length limit; the summary may be cut short")
        return answer

    shown_percent = 0

    def tick(done, total):
        nonlocal shown_percent
        shown_percent = max(shown_percent, min(99, done * 100 // total))  # a later round adds calls; never go back
        ui.bar(shown_percent)

    ui.bar(0)
    answer, chunks = summarize_text(text, style, ask, tick)
    title = "".join(c if c.isprintable() else " " for c in name)
    body = f"# Summary of {title}\n\n{answer}\n".encode("utf-8", "replace").decode()  # a lone surrogate becomes ?
    outputs.publish([(target, body)], call)
    ui.bar(100, done=True)
    ui.say("info", f"summarized {name} ({chunks} chunk{'' if chunks == 1 else 's'}, {model}) into {os.path.basename(target)}")
    return {"file": target, "chunks": chunks, "model": model}


def guarded(*args):
    """summarize_file, with an unexpected error told by its type only (it might quote the document)."""
    try:
        return summarize_file(*args)
    except (Cancelled, Failure):
        raise
    except Exception as e:
        raise Failure(f"summarize failed: {type(e).__name__}") from None


def run(inputs, ctx):
    if not isinstance(inputs.get("file"), str) or not inputs["file"]:
        raise Failure("summarize needs the input file", -32602)
    if any(not isinstance(inputs.get(k), str | None) for k in ("style", "output")):
        raise Failure("style and output must be strings", -32602)
    base = (os.environ.get("DOCK_SUMMARIZE_URL") or DEFAULT_URL).rstrip("/")
    model = os.environ.get("DOCK_SUMMARIZE_MODEL") or DEFAULT_MODEL
    name = os.path.basename(inputs["file"].rstrip("/")) or "document"
    client.check_url(base)
    style = inputs.get("style") or "bullets"
    if style not in INSTRUCTIONS:
        raise Failure(f"style must be one of {', '.join(INSTRUCTIONS)}")
    target = target_path(inputs.get("output"), name, ctx.cwd)
    limit = env.positive_int(MAX_VAR, DEFAULT_MAX_BYTES)
    suffix = os.path.splitext(name)[1]
    if suffix.lower() not in SUFFIXES:
        raise Failure(f"unsupported file type: use one of {', '.join(SUFFIXES)}")
    path = resolve_source(inputs["file"], ctx.in_dir, ctx.work)
    text = clean(decode(read_file(path, name, limit)), suffix)
    with files.Outputs() as outputs:  # a failure or a cancel leaves no file of this run behind
        return cancel.run_worker(guarded, (text, name, base, model, style, target, ctx.call, outputs), ctx.call,
                                 "summarize")
