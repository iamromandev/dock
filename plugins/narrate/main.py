"""narrate plugin for dock: reads a text or Markdown file aloud into a WAV file through a local Kokoro server
(stdlib only). JSON-RPC 2.0, one JSON object line on stdin/stdout. See protocol/PROTOCOL.md.
"""

import base64
import binascii
import http.client
import json
import math
import os
import queue
import re
import socket
import stat
import sys
import threading
import time
import urllib.parse
import wave

DEFAULT_URL = "http://host.docker.internal:7861"
DEFAULT_MODEL = "kokoro"
TIMEOUT = 300  # seconds to wait for an answer (a cold server needs about 40)
CONNECT_TIMEOUT = 10  # seconds to connect; a connect cannot be aborted, so a cancel waits for this at most
VOICES = ("af_heart", "af_bella", "am_michael", "am_fenrir")
SAMPLE_RATE = 24000
MAX_FILE = 1_000_000  # bytes
MAX_TEXT = 20_000  # characters of speech text
CHUNK = 400  # characters per request
MAX_REPLY = 8_000_000  # bytes accepted in one answer


class Cancelled(Exception):
    pass


class Failure(Exception):
    def __init__(self, message, code=-32000):
        super().__init__(message)
        self.code = code


class BadInput(Exception):
    """An input the user can fix; the message is told to the user."""


inbox: queue.Queue = queue.Queue()
cancelled = threading.Event()
state_lock = threading.Lock()
pending = 0  # invokes read from stdin and not yet finished; guarded by state_lock. A cancel with none is ignored.


class Call:
    """The HTTP connection of one invoke: lets the main thread close it on cancel."""

    def __init__(self):
        self.conn = None
        self.stopped = False
        self.lock = threading.Lock()

    def attach(self, conn):
        with self.lock:
            self.conn = conn
            if self.stopped:
                self.hang_up(conn)

    def cancelled(self):
        return self.stopped or cancelled.is_set()

    def hang_up(self, conn):
        # close() alone leaves the socket open while the worker is blocked reading it; shut it down first
        try:
            if conn.sock is not None:
                conn.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        conn.close()

    def stop(self):
        with self.lock:
            self.stopped = True
            if self.conn is not None:
                self.hang_up(self.conn)


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
    notify("bar", id="narrate", label="narrating", percent=percent, **extra)


def stop_if_cancelled():
    if cancelled.is_set():
        raise Cancelled()


# ---- text -> speech text -> chunks

FENCE = re.compile(r"^\s{0,3}(```|~~~)")
RULE = re.compile(r"^\s{0,3}([-*_])(\s*\1){2,}\s*$")
HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)(\s+#+)?\s*$")
LIST = re.compile(r"^\s*([-*+]|\d+[.)])\s+")
SETEXT = re.compile(r"^\s{0,3}(=+|-+)\s*$")
DELIMITER = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$")
DEFINITION = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*\S")
HIDDEN = re.compile(r"<!--.*?-->|<(script|style)\b.*?</\1\s*>|</?[A-Za-z][^<>]*>", re.DOTALL | re.IGNORECASE)
URL = r"\((?:[^()]|\([^()]*\))*\)"
QUOTE = re.compile(r"^\s*(>\s?)+")


def inline(line):
    line = re.sub(r"\[\^[^\]]*\]", "", line)  # footnote markers
    line = re.sub(r"!\[[^\]]*\]" + URL, "", line)  # images
    line = re.sub(r"\[([^\]]*)\]" + URL, r"\1", line)  # links keep their text
    line = re.sub(r"\[([^\]]*)\]\[[^\]]*\]", r"\1", line)  # reference links
    line = re.sub(r"<(https?|mailto):[^>]*>", "", line)  # autolinks
    line = re.sub(r"https?://\S+", "", line)  # bare URLs
    line = re.sub(r"`+([^`]*)`+", r"\1", line)  # inline code keeps its words
    line = re.sub(r"\*+|~~", "", line)
    line = re.sub(r"(?<!\w)_+|_+(?!\w)", "", line)
    return line


def speech_text(text, name):
    """The text to speak: .txt as is, .md without code, tables, images, links' URLs and markup."""
    if not name.lower().endswith(".md"):
        return text
    text = HIDDEN.sub("", text)  # comments, script and style blocks, tags (also over several lines)
    paragraphs: list[list[str]] = [[]]

    def flush():
        if paragraphs[-1]:
            paragraphs.append([])

    fence = None
    previous_blank = True
    in_list = False  # inside a list item: an indented paragraph after a blank line continues it
    in_table = False
    plain = False  # the previous line was plain paragraph text
    for raw in text.split("\n"):
        m = FENCE.match(QUOTE.sub("", raw))
        if fence:
            if m and m.group(1) == fence:
                fence = None
            continue
        if m:
            fence = m.group(1)
            flush()
            continue
        blank = not raw.strip()
        indented = raw.startswith(("    ", "\t"))
        was_blank, previous_blank = previous_blank, blank
        was_plain, plain = plain, False
        if blank:
            in_table = False
            flush()
            continue
        if indented and was_blank and not in_list and not LIST.match(raw):
            previous_blank = True  # more indented lines continue the code block
            continue
        if not indented and not LIST.match(raw):
            in_list = False
        if in_table or DEFINITION.match(raw):
            flush()
            continue
        if "|" in raw and DELIMITER.match(raw) and not raw.lstrip().startswith("|"):
            if was_plain:
                paragraphs[-1].pop()  # the header row
            flush()
            in_table = True
            continue
        if was_plain and SETEXT.match(raw):
            title = " ".join(paragraphs[-1]).strip()
            paragraphs[-1] = [title + "." if title and title[-1] not in ".!?:;,…" else title]
            flush()
            continue
        if raw.lstrip().startswith("|") or RULE.match(raw):
            flush()
            continue
        line = QUOTE.sub("", raw)
        heading = HEADING.match(line)
        if heading:
            flush()
            line = inline(heading.group(1)).strip()
            if line and line[-1] not in ".!?:;,…":
                line += "."
            paragraphs[-1].append(line)
            flush()
            continue
        if LIST.match(line):
            flush()
            line = LIST.sub("", line)
            in_list = True
        else:
            plain = True
        paragraphs[-1].append(inline(line))
    joined = (" ".join(" ".join(p).split()) for p in paragraphs)
    return "\n\n".join(p for p in joined if p)


def split_long(sentence, limit):
    """A sentence over the limit: pieces at spaces; a word over the limit is hard-split."""
    out: list[str] = []
    cur = ""
    for word in sentence.split():
        while len(word) > limit:
            if cur:
                out.append(cur)
                cur = ""
            out.append(word[:limit])
            word = word[limit:]
        if cur and len(cur) + 1 + len(word) > limit:
            out.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}" if cur else word
    if cur:
        out.append(cur)
    return out


