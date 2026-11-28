"""The transcribe plugin driven over its stdio protocol, against a stand-in Whisper server and fake ffmpeg/ffprobe."""

import contextlib
import io
import json
import os
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import unittest
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN))
MAIN = PLUGIN / "main.py"

from dock_plugin import cancel, files, rpc
from transcribecore import client, render, run

rpc.setup({"transcribe": run.run}, "transcribe", "transcribing")  # so rpc.invoke can run the command in this process

RATE_BYTES = 32000  # bytes of 16 kHz int16 mono PCM per second
WINDOW = 19_200_000  # bytes in a 600 s window
MAX_POST = 19_520_000  # bytes in a 610 s window

# Fake ffmpeg: logs its argv and pid, then writes the pattern in FAKE_PCM ("t600,s10": seconds of tone/silence,
# q = quiet above the silence threshold, n = quiet below it). A tone second S has the sample value 200 + S, so the
# first sample of a window says which second it starts at.
FAKE_FFMPEG = """#!{python}
import json, os, sys
d = os.environ["FAKE_DIR"]
with open(os.path.join(d, "ffmpeg.json"), "a") as f:
    null = os.path.samestat(os.fstat(0), os.stat(os.devnull))
    f.write(json.dumps({{"argv": sys.argv[1:], "stdin_null": null}}) + "\\n")
with open(os.path.join(d, "ffmpeg.pid"), "w") as f:
    f.write(str(os.getpid()))
out, sec = sys.stdout.buffer, 0
try:
    for item in filter(None, os.environ.get("FAKE_PCM", "").split(",")):
        kind, n = item[0], int(item[1:])
        for _ in range(n):
            amp = {{"t": 200 + sec, "q": 120, "n": 50, "s": 0}}[kind]
            out.write(amp.to_bytes(2, "little", signed=True) * 16000)
            sec += 1
    out.flush()
except BrokenPipeError:
    pass
sys.exit(int(os.environ.get("FAKE_FFMPEG_EXIT", "0")))
"""

FAKE_FFPROBE = """#!{python}
import json, os, sys
d = os.environ["FAKE_DIR"]
with open(os.path.join(d, "ffprobe.json"), "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
if os.environ.get("FAKE_PROBE_EXIT"):
    sys.exit(int(os.environ["FAKE_PROBE_EXIT"]))
out = {{"streams": [{{"codec_type": "audio"}}] if os.environ.get("FAKE_HAS_AUDIO", "1") == "1" else [],
       "format": {{"duration": os.environ.get("FAKE_DURATION", "N/A")}}}}
print(json.dumps(out))
"""


def seconds_of(pattern):
    return sum(int(i[1:]) for i in pattern.split(",") if i)


def reply(language="en", segments=None, **kw):
    segments = [{"start": 0.0, "end": 2.0, "text": " Hello there."}] if segments is None else segments
    return {"json": {"language": language, "segments": segments}, **kw}


def seg(start, end, text):
    return {"start": start, "end": end, "text": text}


