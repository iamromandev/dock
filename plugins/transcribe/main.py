"""transcribe plugin for dock: turns the speech in an audio or video file into text and subtitle files. ffmpeg
decodes the media in the container; windows of PCM go to a local Whisper server on the host (stdlib only).
JSON-RPC 2.0, one JSON object line on stdin/stdout. See protocol/PROTOCOL.md.
"""

import http.client
import json
import math
import os
import queue
import re
import socket
import stat
import subprocess
import sys
import threading
import urllib.parse

DEFAULT_URL = "http://host.docker.internal:7862"
DEFAULT_MAX_SECONDS = 14400  # 4 hours
TIMEOUT = 300  # seconds to wait for the answer to one window
CONNECT_TIMEOUT = 10  # seconds to connect (a cancel closes the connecting socket, so it never waits for this)
MAX_REPLY = 8_000_000  # bytes accepted in one answer
RATE_BYTES = 32000  # bytes of 16 kHz int16 mono PCM per second
WINDOW_SECONDS = 600
OVERLAP_SECONDS = 10
WINDOW = WINDOW_SECONDS * RATE_BYTES  # new bytes per window
OVERLAP = OVERLAP_SECONDS * RATE_BYTES  # bytes each later window repeats from the one before
CHUNK = 1 << 20  # bytes read from ffmpeg at a time
SILENCE_RMS = 98  # int16 scale: 0.003 of full scale, the same threshold the server applies to a segment
SEAM_END = 0.5  # seconds: a last segment ending this close to the end of a window may have been cut by it
SEAM_SLACK = 0.25  # seconds: how much earlier than a cut segment's start the next window may still keep one
ERROR_CHARS = 300  # characters of the server's own error that are quoted
PROTOCOLS = "file,pipe"  # the only protocols ffmpeg and ffprobe may open: a crafted playlist cannot reach the network
PROBE_TIMEOUT = 60  # seconds
FORMATS = ("txt", "srt", "vtt")
START_SERVER = "uv run --project plugins/transcribe/server python plugins/transcribe/server/serve.py"


class Cancelled(Exception):
    pass


class Failure(Exception):
    def __init__(self, message, code=-32000):
        super().__init__(message)
        self.code = code


class BadInput(Exception):
    """A problem the user can fix or must know about; the message is told to the user."""


inbox: queue.Queue = queue.Queue()
cancelled = threading.Event()
state_lock = threading.Lock()
pending = 0  # invokes read from stdin and not yet finished; guarded by state_lock. A cancel with none is ignored.


class Call:
    """The server socket and child processes of one invoke: lets the main thread stop them on cancel. The lock is
    only held for a flag or a field, never across I/O, so stop() is never kept waiting by a slow send."""

    def __init__(self):
        self.sock: socket.socket | None = None
        self.procs: list[subprocess.Popen] = []
        self.stopped = False
        self.lock = threading.Lock()

    def register(self, sock):
        """Makes `sock` the one stop() hangs up. Raises Cancelled (closing it) if the call is already stopped."""
        with self.lock:
            if not self.stopped:
                self.sock = sock
                return
        self.hang_up(sock)
        raise Cancelled()

    def track(self, proc):
        with self.lock:
            self.procs.append(proc)
            if self.stopped:
                proc.kill()

    def cancelled(self):
        return self.stopped or cancelled.is_set()

    @staticmethod
    def hang_up(sock):
        # close() alone leaves the socket open while the worker is blocked on it; shut it down first
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()

    def stop(self):
        with self.lock:
            self.stopped = True
            sock = self.sock
            for proc in self.procs:
                try:
                    proc.kill()
                except OSError:
                    pass
        if sock is not None:
            self.hang_up(sock)


def send(msg):
    try:
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", **msg}) + "\n")
        sys.stdout.flush()
    except OSError:  # the host is gone (broken pipe); nobody to tell
        pass


def shown(base):
    """The server address without any user:password part, safe to log."""
    parts = urllib.parse.urlsplit(base)
    return f"{parts.scheme}://{parts.netloc.rpartition('@')[2]}" if parts.netloc else base.rpartition("@")[2]


def notify(method, **params):
    send({"method": method, "params": params})


def say(level, text):
    notify("log", level=level, message=text)


def bar(percent, **extra):
    notify("bar", id="transcribe", label="transcribing", percent=percent, **extra)