def chunks(text, limit=CHUNK):
    """Pieces of at most `limit` characters: sentences packed greedily, never across a paragraph."""
    out: list[str] = []
    for paragraph in re.split(r"\n\s*\n", text):
        cur = ""
        for sentence in re.split(r"(?<=[.!?…])\s+", " ".join(paragraph.split())):
            if not sentence:
                continue
            for piece in [sentence] if len(sentence) <= limit else split_long(sentence, limit):
                if cur and len(cur) + 1 + len(piece) > limit:
                    out.append(cur)
                    cur = piece
                else:
                    cur = f"{cur} {piece}" if cur else piece
        if cur:
            out.append(cur)
    return out


# ---- inputs

def read_source(inputs, in_dir):
    """Returns (file name, text) or raises BadInput."""
    if not in_dir:
        raise BadInput("narrate needs the in folder (the host sent no in_dir)")
    name = inputs["file"]
    if "\0" in name or "\0" in in_dir:
        raise BadInput("file must be a file name without NUL characters")
    if not name.lower().endswith((".md", ".txt")):
        raise BadInput("file must end with .md or .txt")
    real_in = os.path.realpath(in_dir)
    real = os.path.realpath(os.path.join(in_dir, name))
    try:
        inside = os.path.commonpath([real, real_in]) == real_in
    except ValueError:
        inside = False
    if not inside:
        raise BadInput("file must be inside the in folder")
    try:
        # O_NONBLOCK: opening a FIFO must not wait for a writer; O_NOFOLLOW: no symlink swapped in after the check
        fd = os.open(real, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise BadInput(f"{name} is not a file in the in folder")
            if info.st_size > MAX_FILE:
                raise BadInput(f"{name} is larger than {MAX_FILE} bytes")
            data = b""
            while len(data) <= MAX_FILE:
                block = os.read(fd, MAX_FILE + 1 - len(data))
                if not block:
                    break
                data += block
        finally:
            os.close(fd)
    except OSError as e:
        raise BadInput(f"cannot read {name}: {e.strerror or e}") from None
    if len(data) > MAX_FILE:
        raise BadInput(f"{name} is larger than {MAX_FILE} bytes")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise BadInput(f"{name} is not valid UTF-8 text") from None
    return os.path.basename(real), text.replace("\r\n", "\n").replace("\r", "\n")


def check(inputs, cwd):
    """Returns (voice, speed, output path) or raises BadInput."""
    voice = inputs.get("voice") or "af_heart"
    if voice not in VOICES:
        raise BadInput(f"voice must be one of {', '.join(VOICES)}")
    try:
        speed = float(inputs.get("speed") or 1.0)
    except ValueError:
        speed = math.nan
    if not 0.5 <= speed <= 2.0:  # false for nan too
        raise BadInput("speed must be a number from 0.5 to 2.0")
    name = inputs.get("output") or time.strftime("narrate-%Y%m%d-%H%M%S.wav")
    if "/" in name or "\\" in name or name in (".", "..") or any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise BadInput("output must be a file name in the current folder")
    if len(name.encode()) > 240:
        raise BadInput("output name is too long")
    if not name.endswith(".wav"):
        raise BadInput("output must end with .wav")
    path = os.path.join(cwd, name)
    if os.path.lexists(path):
        raise BadInput(f"{name} already exists")
    return voice, speed, path


# ---- the server

def parse_reply(status, data, where):
    """Returns the PCM bytes of an answer or raises RuntimeError with the message to show."""
    try:
        reply = json.loads(data)
        if not isinstance(reply, dict):
            raise TypeError("not an object")
    except (ValueError, TypeError):
        if status != 200:
            raise RuntimeError(f"the narrate server at {where} answered HTTP {status}") from None
        raise RuntimeError(f"the narrate server at {where} sent a broken answer") from None
    if status != 200 or "error" in reply:
        raise RuntimeError(f"the narrate server at {where} says: {reply.get('error', f'HTTP {status}')}")
    if reply.get("sample_rate") != SAMPLE_RATE:
        raise RuntimeError(f"the narrate server at {where} sent sample rate {reply.get('sample_rate')}, "
                           f"not {SAMPLE_RATE}")
    try:
        audio = base64.b64decode(reply["audio"], validate=True)
    except (KeyError, TypeError, ValueError, binascii.Error):
        raise RuntimeError(f"the narrate server at {where} sent audio that is not valid base64") from None
    if len(audio) % 2 or not audio:
        raise RuntimeError(f"the narrate server at {where} sent audio that is not 16-bit PCM")
    return audio


def speak(base, body, call):
    """One POST /speak; returns PCM bytes. Raises RuntimeError with a message to show, Cancelled after a cancel.
    A request is never sent once the call is stopped: the connection is made first, then the stop flag is
    checked under the call's lock (stop() takes it too), and a closed connection never reopens by itself."""
    where = shown(base)
    parts = urllib.parse.urlsplit(base)
    cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
    conn = cls(parts.hostname or "", parts.port, timeout=CONNECT_TIMEOUT)
    conn.auto_open = 0
    call.attach(conn)
    try:
        conn.connect()
        if conn.sock is not None:
            conn.sock.settimeout(TIMEOUT)
        with call.lock:  # held only for the check: stop() must be able to shut the socket mid-send
            if call.cancelled():
                raise Cancelled()
        # a stop() from here on closes the connection, which never reopens (auto_open = 0)
        conn.request("POST", f"{parts.path.rstrip('/')}/speak", json.dumps(body).encode(),
                     {"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = resp.read(MAX_REPLY + 1)
        status = resp.status
    except (OSError, http.client.HTTPException) as e:
        if call.cancelled():
            raise Cancelled() from None
        raise RuntimeError(f"cannot reach the narrate server at {where}: {getattr(e, 'reason', e) or type(e).__name__}. "
                           "Set DOCK_NARRATE_URL if it runs elsewhere; to start it see "
                           "plugins/narrate/server/README.md") from None
    finally:
        conn.close()
    if len(data) > MAX_REPLY:
        raise RuntimeError(f"the narrate server at {where} sent an answer that is too large (over {MAX_REPLY} bytes)")
    return parse_reply(status, data, where)


def work(base, pieces, voice, speed, out, call):
    """Runs in a thread: puts ("pcm", bytes)..., then ("done", None) or ("error", text) into `out`.
    After `call.stop()` it says nothing more and sends no further request."""
    def put(item):
        if not call.stopped:
            out.put(item)

    try:
        for piece in pieces:
            body = {"text": piece, "voice": voice, "speed": speed, "model": DEFAULT_MODEL}
            put(("pcm", speak(base, body, call)))
        put(("done", None))
    except Cancelled:
        return
    except RuntimeError as e:
        put(("error", str(e)))
    except Exception as e:  # never let the thread die silently
        put(("error", f"the narrate server at {shown(base)} failed: {e!r}"))


class Wav:
    """A new WAV `path` written as the chunks arrive into path.part; never replaces a file.
    Only removes the .part it created itself."""

    def __init__(self, path):
        self.path = path
        self.name = os.path.basename(path)
        self.part = path + ".part"
        self.frames = 0
        try:
            fd = os.open(self.part, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            raise Failure(f"{self.name}.part already exists", -32000) from None
        self.created = True
        self.fh = os.fdopen(fd, "wb")
        self.wave = wave.open(self.fh, "wb")  # noqa: SIM115 (closed in publish/discard)
        self.wave.setnchannels(1)
        self.wave.setsampwidth(2)
        self.wave.setframerate(SAMPLE_RATE)

    def add(self, pcm):
        self.wave.writeframes(pcm)
        self.frames += len(pcm) // 2

    def publish(self, before_publish):
        self.wave.close()  # patches the header
        self.fh.close()
        before_publish()
        try:
            os.link(self.part, self.path)
        except FileExistsError:
            raise Failure(f"{self.name} already exists", -32000) from None

    def discard(self):
        if not self.created:
            return
        self.created = False
        for close in (self.wave.close, self.fh.close):
            try:
                close()
            except (OSError, wave.Error):
                pass
        try:
            os.remove(self.part)
        except OSError:
            pass


def narrate(inputs, in_dir, cwd):
    if not isinstance(inputs.get("file"), str) or not inputs["file"]:
        raise Failure("narrate needs the input file", -32602)
    if any(not isinstance(inputs.get(k), str | None) for k in ("voice", "speed", "output")):
        raise Failure("voice, speed and output must be strings", -32602)
    if in_dir is not None and not isinstance(in_dir, str):
        raise Failure("in_dir must be a string", -32602)
    base = (os.environ.get("DOCK_NARRATE_URL") or DEFAULT_URL).rstrip("/")
    try:
        model = os.environ.get("DOCK_NARRATE_MODEL") or DEFAULT_MODEL
        if model != DEFAULT_MODEL:
            raise BadInput(f"DOCK_NARRATE_MODEL must be {DEFAULT_MODEL}, not {model}")
        voice, speed, path = check(inputs, os.path.abspath(cwd))
        source, text = read_source(inputs, in_dir)
        text = speech_text(text, source)
        if not any(c.isalnum() for c in text):
            raise BadInput(f"{source} has nothing to read aloud")
        if len(text) > MAX_TEXT:
            raise BadInput(f"{source} has {len(text)} characters to read; the limit is {MAX_TEXT}")
    except BadInput as e:
        say("error", str(e))
        return {"exit_code": 1}
    pieces = chunks(text)
    try:
        wav = Wav(path)
    except Failure as e:
        say("error", str(e))
        return {"exit_code": 1}
    except OSError as e:
        say("error", f"cannot write {os.path.basename(path)}: {e}")
        return {"exit_code": 1}
    out: queue.Queue = queue.Queue()  # one per invoke, so a stale worker can never feed the next one
    call = Call()
    try:
        threading.Thread(target=work, args=(base, pieces, voice, speed, out, call), daemon=True).start()
        return wait_and_publish(out, wav, len(pieces))
    except BaseException:
        call.stop()  # the server sees a disconnect
        raise
    finally:
        wav.discard()


def wait_and_publish(out, wav, total):
    bar(None)
    done = 0
    while True:
        stop_if_cancelled()
        try:
            item = out.get(timeout=0.1)
        except queue.Empty:
            continue
        if item[0] == "pcm":
            try:
                wav.add(item[1])
            except OSError as e:
                say("error", f"cannot write {wav.name}: {e}")
                return {"exit_code": 1}
            done += 1
            if done < total:
                bar(min(99, int(done / total * 100)))
        elif item[0] == "error":
            say("error", item[1])
            return {"exit_code": 1}
        else:
            break
    stop_if_cancelled()
    try:
        # Once the file is linked into place the call counts as a success, even if a cancel lands after.
        wav.publish(stop_if_cancelled)
    except Failure as e:
        say("error", str(e))
        return {"exit_code": 1}
    except OSError as e:
        say("error", f"cannot write {wav.name}: {e}")
        return {"exit_code": 1}
    seconds = int(wav.frames / SAMPLE_RATE)
    bar(100, done=True)
    say("info", f"saved {wav.name} ({seconds // 60}:{seconds % 60:02d})")
    return {"exit_code": 0, "outputs": {"files": [wav.path], "seconds": round(wav.frames / SAMPLE_RATE, 1)}}


def invoke(params):
    if params.get("command") != "narrate":
        raise Failure(f"unknown command {params.get('command')}", -32601)
    inputs = params.get("inputs") or {}
    cwd = params.get("cwd") or os.getcwd()
    if not isinstance(inputs, dict) or not isinstance(cwd, str):
        raise Failure("inputs must be an object and cwd a string", -32602)
    return narrate(inputs, params.get("in_dir"), cwd)


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
            result = {"name": "narrate"}
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
        say("error", f"narrate failed: {e!r}")
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