class Server:
    """A local HTTP server answering POST /transcribe. `replies` is one reply per request (the last repeats): a dict
    with optional status, json, raw, block, drop. A blocking reply waits until `release` is set."""

    def __init__(self, replies=None, on_request=None):
        self.requests: list[dict] = []
        self.cond = threading.Condition()
        self.release = threading.Event()
        replies = replies or [reply()]
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                size = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(size)
                first = struct.unpack("<h", body[:2])[0] if body else None
                with outer.cond:
                    outer.requests.append({"path": self.path, "length": size, "first": first,
                                           "type": self.headers.get("Content-Type")})
                    n = len(outer.requests)
                    outer.cond.notify_all()
                if on_request:
                    on_request(n)
                one = replies[min(n, len(replies)) - 1]
                if one.get("drop"):
                    return  # closes the connection without an answer
                if one.get("block"):
                    outer.release.wait(30)
                data = one.get("raw")
                if data is None:
                    data = json.dumps(one["json"]).encode()
                try:
                    self.send_response(one.get("status", 200))
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:
                    pass

            def log_message(self, format, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def wait_for(self, n):
        with self.cond:
            if not self.cond.wait_for(lambda: len(self.requests) >= n, 30):
                raise AssertionError(f"server saw {len(self.requests)} requests, wanted {n}")

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()


class StuckServer:
    """Accepts one connection, reads the first bytes of the request and then never reads again."""

    def __init__(self):
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.url = f"http://127.0.0.1:{self.listener.getsockname()[1]}"
        self.got_bytes = threading.Event()
        self.conns: list[socket.socket] = []
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        try:
            conn, _ = self.listener.accept()
            self.conns.append(conn)
            conn.recv(65536)
            self.got_bytes.set()
        except OSError:
            pass

    def close(self):
        for c in self.conns:
            c.close()
        self.listener.close()


class Base(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp(prefix="dock-transcribe-")).resolve()
        self.dir = root / "out"  # the cwd
        self.dir.mkdir()
        self.inn = root / "in"
        self.inn.mkdir()
        self.work = root / "work"
        self.work.mkdir()
        self.outside = root / "outside"
        self.outside.mkdir()
        self.fake = root / "fake"  # fake binaries on PATH and their logs
        self.fake.mkdir()
        for name, script in (("ffmpeg", FAKE_FFMPEG), ("ffprobe", FAKE_FFPROBE)):
            path = self.fake / name
            path.write_text(script.format(python=sys.executable), encoding="utf-8")
            path.chmod(path.stat().st_mode | stat.S_IXUSR)
        self.servers: list[Server] = []
        self.procs: list[subprocess.Popen] = []

    def tearDown(self):
        for p in self.procs:
            p.kill()
            p.wait()
        for s in self.servers:
            s.close()

    def server(self, *a, **kw) -> Server:
        s = Server(*a, **kw)
        self.servers.append(s)
        return s

    def media(self, name="talk.mp3"):
        (self.inn / name).write_bytes(b"not really media")
        return name

    def begin(self, url, inputs, pcm="t30", duration="auto", env=None, in_dir="default", work=None):
        e = {**os.environ, "PATH": f"{self.fake}{os.pathsep}{os.environ['PATH']}", "FAKE_DIR": str(self.fake),
             "FAKE_PCM": pcm, "DOCK_TRANSCRIBE_URL": url}
        e["FAKE_DURATION"] = str(seconds_of(pcm)) if duration == "auto" else str(duration)
        for k in ("DOCK_TRANSCRIBE_MAX_SECONDS", "FAKE_HAS_AUDIO", "FAKE_FFMPEG_EXIT", "FAKE_PROBE_EXIT"):
            e.pop(k, None)
        e.update(env or {})
        p = subprocess.Popen([sys.executable, str(MAIN)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, env=e)
        self.procs.append(p)
        params = {"command": "transcribe", "args": [], "cwd": str(self.dir), "inputs": inputs}
        if in_dir == "default":
            in_dir = str(self.inn)
        if in_dir is not None:
            params["in_dir"] = in_dir
        if work is not None:
            params["work"] = work
        self.send(p, id=1, method="initialize", params={})
        self.send(p, id=2, method="invoke", params=params)
        return p

    def inproc(self, url, inputs, pcm="t30"):
        """Runs the transcribe command in this process (for seams a subprocess cannot reach); returns (the result or
        the exception it raised, the notifications)."""
        env = {"PATH": f"{self.fake}{os.pathsep}{os.environ['PATH']}", "FAKE_DIR": str(self.fake), "FAKE_PCM": pcm,
               "FAKE_DURATION": str(seconds_of(pcm)), "DOCK_TRANSCRIBE_URL": url}
        out = io.StringIO()
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out):
            try:
                result = rpc.invoke({"command": "transcribe", "inputs": inputs, "cwd": str(self.dir),
                                     "in_dir": str(self.inn)})
            except BaseException as e:
                result = e
        cancel.cancelled.clear()
        return result, [json.loads(x) for x in out.getvalue().splitlines() if x.startswith("{")]

    @staticmethod
    def send(p, **msg):
        p.stdin.write(json.dumps({"jsonrpc": "2.0", **msg}) + "\n")
        p.stdin.flush()

    @staticmethod
    def collect(p):
        # Read until the shutdown answer before closing stdin: closing it mid-invoke cancels the invoke.
        lines = []
        for line in p.stdout:
            lines.append(line)
            if line.startswith("{") and json.loads(line).get("id") == 3:
                break
        rest, _ = p.communicate(timeout=60)
        msgs = [json.loads(x) for x in "".join(lines).splitlines() + rest.splitlines() if x.startswith("{")]
        return next(m for m in msgs if m.get("id") == 2), [m for m in msgs if "method" in m]

    def invoke(self, url, inputs, **kw):
        """Returns (the invoke answer, the notifications seen before it)."""
        p = self.begin(url, inputs, **kw)
        self.send(p, id=3, method="shutdown", params={})
        return self.collect(p)

    def run_one(self, server=None, name="talk.mp3", inputs=None, **kw):
        """Transcribes a fresh `name`; returns (server, answer, notes)."""
        s = server or self.server()
        answer, notes = self.invoke(s.url, {"file": self.media(name), **(inputs or {})}, **kw)
        return s, answer, notes

    def said(self, notes, level="error"):
        return " ".join(n["params"]["message"] for n in notes if n["method"] == "log" and n["params"]["level"] == level)

    def files(self):
        return sorted(p.name for p in self.dir.iterdir())

    def log(self, name):
        path = self.fake / name
        return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []

    def assertRefused(self, answer, notes, s=None, contains="", decoded=False):
        self.assertEqual(answer["result"], {"exit_code": 1})
        self.assertIn(contains, self.said(notes))
        self.assertTrue(self.said(notes))
        if s is not None:
            self.assertEqual(s.requests, [])
        self.assertEqual(self.files(), [])
        if not decoded:
            self.assertEqual(self.log("ffmpeg.json"), [])

    def assertGone(self):
        pid = int((self.fake / "ffmpeg.pid").read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)


class Manifest(unittest.TestCase):
    def test_the_manifest_matches_the_spec(self):  # R15
        m = tomllib.loads((PLUGIN / "plugin.toml").read_text(encoding="utf-8"))
        self.assertEqual((m["id"], m["version"], m["api_version"]), ("dev.dock.transcribe", "0.1.0", "2"))
        self.assertEqual(m["run"]["container"], {"cmd": "python3", "args": ["/plugin/main.py"]})
        self.assertEqual(m["capabilities"]["permissions"], ["network", "fs:read:in", "fs:write:cwd"])
        self.assertEqual(m["capabilities"]["env"], ["DOCK_TRANSCRIBE_URL", "DOCK_TRANSCRIBE_MAX_SECONDS"])
        (cmd,) = m["commands"]
        self.assertEqual(cmd["name"], "transcribe")
        self.assertEqual([i["name"] for i in cmd["inputs"]], ["file", "format", "language", "output"])
        self.assertEqual([i.get("flag") for i in cmd["inputs"]], [None, "--format", "--language", "--output"])
        self.assertTrue(cmd["inputs"][0]["required"])
        self.assertEqual(cmd["inputs"][3]["short"], "-o")
        self.assertTrue({"transcribe", "speech to text", "subtitles", "captions", "srt", "whisper"} <= set(cmd["keywords"]))
        self.assertEqual(run.DEFAULT_URL, "http://host.docker.internal:7862")


class Render(unittest.TestCase):
    def test_cues_past_an_hour_and_the_empty_cases(self):  # R10
        cues = [(3661.789, 7322.0, "late"), (0.0004, 0.9996, "edge")]
        self.assertEqual(render.render("txt", cues), "late\nedge\n")
        self.assertEqual(render.render("srt", cues),
                         "1\n01:01:01,789 --> 02:02:02,000\nlate\n\n2\n00:00:00,000 --> 00:00:01,000\nedge\n")
        self.assertEqual(render.render("vtt", cues),
                         "WEBVTT\n\n01:01:01.789 --> 02:02:02.000\nlate\n\n00:00:00.000 --> 00:00:01.000\nedge\n")
        self.assertEqual([render.render(f, []) for f in ("txt", "srt", "vtt")], ["", "", "WEBVTT\n"])

    def test_an_arrow_in_the_text_cannot_fake_a_cue_line(self):
        for fmt, sep in (("srt", ","), ("vtt", ".")):
            body = render.render(fmt, [(0, 1, "a --> b")])
            self.assertEqual([x for x in body.splitlines() if "-->" in x], [f"00:00:00{sep}000 --> 00:00:01{sep}000"])
            self.assertIn("a -> b", body)


class Transcribe(Base):
    def test_one_short_file_into_txt_with_the_documented_calls(self):  # R7 R12
        s, answer, notes = self.run_one(self.server([reply(segments=[seg(0, 2, " Hello there. ")])]))
        self.assertEqual(answer["result"], {"exit_code": 0, "outputs": {"files": [str(self.dir / "talk.txt")],
                                                                          "language": "en"}})
        self.assertEqual((self.dir / "talk.txt").read_text(encoding="utf-8"), "Hello there.\n")
        self.assertEqual(self.files(), ["talk.txt"])
        self.assertEqual(s.requests, [{"path": "/transcribe", "length": 30 * RATE_BYTES, "first": 200,
                                       "type": "application/octet-stream"}])
        (call,) = self.log("ffmpeg.json")
        path = str(self.inn / "talk.mp3")
        self.assertEqual(call["argv"], ["-nostdin", "-v", "error", "-protocol_whitelist", "file,pipe", "-i", path, "-map", "0:a:0", "-vn", "-ac", "1",
                                        "-ar", "16000", "-f", "s16le", "pipe:1"])
        self.assertTrue(call["stdin_null"])
        (probe,) = self.log("ffprobe.json")
        self.assertEqual(probe[:2], ["-v", "error"])
        self.assertEqual(probe[-4:], ["-protocol_whitelist", "file,pipe", "-i", path])
        self.assertEqual(self.said(notes, "info"), "transcribed 0:30 (en) into talk.txt")

    def test_formats_are_a_set_written_in_a_fixed_order_with_golden_content(self):  # R9 R10 R12
        r = reply(segments=[seg(3.0, 4.0, "  Second\n line "), seg(0.5, 2.25, " Hello there. ")])  # out of order
        s, answer, notes = self.run_one(self.server([r]), inputs={"format": "vtt,srt,txt,txt"})
        out = [str(self.dir / f"talk.{x}") for x in ("txt", "srt", "vtt")]
        self.assertEqual(answer["result"]["outputs"]["files"], out)
        self.assertEqual(self.files(), ["talk.srt", "talk.txt", "talk.vtt"])
        self.assertEqual((self.dir / "talk.txt").read_text(), "Hello there.\nSecond line\n")
        self.assertEqual((self.dir / "talk.srt").read_text(),
                         "1\n00:00:00,500 --> 00:00:02,250\nHello there.\n\n2\n00:00:03,000 --> 00:00:04,000\nSecond line\n")
        self.assertEqual((self.dir / "talk.vtt").read_text(),
                         "WEBVTT\n\n00:00:00.500 --> 00:00:02.250\nHello there.\n\n00:00:03.000 --> 00:00:04.000\nSecond line\n")
        self.assertEqual(self.said(notes, "info"), "transcribed 0:30 (en) into talk.txt, talk.srt, talk.vtt")

    def test_no_speech_writes_empty_files_and_warns(self):  # R10
        s, answer, notes = self.run_one(self.server([reply(segments=[])]), inputs={"format": "txt,srt,vtt"})
        self.assertEqual(answer["result"]["exit_code"], 0)
        self.assertEqual([(self.dir / f"talk.{x}").read_text() for x in ("txt", "srt", "vtt")], ["", "", "WEBVTT\n"])
        self.assertEqual(self.said(notes, "warn"), "no speech found")

    def test_a_bad_format_language_or_output_is_refused_before_any_work(self):  # R9
        cases = {"format": ["mp3", "txt,,srt", "txt;srt", "TXT,json"],
                 "language": ["English", "EN", "e", "en-US", "e1"],
                 "output": ["a/b", "a\\b", ".", "..", "x" * 201, "a\0b", "a\nb"]}
        for key, values in cases.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    s, answer, notes = self.run_one(inputs={key: value})
                    self.assertRefused(answer, notes, s, key)

    def test_the_stem_is_the_name_without_its_last_suffix_or_the_given_one(self):  # R9
        self.run_one(name="talk.final.mp3")
        self.run_one(name="plain", inputs={"output": "mine"})
        self.assertEqual(self.files(), ["mine.txt", "talk.final.txt"])

    def test_the_language_is_passed_through_and_a_server_refusal_is_quoted(self):  # R9
        s, answer, notes = self.run_one(self.server([reply("haw")]), inputs={"language": "haw"})
        self.assertEqual(answer["result"]["outputs"]["language"], "haw")
        self.assertEqual(s.requests[0]["path"], "/transcribe?language=haw")
        bad = self.server([{"status": 400, "json": {"error": "unknown language: xx"}}])
        s, answer, notes = self.run_one(bad, name="b.mp3", inputs={"language": "xx"})
        self.assertEqual(answer["result"], {"exit_code": 1})
        self.assertIn("unknown language: xx", self.said(notes))

    def test_25_minutes_make_three_shifted_windows_without_duplicates(self):  # R8 R12
        s = self.server([
            reply("de", [seg(0, 2, "a"), seg(595, 599, "b one two")]),
            reply("de", [seg(5, 9, "b one two"), seg(10, 12, "c"), seg(605, 609, "d four five")]),
            reply("de", [seg(5, 9, "d four five"), seg(10, 14, "e")]),
        ])
        _, answer, notes = self.run_one(s, inputs={"format": "txt,srt"}, pcm="t1500")
        self.assertEqual([r["length"] for r in s.requests], [WINDOW, MAX_POST, 9_920_000])
        self.assertEqual([r["first"] for r in s.requests], [200, 200 + 590, 200 + 1190])
        self.assertEqual([r["path"] for r in s.requests],
                         ["/transcribe", "/transcribe?language=de", "/transcribe?language=de"])
        self.assertEqual((self.dir / "talk.txt").read_text(), "a\nb one two\nc\nd four five\ne\n")
        srt = (self.dir / "talk.srt").read_text()
        self.assertIn("3\n00:10:00,000 --> 00:10:02,000\nc\n", srt)
        self.assertIn("4\n00:19:55,000 --> 00:19:59,000\nd four five\n", srt)
        self.assertIn("5\n00:20:00,000 --> 00:20:04,000\ne\n", srt)
        starts = [m.split(" --> ")[0] for m in srt.splitlines() if " --> " in m]
        self.assertEqual(starts, sorted(starts))
        self.assertEqual(self.said(notes, "info"), "transcribed 25:00 (de) into talk.txt, talk.srt")
        bars = [n["params"] for n in notes if n["method"] == "bar"]
        self.assertEqual([b["id"] for b in bars], ["transcribe"] * 4)
        self.assertEqual([b["label"] for b in bars], ["transcribing"] * 4)
        self.assertEqual([b["percent"] for b in bars], [0, 40, 80, 100])
        self.assertEqual([b.get("done") for b in bars], [None, None, None, True])

    def test_a_silent_window_is_skipped_and_the_language_is_pinned_from_the_first_sent(self):  # R8 R9
        s = self.server([reply("fr", [seg(1, 2, "x")]), reply("fr", [seg(20, 21, "y")])])
        _, answer, notes = self.run_one(s, pcm="t590,s620,t290")
        self.assertEqual(answer["result"]["exit_code"], 0)
        self.assertEqual([r["first"] for r in s.requests], [200, 0])
        self.assertEqual([r["path"] for r in s.requests], ["/transcribe", "/transcribe?language=fr"])
        self.assertEqual((self.dir / "talk.txt").read_text(), "x\ny\n")

    def test_the_silence_threshold_sits_between_rms_50_and_120(self):  # R8
        s, answer, notes = self.run_one(pcm="n20")
        self.assertEqual((s.requests, self.said(notes, "warn")), ([], "no speech found"))
        s, answer, notes = self.run_one(name="b.mp3", pcm="q20")
        self.assertEqual(len(s.requests), 1)

    def test_an_existing_target_stops_everything_before_decoding(self):  # R11
        (self.dir / "talk.srt").write_text("mine")
        s, answer, notes = self.run_one(inputs={"format": "txt,srt"})
        self.assertEqual(answer["result"], {"exit_code": 1})
        self.assertEqual(self.said(notes), "talk.srt already exists")
        self.assertEqual((s.requests, self.log("ffmpeg.json"), self.log("ffprobe.json")), ([], [], []))
        self.assertEqual(self.files(), ["talk.srt"])
        self.assertEqual((self.dir / "talk.srt").read_text(), "mine")

    def test_a_file_that_appears_while_working_is_kept_and_this_runs_outputs_are_removed(self):  # R11
        def appear(n):
            (self.dir / "talk.srt").write_text("mine")

        s = self.server(on_request=appear)
        answer, notes = self.invoke(s.url, {"file": self.media(), "format": "txt,srt,vtt"})
        self.assertEqual(answer["result"], {"exit_code": 1})
        self.assertEqual(self.said(notes), "talk.srt already exists")
        self.assertEqual(self.files(), ["talk.srt"])
        self.assertEqual((self.dir / "talk.srt").read_text(), "mine")

    def test_input_outside_the_folders_or_not_a_regular_file_is_refused(self):  # R6
        (self.outside / "x.mp3").write_bytes(b"x")
        (self.inn / "link.mp3").symlink_to(self.outside / "x.mp3")
        os.mkfifo(self.inn / "pipe.mp3")
        for name in ("../outside/x.mp3", str(self.outside / "x.mp3"), "link.mp3", "pipe.mp3", "missing.mp3"):
            with self.subTest(name=name):
                s = self.server()
                answer, notes = self.invoke(s.url, {"file": name})
                self.assertRefused(answer, notes, s)

    def test_input_is_found_in_the_work_folder_and_through_an_inside_symlink(self):  # R6
        (self.work / "w.mp3").write_bytes(b"x")
        (self.inn / "real.mp3").write_bytes(b"x")
        (self.inn / "alias.mp3").symlink_to(self.inn / "real.mp3")
        for name in (str(self.work / "w.mp3"), "alias.mp3"):
            with self.subTest(name=name):
                s = self.server()
                answer, notes = self.invoke(s.url, {"file": name, "output": Path(name).stem + "-t"}, work=str(self.work))
                self.assertEqual(answer["result"]["exit_code"], 0, self.said(notes))
        self.assertEqual(self.files(), ["alias-t.txt", "w-t.txt"])

    def test_a_file_without_audio_or_that_cannot_be_decoded_is_refused(self):  # R7
        s, answer, notes = self.run_one(env={"FAKE_HAS_AUDIO": "0"})
        self.assertRefused(answer, notes, s, "not a media file with audio: talk.mp3")
        s = self.server()
        for env in ({"FAKE_PROBE_EXIT": "1"}, {"FAKE_FFMPEG_EXIT": "1"}):
            with self.subTest(env=env):
                answer, notes = self.invoke(s.url, {"file": "talk.mp3"}, pcm="", duration=5, env=env)
                self.assertRefused(answer, notes, s, "not a media file with audio: talk.mp3", decoded=True)

    def test_a_probed_duration_over_the_limit_is_refused_before_decoding(self):  # R7
        s, answer, notes = self.run_one(duration=14401)
        self.assertRefused(answer, notes, s, "input is longer than 4:00:00")
        s, answer, notes = self.run_one(duration=101, env={"DOCK_TRANSCRIBE_MAX_SECONDS": "100"}, name="b.mp3")
        self.assertRefused(answer, notes, s, "input is longer than 1:40")
        s, answer, notes = self.run_one(duration=100, env={"DOCK_TRANSCRIBE_MAX_SECONDS": "100"}, name="c.mp3",
                                        pcm="t100")
        self.assertEqual(answer["result"]["exit_code"], 0)

    def test_decoded_audio_over_the_limit_is_refused_and_ffmpeg_is_killed(self):  # R7
        s, answer, notes = self.run_one(pcm="t41", duration="N/A", env={"DOCK_TRANSCRIBE_MAX_SECONDS": "40"})
        self.assertRefused(answer, notes, s, "input is longer than 0:40", decoded=True)
        self.assertGone()

    def test_the_limit_must_be_a_positive_integer(self):  # R7
        for value in ("0", "-5", "abc", "1.5", " 5"):
            with self.subTest(value=value):
                s, answer, notes = self.run_one(env={"DOCK_TRANSCRIBE_MAX_SECONDS": value})
                self.assertRefused(answer, notes, s, "DOCK_TRANSCRIBE_MAX_SECONDS")

    def test_a_server_failure_on_window_2_leaves_nothing_and_stops_ffmpeg(self):  # R11 R14
        s = self.server([reply(), {"status": 500, "json": {"error": "RuntimeError: boom"}}])
        _, answer, notes = self.run_one(s, inputs={"format": "txt,srt,vtt"}, pcm="t1500")
        self.assertRefused(answer, notes, None, "RuntimeError: boom", decoded=True)
        self.assertIn("HTTP 500", self.said(notes))
        self.assertEqual(len(s.requests), 2)
        self.assertGone()

    def test_busy_and_broken_answers_exit_1_with_the_servers_words(self):  # R14
        cases = [({"status": 503, "json": {"error": "busy: a request is already waiting"}}, "busy: a request"),
                 ({"status": 500, "raw": b"<html>", "json": None}, "HTTP 500"),
                 ({"raw": b"not json", "json": None}, "broken answer"),
                 (reply(segments="no"), "broken answer"),
                 (reply(segments=[seg(5, 1, "x")]), "broken answer")]
        for one, want in cases:
            with self.subTest(want=want, one=one):
                s, answer, notes = self.run_one(self.server([one]))
                self.assertRefused(answer, notes, None, want, decoded=True)

    def test_an_unreachable_server_names_the_url_and_how_to_start_it(self):  # R14
        s = self.server()
        url = s.url
        s.close()
        answer, notes = self.invoke(url, {"file": self.media()})
        self.assertRefused(answer, notes, None, url, decoded=True)
        said = self.said(notes)
        self.assertIn("uv run --project plugins/transcribe/server python plugins/transcribe/server/serve.py", said)
        self.assertIn("DOCK_TRANSCRIBE_URL", said)

    def test_wrong_input_types_are_invalid_params(self):  # R14
        for inputs in ({}, {"file": 5}, {"file": ""}, {"file": "a", "format": 1}, {"file": "a", "language": ["en"]},
                       {"file": "a", "output": 3}):
            with self.subTest(inputs=inputs):
                answer, _ = self.invoke(self.server().url, inputs)
                self.assertEqual(answer["error"]["code"], -32602)
        answer, _ = self.invoke(self.server().url, {"file": "a"}, in_dir=5)
        self.assertEqual(answer["error"]["code"], -32602)

    def test_the_silence_gate_looks_at_the_loudest_second(self):  # R8
        s, answer, notes = self.run_one(pcm="s300,q5,s295")  # 5 s above the threshold in a 600 s window
        self.assertEqual(len(s.requests), 1)
        s, answer, notes = self.run_one(name="b.mp3", pcm="s600")
        self.assertEqual(s.requests, [])
        self.assertEqual(answer["result"], {"exit_code": 0, "outputs": {"files": [str(self.dir / "b.txt")], "language": None}})
        self.assertEqual(self.said(notes, "info"), "transcribed 10:00 (unknown) into b.txt")

    A1 = "market filled with farmers selling apples, cheese, and baskets woven from willow. Part 89. When the"
    A2 = "sun went down, the lamplighter walked slowly from post to post, humming a song nobody else remembered."
    B1 = "Part 89. When the sun went down, the lamplighter walked slowly from post to post, humming a song"
    B2 = "nobody else remembered. Part 90. Some evenings a storm rolled in from the hills, and the whole"

    def test_a_real_seam_is_spliced_by_words_losing_and_repeating_nothing(self):  # R8
        s = self.server([
            reply("en", [seg(585.34, 591.76, " " + self.A1), seg(591.76, 597.42, " " + self.A2),
                         seg(597.76, 600.0, " Part 90. Some evenings a storm rose.")]),
            reply("en", [seg(0.0, 6.22, " " + self.B1), seg(6.22, 11.66, " " + self.B2),
                         seg(11.66, 16.94, " valley listened to the rain on the slate roofs.")])])
        self.run_one(s, pcm="t630", inputs={"format": "txt,srt"})
        text = (self.dir / "talk.txt").read_text()
        self.assertEqual(" ".join(text.split()), "market filled with farmers selling apples, cheese, and baskets woven "
                         "from willow. " + self.B1 + " " + self.B2 + " valley listened to the rain on the slate roofs.")
        srt = (self.dir / "talk.srt").read_text()
        starts = [m.split(" --> ")[0] for m in srt.splitlines() if " --> " in m]
        self.assertEqual(starts, sorted(starts))

    def test_a_seam_with_no_common_words_is_cut_at_the_middle_of_the_overlap(self):  # R8
        s = self.server([reply("en", [seg(580, 594, "alpha beta gamma"), seg(596, 600, "delta epsilon zeta")]),
                         reply("en", [seg(1, 4, "one two three"), seg(6, 9, "four five six"), seg(20, 30, "seven")])])
        self.run_one(s, pcm="t630")
        self.assertEqual((self.dir / "talk.txt").read_text(), "alpha beta gamma\nfour five six\nseven\n")

    def test_an_uncut_last_segment_is_not_repeated_by_the_next_window(self):  # R8
        s = self.server([reply("en", [seg(0, 2, "a"), seg(590, 598, "the quick brown fox jumps")]),
                         reply("en", [seg(0, 8, "the quick brown fox jumps"), seg(10, 12, "over the lazy dog")])])
        self.run_one(s, pcm="t630")
        self.assertEqual((self.dir / "talk.txt").read_text(), "a\nthe quick brown fox jumps\nover the lazy dog\n")

    def test_a_silent_window_after_a_seam_does_not_lose_or_repeat_the_tail(self):  # R8
        s = self.server([reply("en", [seg(1, 2, "a"), seg(595, 600, "tail words here")]),
                         reply("en", [seg(30, 31, "later")])])
        self.run_one(s, pcm="t590,s620,t290")
        self.assertEqual((self.dir / "talk.txt").read_text(), "a\ntail words here\nlater\n")

    def test_a_cut_segment_at_the_very_end_of_the_audio_is_kept(self):  # R8
        s = self.server([reply("en", [seg(595, 600, "tail")])])
        self.run_one(s, pcm="t600")  # exactly one window, then no more audio
        self.assertEqual((self.dir / "talk.txt").read_text(), "tail\n")

    def test_a_bad_server_url_is_refused_before_any_work(self):
        for url in ("ftp://h:1", "https://h:1", "http://", "http://:80", "http://h:0", "http://h:65536", "http://h:x",
                    "localhost:7862"):
            with self.subTest(url=url):
                answer, notes = self.invoke(url, {"file": self.media()})
                self.assertRefused(answer, notes, None, "DOCK_TRANSCRIBE_URL is not a valid http URL")

    def test_the_servers_error_is_quoted_up_to_300_characters(self):
        bad = self.server([{"status": 400, "json": {"error": "x" * 1000}}])
        s, answer, notes = self.run_one(bad)
        self.assertIn("x" * 300, self.said(notes))
        self.assertNotIn("x" * 301, self.said(notes))

    def test_a_language_that_is_not_a_code_is_a_broken_answer(self):
        for language in ("EN", "en; rm", "../x", "", "e", "abcdefghi", 5):
            with self.subTest(language=language):
                s, answer, notes = self.run_one(self.server([reply(language)]))
                self.assertRefused(answer, notes, None, "broken answer", decoded=True)

    def test_ffmpeg_failing_after_a_window_was_decoded_is_a_partial_decode(self):
        s, answer, notes = self.run_one(env={"FAKE_FFMPEG_EXIT": "1"})
        self.assertRefused(answer, notes, None, "could not decode all of talk.mp3", decoded=True)
        self.assertEqual(len(s.requests), 1)

    def test_a_server_that_hangs_up_without_answering_is_not_called_unreachable(self):
        s, answer, notes = self.run_one(self.server([{"drop": True}]))
        self.assertRefused(answer, notes, None, f"transcribe server stopped answering ({s.url})", decoded=True)
        self.assertNotIn("cannot reach", self.said(notes))

    def test_the_answer_timeout_is_20_minutes_and_the_connect_timeout_10_seconds(self):
        self.assertEqual((client.TIMEOUT, client.CONNECT_TIMEOUT), (1200, 10))

    def test_a_server_that_never_answers_times_out_with_its_own_message(self):
        s = self.server([{"block": True}])
        started = time.monotonic()
        with mock.patch.object(client, "TIMEOUT", 0.5):
            result, notes = self.inproc(s.url, {"file": self.media()})
        self.assertLess(time.monotonic() - started, 5)  # the 10 s connect timeout must not apply to the answer
        self.assertEqual(result, {"exit_code": 1})
        self.assertIn("timed out after 0.5 s waiting for the transcribe server", self.said(notes))
        self.assertNotIn("cannot reach", self.said(notes))
        self.assertEqual(self.files(), [])

    def test_a_cancel_after_the_files_are_linked_removes_them_and_is_cancelled(self):  # R13
        real, seen = files.Outputs.publish, []

        def publish_then_cancel(outputs, files, call):
            real(outputs, files, call)
            seen.append(self.files())
            cancel.cancelled.set()

        with mock.patch.object(files.Outputs, "publish", publish_then_cancel):
            result, _ = self.inproc(self.server().url, {"file": self.media(), "format": "txt,srt"})
        self.assertEqual(seen, [["talk.srt", "talk.txt"]])
        self.assertIsInstance(result, cancel.Cancelled)
        self.assertEqual(self.files(), [])

    def test_a_cancel_while_connecting_closes_the_socket_and_answers_fast(self):  # R13
        entered, freed = threading.Event(), threading.Event()

        def hung_connect(sock, addr):  # stands in for a connect to a black hole; only a close ends it
            entered.set()
            for _ in range(500):
                if sock.fileno() == -1:
                    freed.set()
                    break
                time.sleep(0.02)
            raise OSError("closed")

        threading.Thread(target=lambda: entered.wait(30) and cancel.cancelled.set(), daemon=True).start()
        started = time.monotonic()
        with mock.patch.object(socket.socket, "connect", hung_connect):
            result, _ = self.inproc("http://127.0.0.1:9", {"file": self.media()})
        self.assertLess(time.monotonic() - started, 2)
        self.assertIsInstance(result, cancel.Cancelled)
        self.assertTrue(freed.wait(2), "the connecting socket was never closed")
        self.assertEqual(self.files(), [])

    def test_a_cancel_while_sending_a_large_body_answers_fast(self):  # R13
        stuck = StuckServer()
        self.servers.append(stuck)  # ty: ignore[invalid-argument-type]
        p = self.begin(stuck.url, {"file": self.media()}, pcm="t600")
        self.assertTrue(stuck.got_bytes.wait(30))  # the body is going out and the server has stopped reading
        self.assertCancelledFast(p)

    def assertCancelledFast(self, p, cancel=True):
        """Cancels p (unless already done) and expects the -32800 answer within 2 s, nothing left behind."""
        watchdog = threading.Timer(15, p.kill)  # a hung plugin must fail the test, not stall it
        watchdog.start()
        started = time.monotonic()
        if cancel:
            self.send(p, method="cancel", params={})
        answer = None
        for line in p.stdout:
            if line.startswith("{") and json.loads(line).get("id") == 2:
                answer = json.loads(line)
                break
        watchdog.cancel()
        self.assertLess(time.monotonic() - started, 2)
        assert answer is not None
        self.assertEqual(answer["error"]["code"], -32800)
        self.assertGone()
        self.assertEqual(self.files(), [])

    def cancel_in_window_2(self, how):
        s = self.server([reply(), {"block": True}])
        p = self.begin(s.url, {"file": self.media(), "format": "txt,srt"}, pcm="t1500")
        s.wait_for(2)
        if how == "cancel":
            self.send(p, method="cancel", params={})
        else:
            p.stdin.close()
        self.assertCancelledFast(p, cancel=False)
        if how != "cancel":
            p.wait(timeout=10)

    def test_a_cancel_during_window_2_answers_fast_and_leaves_nothing(self):  # R13
        self.cancel_in_window_2("cancel")

    def test_closing_stdin_during_window_2_cancels_the_same_way(self):  # R13
        self.cancel_in_window_2("stdin")


if __name__ == "__main__":
    unittest.main()