def stop_if_cancelled():
    if cancelled.is_set():
        raise Cancelled()


def clock(seconds):
    seconds = int(seconds)
    h, rest = divmod(seconds, 3600)
    return f"{h}:{rest // 60:02d}:{rest % 60:02d}" if h else f"{rest // 60}:{rest % 60:02d}"


# ---- subtitles

def stamp(seconds, sep):
    ms = round(seconds * 1000)
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    return f"{h:02d}:{m:02d}:{ms // 1000:02d}{sep}{ms % 1000:03d}"


def render(fmt, cues):
    """The text of one output file from cues [(start, end, text)] in seconds."""
    if fmt == "txt":
        return "".join(f"{text}\n" for _, _, text in cues)
    sep = "," if fmt == "srt" else "."
    blocks = []
    for i, (start, end, text) in enumerate(cues, 1):
        head = f"{i}\n" if fmt == "srt" else ""
        blocks.append(f"{head}{stamp(start, sep)} --> {stamp(end, sep)}\n{text.replace('-->', '->')}\n")
    if fmt == "srt":
        return "\n".join(blocks)
    return "WEBVTT\n" + "".join(f"\n{b}" for b in blocks)


# ---- inputs

def inside(real, root):
    try:
        return os.path.commonpath([real, root]) == root
    except ValueError:
        return False


def resolve_source(name, in_dir, work):
    """The absolute path of the media file: inside the in folder (or the flow's work folder), a regular file,
    never through a final symlink that leaves them or a FIFO. Raises BadInput."""
    base = in_dir or work
    if not base:
        raise BadInput("transcribe needs the in folder (the host sent no in_dir)")
    if "\0" in name or "\0" in base or (work and "\0" in work):
        raise BadInput("file must be a file name without NUL characters")
    real = os.path.realpath(os.path.join(base, name))
    roots = [os.path.realpath(r) for r in (in_dir, work) if r]
    if not any(inside(real, r) for r in roots):
        raise BadInput("file must be inside the in folder")
    try:
        # O_NONBLOCK: opening a FIFO must not wait for a writer; O_NOFOLLOW: no symlink swapped in after the check
        fd = os.open(real, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except OSError as e:
        raise BadInput(f"cannot read {name}: {e.strerror or e}") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise BadInput(f"{name} is not a file in the in folder")
    finally:
        os.close(fd)
    return real


def check_url(base):
    """Raises BadInput unless base is an http URL with a host and a port in 1-65535 (if given)."""
    try:
        parts = urllib.parse.urlsplit(base)
        port = parts.port
        ok = parts.scheme == "http" and bool(parts.hostname) and (port is None or 1 <= port <= 65535)
    except ValueError:
        ok = False
    if not ok:
        raise BadInput("DOCK_TRANSCRIBE_URL is not a valid http URL")


def max_seconds():
    raw = os.environ.get("DOCK_TRANSCRIBE_MAX_SECONDS")
    if raw is None or raw == "":
        return DEFAULT_MAX_SECONDS
    if not re.fullmatch(r"[0-9]+", raw) or int(raw) < 1:
        raise BadInput(f"DOCK_TRANSCRIBE_MAX_SECONDS must be a positive integer, not {raw[:30]!r}")
    return int(raw)


def check(inputs, source_name, cwd):
    """Returns (formats, language, [target paths in format order]) or raises BadInput."""
    wanted = {f.strip() for f in (inputs.get("format") or "txt").split(",")}
    if not wanted <= set(FORMATS):
        raise BadInput(f"format must be a comma list of {', '.join(FORMATS)}")
    formats = [f for f in FORMATS if f in wanted]
    language = inputs.get("language") or None
    if language is not None and not re.fullmatch(r"[a-z]{2,3}", language):
        raise BadInput("language must be a language code such as en, de or yue")
    stem = inputs.get("output")
    if stem is None or stem == "":
        stem = os.path.splitext(source_name)[0]
        if not stem or stem in (".", "..") or len(stem.encode()) > 200:
            stem = "transcript"
    elif "/" in stem or "\\" in stem or stem in (".", "..") or any(ord(c) < 32 or ord(c) == 127 for c in stem):
        raise BadInput("output must be a plain name without a folder (no slash)")
    elif len(stem.encode()) > 200:
        raise BadInput("output name is too long")
    targets = [os.path.join(cwd, f"{stem}.{f}") for f in formats]
    for path in targets:
        if os.path.lexists(path):
            raise BadInput(f"{os.path.basename(path)} already exists")
    return formats, language, targets


# ---- media

def probe(path, name, call):
    """Duration in seconds (None if unknown) of the first audio stream's file; raises BadInput without audio."""
    cmd = ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_type:format=duration",
           "-of", "json", "-protocol_whitelist", PROTOCOLS, "-i", path]
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError as e:
        raise BadInput(f"cannot run ffprobe: {e.strerror or e}") from None
    call.track(proc)
    try:
        out, _ = proc.communicate(timeout=PROBE_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise BadInput("ffprobe took too long") from None
    if call.cancelled():
        raise Cancelled()
    try:
        info = json.loads(out)
        streams = info.get("streams")
        if proc.returncode != 0 or not isinstance(streams, list) or not streams:
            raise ValueError("no audio")
    except (ValueError, AttributeError):
        raise BadInput(f"not a media file with audio: {name}") from None
    try:
        duration = float(str((info.get("format") or {}).get("duration")))
    except ValueError:
        return None
    return duration if math.isfinite(duration) and duration >= 0 else None


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
                raise BadInput(f"input is longer than {clock(limit_bytes // RATE_BYTES)}")
        last = new < WINDOW
        if len(buf) % 2:
            del buf[-1:]
            new -= 1
        if new <= 0:
            if index == 0:
                raise BadInput(f"not a media file with audio: {name}")
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


# ---- the server

def parse_reply(status, data, where, length):
    """Returns (language, [(start, end, text)]) of an answer or raises BadInput with the message to show."""
    try:
        reply = json.loads(data)
        if not isinstance(reply, dict):
            raise TypeError("not an object")
    except (ValueError, TypeError):
        if status != 200:
            raise BadInput(f"the transcribe server at {where} answered HTTP {status}") from None
        raise BadInput(f"the transcribe server at {where} sent a broken answer") from None
    if status != 200 or "error" in reply:
        error = reply.get("error")
        detail = f": {str(error)[:ERROR_CHARS]}" if error else ""
        raise BadInput(f"the transcribe server at {where} answered HTTP {status}{detail}")
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
    raise BadInput(f"the transcribe server at {where} sent a broken answer")


def open_socket(host, port, call):
    """A socket connected to host:port. Each socket is registered with the call before it connects, so a cancel can
    close it and end the connect."""
    last = None
    for family, kind, proto, _, addr in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
        sock = socket.socket(family, kind, proto)
        try:
            call.register(sock)
            sock.settimeout(CONNECT_TIMEOUT)
            sock.connect(addr)
            sock.settimeout(TIMEOUT)
            return sock
        except OSError as e:
            sock.close()
            last = e
    raise last or OSError("no address")


def post(base, pcm, language, call):
    """One POST /transcribe; returns (language, cues). Raises BadInput with a message to show, Cancelled after a
    cancel. The call's lock is never held while sending or waiting: a cancel closes the registered socket, which
    ends the send or the read at once. A request is never sent once the call is stopped."""
    where = shown(base)
    parts = urllib.parse.urlsplit(base)
    conn = http.client.HTTPConnection(parts.hostname or "", parts.port)
    conn.auto_open = 0
    query = f"?language={language}" if language else ""
    reaching = True  # still making the connection
    try:
        conn.sock = open_socket(parts.hostname, parts.port or 80, call)
        with call.lock:
            if call.cancelled():
                raise Cancelled()
        reaching = False
        conn.request("POST", f"{parts.path.rstrip('/')}/transcribe{query}", pcm,
                     {"Content-Type": "application/octet-stream"})
        resp = conn.getresponse()
        data = resp.read(MAX_REPLY + 1)
        status = resp.status
    except (OSError, http.client.HTTPException) as e:
        if call.cancelled():
            raise Cancelled() from None
        if reaching:
            raise BadInput(f"cannot reach the transcribe server at {where}: {getattr(e, 'reason', e) or type(e).__name__}. "
                           f"Start it with `{START_SERVER}`; "
                           "set DOCK_TRANSCRIBE_URL if it runs elsewhere") from None
        if isinstance(e, TimeoutError):
            raise BadInput(f"timed out after {TIMEOUT:g} s waiting for the transcribe server") from None
        raise BadInput(f"the transcribe server stopped answering ({where})") from None
    finally:
        conn.close()
    if len(data) > MAX_REPLY:
        raise BadInput(f"the transcribe server at {where} sent an answer that is too large (over {MAX_REPLY} bytes)")
    return parse_reply(status, data, where, len(pcm) / RATE_BYTES)


# ---- output files

class Outputs:
    """Writes each file as .name.part, links it into place (never replacing a file) and removes the .part.
    `discard` removes the parts and the files this run linked, and nothing else."""

    def __init__(self):
        self.published: list[str] = []
        self.parts: list[str] = []

    def publish(self, files, call):
        with call.lock:  # a cancel (stop()) waits here, so it never lands halfway
            if call.cancelled():
                raise Cancelled()
            try:
                for path, text in files:
                    self.one(path, text)
            except BaseException:
                self.discard()
                raise

    def one(self, path, text):
        name = os.path.basename(path)
        part = os.path.join(os.path.dirname(path), f".{name}.part")
        try:
            fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            raise BadInput(f".{name}.part already exists") from None
        except OSError as e:
            raise BadInput(f"cannot write {name}: {e.strerror or e}") from None
        self.parts.append(part)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(text.encode("utf-8"))
            os.link(part, path)
        except FileExistsError:
            raise BadInput(f"{name} already exists") from None
        except OSError as e:
            raise BadInput(f"cannot write {name}: {e.strerror or e}") from None
        self.published.append(path)
        os.unlink(part)
        self.parts.remove(part)

    def discard(self):
        for path in self.parts + self.published:
            try:
                os.unlink(path)
            except OSError:
                pass
        self.parts.clear()
        self.published.clear()


# ---- the work

def transcribe_media(path, name, base, language, formats, targets, call, outputs):
    """Runs in a thread: probes, decodes and sends windows, publishes the files. Returns the result dict.
    Raises BadInput (told to the user) or Cancelled."""
    limit = max_seconds()
    duration = probe(path, name, call)
    if duration is not None and duration > limit:
        raise BadInput(f"input is longer than {clock(limit)}")
    bar(0 if duration else None)
    try:
        proc = subprocess.Popen(
            ["ffmpeg", "-nostdin", "-v", "error", "-protocol_whitelist", PROTOCOLS, "-i", path, "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000",
             "-f", "s16le", "pipe:1"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError as e:
        raise BadInput(f"cannot run ffmpeg: {e.strerror or e}") from None
    call.track(proc)
    cues: list[tuple[float, float, str]] = []
    held = None  # a last segment the end of its window may have cut: the next window gives it back whole
    decoded = False
    seconds = 0.0
    try:
        for index, start, fresh, pcm, last in windows(proc.stdout, limit * RATE_BYTES, call, name):
            decoded = True
            seconds = start + len(pcm) / RATE_BYTES
            # the first window keeps all; later ones repeat the previous window's last seconds, and start again
            # where a cut segment began
            cut = -1.0 if index == 0 else fresh if held is None else min(held[0], fresh) - SEAM_SLACK
            if loud(pcm):
                found, got = post(base, pcm, language, call)
                language = language or found  # later windows follow the language heard first
                kept = [(start + s, start + e, text) for s, e, text in sorted(got, key=lambda c: c[0])
                        if text and start + s >= cut]
                held = kept.pop() if not last and kept and kept[-1][1] >= seconds - SEAM_END else None
                cues += kept
            elif held is not None:
                cues.append(held)
                held = None
            if duration and not last:
                bar(min(99, int(seconds / duration * 100)))
        if held is not None:  # the audio ended where the window did
            cues.append(held)
        proc.wait()
        if call.cancelled():
            raise Cancelled()
        if proc.returncode != 0:
            raise BadInput(f"could not decode all of {name}" if decoded else f"not a media file with audio: {name}")
    finally:
        proc.kill()
        proc.wait()
        if proc.stdout:
            proc.stdout.close()
    cues.sort(key=lambda c: c[0])
    if not cues:
        say("warn", "no speech found")
    outputs.publish([(t, render(f, cues)) for f, t in zip(formats, targets, strict=True)], call)
    bar(100, done=True)
    names = ", ".join(os.path.basename(t) for t in targets)
    say("info", f"transcribed {clock(round(seconds))} ({language or 'unknown'}) into {names}")
    return {"exit_code": 0, "outputs": {"files": targets, "language": language}}


def work(args, out, call):
    """Puts ("done", result) or ("error", text) into `out`. After `call.stop()` it says nothing more."""
    def put(item):
        if not call.stopped:
            out.put(item)

    try:
        put(("done", transcribe_media(*args)))
    except Cancelled:
        return
    except BadInput as e:
        put(("error", str(e)))
    except Exception as e:  # never let the thread die silently
        put(("error", f"transcribe failed: {e!r}"))


def transcribe(inputs, in_dir, work_dir, cwd):
    if not isinstance(inputs.get("file"), str) or not inputs["file"]:
        raise Failure("transcribe needs the input file", -32602)
    if any(not isinstance(inputs.get(k), str | None) for k in ("format", "language", "output")):
        raise Failure("format, language and output must be strings", -32602)
    if any(v is not None and not isinstance(v, str) for v in (in_dir, work_dir)):
        raise Failure("in_dir and work must be strings", -32602)
    base = (os.environ.get("DOCK_TRANSCRIBE_URL") or DEFAULT_URL).rstrip("/")
    name = os.path.basename(inputs["file"].rstrip("/")) or "media"
    try:
        check_url(base)
        formats, language, targets = check(inputs, name, os.path.abspath(cwd))
        max_seconds()
        path = resolve_source(inputs["file"], in_dir, work_dir)
    except BadInput as e:
        say("error", str(e))
        return {"exit_code": 1}
    out: queue.Queue = queue.Queue()  # one per invoke, so a stale worker can never feed the next one
    call = Call()
    outputs = Outputs()
    thread = threading.Thread(target=work, args=((path, name, base, language, formats, targets, call, outputs), out, call),
                              daemon=True)
    kind = value = None
    try:
        thread.start()
        while True:
            stop_if_cancelled()
            try:
                kind, value = out.get(timeout=0.1)
            except queue.Empty:
                continue
            if cancelled.is_set():  # a cancel that arrives with the answer wins, even after the files were linked
                kind = None
                raise Cancelled()
            break
    except BaseException:
        call.stop()  # kills ffmpeg and hangs up on the server
        raise
    finally:
        thread.join(0.5)  # after a cancel the worker is a daemon that has been hung up on; it must not delay the answer
        if kind != "done":  # a failure or a cancel leaves no file of this run behind
            outputs.discard()
    if kind == "error":
        say("error", value)
        return {"exit_code": 1}
    return value


def invoke(params):
    if params.get("command") != "transcribe":
        raise Failure(f"unknown command {params.get('command')}", -32601)
    inputs = params.get("inputs") or {}
    cwd = params.get("cwd") or os.getcwd()
    if not isinstance(inputs, dict) or not isinstance(cwd, str):
        raise Failure("inputs must be an object and cwd a string", -32602)
    return transcribe(inputs, params.get("in_dir"), params.get("work"), cwd)


def handle(msg):
    global pending
    method, msg_id, params = msg.get("method"), msg.get("id"), msg.get("params")
    if method is None or msg_id is None:
        return False
    counted = method == "invoke"  # the reader counted this invoke as pending
    try:
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise Failure("params must be an object", -32602)
        if method == "initialize":
            result = {"name": "transcribe"}
        elif method == "invoke":
            result = invoke(params)
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
    except Exception as e:  # the plugin must always answer
        say("error", f"transcribe failed: {e!r}")
        send({"id": msg_id, "error": {"code": -32603, "message": f"internal error: {e}"}})
    finally:
        if counted:
            with state_lock:
                pending -= 1
                cancelled.clear()
    return False


def read_stdin():
    global pending
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(msg, dict):
            continue
        if msg.get("method") == "cancel":
            with state_lock:
                if pending:  # a cancel with nothing pending or running is ignored
                    cancelled.set()  # seen at once, even while a request is busy
            continue
        if msg.get("method") == "invoke" and msg.get("id") is not None:
            with state_lock:
                pending += 1
        inbox.put(msg)
    with state_lock:
        if pending:  # stdin closed during an invoke: stop
            cancelled.set()
    inbox.put(None)


def main():
    threading.Thread(target=read_stdin, daemon=True).start()
    while True:
        message = inbox.get()
        if message is None or handle(message):
            break


if __name__ == "__main__":
    main()
