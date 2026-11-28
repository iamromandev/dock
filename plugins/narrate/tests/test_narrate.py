"""The narrate plugin driven over its stdio protocol, against a local stand-in for the Kokoro server."""

import base64
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import unittest
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN))
NARRATE = PLUGIN / "main.py"

import main as narrate  # the loop only runs under __main__, so the pure helpers can be tested directly


def sentence(letter, size=299):
    return letter * size + "."


def pcm(n, fill=1):
    """n frames of 16-bit mono PCM whose bytes are all `fill`."""
    return bytes([fill]) * (2 * n)


def ok(data, rate=24000):
    return {"audio": base64.b64encode(data).decode(), "sample_rate": rate}


class Server:
    """A local HTTP server answering POST /speak. `replies` is one reply per request (the last repeats):
    a dict with optional status, json, raw, delay."""

    def __init__(self, replies=None):
        self.requests: list[dict] = []
        replies = replies or [{"json": ok(pcm(100))}]
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                size = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(size))
                outer.requests.append({"path": self.path, "body": body, "headers": dict(self.headers)})
                reply = replies[min(len(outer.requests), len(replies)) - 1]
                time.sleep(reply.get("delay", 0))
                data = reply.get("raw")
                if data is None:
                    data = json.dumps(reply.get("json", ok(pcm(100)))).encode()
                try:
                    self.send_response(reply.get("status", 200))
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

    def texts(self):
        return [r["body"]["text"] for r in self.requests]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Base(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp(prefix="dock-narrate-")).resolve()
        self.dir = root / "out"  # the cwd
        self.dir.mkdir()
        self.inn = root / "in"
        self.inn.mkdir()
        self.outside = root / "outside"
        self.outside.mkdir()
        self.servers: list[Server] = []

    def tearDown(self):
        for s in self.servers:
            s.close()

    def server(self, *a, **kw) -> Server:
        s = Server(*a, **kw)
        self.servers.append(s)
        return s

    def doc(self, text, name="a.md"):
        (self.inn / name).write_text(text, encoding="utf-8")
        return name

    def start(self, url, model=None):
        env = {**os.environ, "DOCK_NARRATE_URL": url}
        env.pop("DOCK_NARRATE_MODEL", None)
        if model:
            env["DOCK_NARRATE_MODEL"] = model
        return subprocess.Popen([sys.executable, str(NARRATE)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env=env)

    @staticmethod
    def send(p, **msg):
        p.stdin.write(json.dumps({"jsonrpc": "2.0", **msg}) + "\n")
        p.stdin.flush()

    def begin(self, url, inputs, in_dir="default", model=None):
        p = self.start(url, model)
        params = {"command": "narrate", "args": [], "cwd": str(self.dir), "inputs": inputs}
        if in_dir == "default":
            in_dir = str(self.inn)
        if in_dir is not None:
            params["in_dir"] = in_dir
        self.send(p, id=1, method="initialize", params={})
        self.send(p, id=2, method="invoke", params=params)
        return p

    @staticmethod
    def collect(p):
        # Read until the shutdown answer before closing stdin: closing it mid-invoke cancels the invoke.
        lines = []
        for line in p.stdout:
            lines.append(line)
            if line.startswith("{") and json.loads(line).get("id") == 3:
                break
        rest, _ = p.communicate(timeout=30)
        msgs = [json.loads(x) for x in "".join(lines).splitlines() + rest.splitlines() if x.startswith("{")]
        return next(m for m in msgs if m.get("id") == 2), [m for m in msgs if "method" in m]

    def invoke(self, url, inputs, **kw):
        """Returns (the invoke answer, the notifications seen before it)."""
        p = self.begin(url, inputs, **kw)
        self.send(p, id=3, method="shutdown", params={})
        return self.collect(p)

    def narrate(self, text, server=None, **inputs):
        """Narrates a fresh a.md holding `text`; returns (server, answer, notes)."""
        s = server or self.server()
        answer, notes = self.invoke(s.url, {"file": self.doc(text), **inputs})
        return s, answer, notes

    def said(self, notes, level="error"):
        return " ".join(n["params"]["message"] for n in notes if n["method"] == "log" and n["params"]["level"] == level)

    def files(self):
        return sorted(p.name for p in self.dir.iterdir())

    def assertRefused(self, answer, notes, s=None, contains=""):
        self.assertEqual(answer["result"], {"exit_code": 1})
        self.assertIn(contains, self.said(notes))
        self.assertTrue(self.said(notes))
        if s is not None:
            self.assertEqual(s.requests, [])
        self.assertEqual(self.files(), [])

    def wait_for_requests(self, s, n):
        for _ in range(200):
            if len(s.requests) >= n:
                return
            time.sleep(0.05)
        self.fail(f"server saw {len(s.requests)} requests, wanted {n}")


class Manifest(unittest.TestCase):
    def test_the_manifest_matches_the_spec(self):  # R1
        m = tomllib.loads((PLUGIN / "plugin.toml").read_text(encoding="utf-8"))
        self.assertEqual((m["id"], m["version"], m["api_version"]), ("dev.dock.narrate", "0.1.0", "2"))
        self.assertEqual(m["run"]["container"], {"cmd": "python3", "args": ["/plugin/main.py"]})
        self.assertEqual(m["capabilities"]["permissions"], ["network", "fs:read:in", "fs:write:cwd"])
        self.assertEqual(m["capabilities"]["env"], ["DOCK_NARRATE_URL", "DOCK_NARRATE_MODEL"])
        (cmd,) = m["commands"]
        self.assertEqual(cmd["name"], "narrate")
        self.assertEqual([i["name"] for i in cmd["inputs"]], ["file", "voice", "speed", "output"])
        self.assertEqual(cmd["inputs"][3]["short"], "-o")


class Helpers(unittest.TestCase):
    def test_markdown_is_reduced_to_speech(self):  # R4
        md = ("# Title\n\nSome **bold** and _soft_ and ~~gone~~ text with `code words` and [a link](http://x.io/p).\n"
              "![pic](http://img/x.png) <b>tag</b> see https://bare.example/z and <https://auto.example>.\n\n"
              "```py\nsecret = 1\n```\n\n~~~\ntilde fence\n~~~\n\n    indented code\n\n"
              "| a | b |\n|---|---|\n| 1 | 2 |\n\n---\n\n- item one\n1. item two\n> quoted\n")
        out = narrate.speech_text(md, "a.md")
        for want in ("Title.", "bold", "soft", "code words", "a link", "item one", "item two", "quoted"):
            self.assertIn(want, out)
        for bad in ("*", "_", "~", "`", "|", "http", "secret", "tilde fence", "indented code", "pic", "<", "---"):
            self.assertNotIn(bad, out)

    def test_markdown_constructs_are_reduced_to_speech(self):  # R4
        cases = {
            "list continuation": ("- item\n\n    continued words\n\nafter", "item\n\ncontinued words\n\nafter"),
            "setext equals": ("Title\n=====\n\nBody", "Title.\n\nBody"),
            "setext dashes": ("Sub\n---\n\nBody", "Sub.\n\nBody"),
            "rule after blank": ("Para\n\n---\n\nNext", "Para\n\nNext"),
            "references and footnotes": ("[text][ref] and [t2][] x\n\n[ref]: http://u \"title\"\n[^1]: note\n\nmore[^1] end.",
                                         "text and t2 x\n\nmore end."),
            "html blocks": ("Before <!-- hidden\nmore --> after.\n\n<script>\nvar x = 1;\n</script>\n\n"
                            "<style>p{}</style>\n\n<div\n class=\"c\">Shown</div>", "Before after.\n\nShown"),
            "fence in quote": ("> ```\n> secret\n> ```\n\nafter", "after"),
            "table without pipes": ("Intro.\n\na | b\n--|--\n1 | 2\n3 | 4\n\nAfter.", "Intro.\n\nAfter."),
            "aligned table": ("a | b\n:---|---:\n1 | 2\n\nAfter.", "After."),
            "link with parens": ("[wiki](https://x/Foo_(bar)) here", "wiki here"),
            "image with parens": ("![p](https://x/Foo_(bar).png) here", "here"),
        }
        for label, (md, want) in cases.items():
            with self.subTest(label):
                self.assertEqual(narrate.speech_text(md, "a.md"), want)

    def test_a_heading_with_punctuation_gets_no_extra_period_and_text_is_used_as_is(self):  # R4
        self.assertEqual(narrate.speech_text("# Why?\n\nBody", "a.md"), "Why?\n\nBody")
        raw = "# not *markdown*\n  spaced"
        self.assertEqual(narrate.speech_text(raw, "a.txt"), raw)

    def test_chunks_pack_sentences_up_to_the_limit_and_keep_paragraphs_apart(self):  # R6
        self.assertEqual(narrate.chunks("One. Two. Three.", 400), ["One. Two. Three."])
        self.assertEqual(narrate.chunks("One.\n\nTwo.", 400), ["One.", "Two."])
        out = narrate.chunks(" ".join(sentence(c, 150) for c in "abc"), 400)
        self.assertEqual([len(c) for c in out], [303, 151])

    def test_long_sentences_split_at_spaces_and_long_words_are_hard_split(self):  # R6
        words = " ".join(["word"] * 300)
        out = narrate.chunks(words, 400)
        self.assertTrue(all(len(c) <= 400 for c in out) and len(out) > 1)
        self.assertEqual(" ".join(out), words)
        out = narrate.chunks("x" * 1000, 400)
        self.assertEqual([len(c) for c in out], [400, 400, 200])


class Narrate(Base):
    def test_three_chunks_are_sent_in_order_and_joined_into_one_wav(self):  # R6 R11 R2
        data = [pcm(2400, 1), pcm(4800, 2), pcm(2400, 3)]
        s = self.server([{"json": ok(d)} for d in data])
        text = " ".join(sentence(c) for c in "abc")
        _, answer, notes = self.narrate(text, s)
        self.assertEqual(s.texts(), [sentence(c) for c in "abc"])
        self.assertEqual(s.requests[0]["path"], "/speak")
        (name,) = self.files()
        self.assertRegex(name, r"^narrate-\d{8}-\d{6}\.wav$")
        self.assertEqual(answer["result"], {"exit_code": 0, "outputs": {"files": [str(self.dir / name)],
                                                                        "seconds": 0.4}})
        with wave.open(str(self.dir / name)) as w:
            self.assertEqual((w.getframerate(), w.getnchannels(), w.getsampwidth()), (24000, 1, 2))
            self.assertEqual(w.getnframes(), 9600)
            self.assertEqual(w.readframes(9600), b"".join(data))
        self.assertIn(f"{name} (0:00)", self.said(notes, "info"))

    def test_the_request_has_json_content_type_voice_speed_and_model(self):  # R7 R8 server contract
        s, _, _ = self.narrate("Hello there.")
        req = s.requests[0]
        self.assertEqual(req["headers"]["Content-Type"], "application/json")
        self.assertEqual(req["body"], {"text": "Hello there.", "voice": "af_heart", "speed": 1.0,
                                       "model": "kokoro"})
        s, answer, _ = self.narrate("Hi.", voice="am_fenrir", speed="1.5", output="b.wav")
        self.assertEqual((s.requests[0]["body"]["voice"], s.requests[0]["body"]["speed"]), ("am_fenrir", 1.5))

    def test_a_2000_character_sentence_without_punctuation_is_split_at_spaces(self):  # R6
        text = " ".join(["abcdefgh"] * 222)  # 1997 characters
        s, answer, _ = self.narrate(text)
        self.assertEqual(answer["result"]["exit_code"], 0)
        self.assertGreater(len(s.texts()), 4)
        self.assertTrue(all(len(t) <= 400 for t in s.texts()))
        self.assertEqual(" ".join(s.texts()), text)

    def test_markdown_is_reduced_before_it_is_sent(self):  # R4
        md = ("# Heading\n\nSome **bold** words, a [link](http://secret.example/x) and ![img](http://i/x.png).\n\n"
              "```\nprint('code')\n```\n\n| a | b |\n|---|---|\n| 1 | 2 |\n")
        s, answer, _ = self.narrate(md)
        sent = " ".join(s.texts())
        for want in ("Heading", "bold", "link"):
            self.assertIn(want, sent)
        for bad in ("*", "http", "code", "|", "img"):
            self.assertNotIn(bad, sent)

    def test_bad_voice_and_speed_are_refused_with_the_choices(self):  # R7 R8
        s = self.server()
        cases = {"voice": ({"voice": "zz"}, "af_bella"), "low": ({"speed": "0.4"}, "0.5"),
                 "high": ({"speed": "2.1"}, "2.0"), "text": ({"speed": "fast"}, "speed"),
                 "nan": ({"speed": "nan"}, "speed")}
        for label, (extra, hint) in cases.items():
            with self.subTest(label):
                _, answer, notes = self.narrate("Hi.", s, **extra)
                self.assertRefused(answer, notes, s, hint)

    def test_output_names_are_checked_and_an_existing_file_is_never_replaced(self):  # R9
        (self.dir / "taken.wav").write_bytes(b"old")
        s = self.server()
        for name in ("sub/a.wav", "a.mp3", "../a.wav", "a\\b.wav", "a\nb.wav", "x" * 240 + ".wav", "taken.wav", "..", "."):
            with self.subTest(name):
                _, answer, notes = self.narrate("Hi.", s, output=name)
                self.assertEqual(answer["result"], {"exit_code": 1})
                self.assertTrue(self.said(notes))
        self.assertEqual(s.requests, [])
        self.assertEqual(self.files(), ["taken.wav"])
        self.assertEqual((self.dir / "taken.wav").read_bytes(), b"old")

    def test_the_output_name_is_used_and_dots_inside_it_are_fine(self):  # R9
        _, answer, _ = self.narrate("Hi.", output="mine.wav")
        self.assertEqual(answer["result"]["outputs"]["files"], [str(self.dir / "mine.wav")])
        _, answer, _ = self.narrate("Hi.", output="a..wav")
        self.assertEqual(answer["result"]["outputs"]["files"], [str(self.dir / "a..wav")])

    def test_files_outside_the_in_folder_are_refused_before_any_request(self):  # R3 NFR1
        (self.outside / "x.md").write_text("secret")
        (self.inn / "link.md").symlink_to(self.outside / "x.md")
        (self.inn / "dirlink").symlink_to(self.outside)
        s = self.server()
        for label, name in {"parent": "../outside/x.md", "absolute": str(self.outside / "x.md"),
                            "symlink": "link.md", "symlinked dir": "dirlink/x.md"}.items():
            with self.subTest(label):
                answer, notes = self.invoke(s.url, {"file": name})
                self.assertRefused(answer, notes, s)
                self.assertNotIn("secret", json.dumps(notes))

    def test_an_absolute_path_inside_the_in_folder_is_read(self):  # R2
        self.doc("Inside.")
        s = self.server()
        answer, _ = self.invoke(s.url, {"file": str(self.inn / "a.md")})
        self.assertEqual(answer["result"]["exit_code"], 0)
        self.assertEqual(s.texts(), ["Inside."])

    def test_a_missing_in_folder_says_narrate_needs_it(self):  # R3
        s = self.server()
        answer, notes = self.invoke(s.url, {"file": "a.md"}, in_dir=None)
        self.assertRefused(answer, notes, s, "in folder")

    def test_bad_files_are_refused_before_any_request(self):  # R2
        (self.inn / "a.pdf").write_text("x")
        (self.inn / "bad.txt").write_bytes(b"\xff\xfe bad")
        (self.inn / "big.txt").write_text("a" * 1_000_001)
        (self.inn / "dir.md").mkdir()
        s = self.server()
        for name in ("a.pdf", "bad.txt", "big.txt", "dir.md", "missing.md", "a\0.md"):
            with self.subTest(name):
                answer, notes = self.invoke(s.url, {"file": name})
                self.assertRefused(answer, notes, s)

    def test_extensions_are_case_insensitive_and_a_bom_is_dropped(self):  # R2
        (self.inn / "A.TXT").write_bytes(b"\xef\xbb\xbfHello.\r\nWorld.")
        s = self.server()
        answer, _ = self.invoke(s.url, {"file": "A.TXT"})
        self.assertEqual(answer["result"]["exit_code"], 0)
        self.assertEqual(s.texts(), ["Hello. World."])

    def test_empty_and_oversized_speech_text_is_refused(self):  # R5
        s = self.server()
        _, answer, notes = self.narrate("```\ncode only\n```\n", s)
        self.assertRefused(answer, notes, s)
        _, answer, notes = self.narrate("a" * 20_001, s, output="big.wav")
        self.assertEqual(answer["result"], {"exit_code": 1})
        self.assertIn("20000", self.said(notes))
        self.assertEqual(s.requests, [])

    def test_a_fifo_in_the_in_folder_is_refused_without_hanging(self):  # R2
        os.mkfifo(self.inn / "pipe.txt")
        s = self.server()
        p = self.begin(s.url, {"file": "pipe.txt"})
        timer = threading.Timer(15, p.kill)
        timer.start()
        self.send(p, id=3, method="shutdown", params={})
        try:
            answer, notes = self.collect(p)
        finally:
            timer.cancel()
        self.assertRefused(answer, notes, s)

    def test_a_symlink_to_another_file_inside_the_in_folder_is_read(self):  # R3
        self.doc("Linked words.", "real.txt")
        (self.inn / "alias.txt").symlink_to(self.inn / "real.txt")
        s = self.server()
        answer, _ = self.invoke(s.url, {"file": "alias.txt"})
        self.assertEqual(answer["result"]["exit_code"], 0)
        self.assertEqual(s.texts(), ["Linked words."])

    def test_text_without_a_letter_or_digit_is_refused_and_a_lone_cr_is_a_line_break(self):  # R5
        s = self.server()
        _, answer, notes = self.narrate("...", s)
        self.assertRefused(answer, notes, s, "nothing to read aloud")
        (self.inn / "cr.md").write_bytes(b"# Title\rBody text.")
        answer, _ = self.invoke(s.url, {"file": "cr.md"})
        self.assertEqual(answer["result"]["exit_code"], 0)
        self.assertEqual(s.texts(), ["Title.", "Body text."])

    def test_an_oversized_server_answer_is_refused_not_truncated(self):  # NFR2
        s = self.server([{"raw": b"x" * 8_000_001}])
        _, answer, notes = self.narrate("Hi.", s)
        self.assertRefused(answer, notes, None, "too large")

    def test_a_stopped_call_never_sends_a_request(self):  # R13
        s = self.server()
        body = {"text": "Hi.", "voice": "af_heart", "speed": 1.0, "model": "kokoro"}
        call = narrate.Call()
        call.stop()
        with self.assertRaises(narrate.Cancelled):
            narrate.speak(s.url, body, call)
        narrate.cancelled.set()
        try:
            with self.assertRaises(narrate.Cancelled):
                narrate.speak(s.url, body, narrate.Call())
        finally:
            narrate.cancelled.clear()
        self.assertEqual(s.requests, [])

    def test_a_stop_while_a_large_body_is_being_sent_ends_the_call_fast(self):  # R13
        # A server that accepts but never reads: the send blocks once the socket buffers fill.
        listener = socket.create_server(("127.0.0.1", 0))
        self.addCleanup(listener.close)
        accepted = []
        threading.Thread(target=lambda: accepted.append(listener.accept()[0]), daemon=True).start()
        self.addCleanup(lambda: [c.close() for c in accepted])
        url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        body = {"text": "a" * 20_000_000, "voice": "af_heart", "speed": 1.0, "model": "kokoro"}
        call, errors = narrate.Call(), []

        def run():
            try:
                narrate.speak(url, body, call)
            except Exception as e:
                errors.append(e)

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        deadline = time.monotonic() + 5
        while not accepted and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.3)  # let the send fill the buffers and block
        stopper = threading.Thread(target=call.stop, daemon=True)
        began = time.monotonic()
        stopper.start()
        stopper.join(2)
        worker.join(2)
        self.assertFalse(stopper.is_alive(), "stop() blocked behind the send")
        self.assertFalse(worker.is_alive(), "the send was not interrupted")
        self.assertLess(time.monotonic() - began, 2)
        self.assertEqual([type(e) for e in errors], [narrate.Cancelled])

    def test_a_cancel_just_before_publishing_leaves_no_wav_or_part(self):  # R13
        wav = narrate.Wav(str(self.dir / "x.wav"))
        wav.add(pcm(10))

        def cancel():
            raise narrate.Cancelled()

        try:
            with self.assertRaises(narrate.Cancelled):
                wav.publish(cancel)
        finally:
            wav.discard()
        self.assertEqual(self.files(), [])

    def test_progress_runs_from_unknown_to_done(self):  # R10
        s = self.server()
        _, _, notes = self.narrate(" ".join(sentence(c) for c in "abcd"), s)
        bars = [n["params"] for n in notes if n["method"] == "bar"]
        self.assertEqual([b["percent"] for b in bars], [None, 25, 50, 75, 100])
        self.assertTrue(all(b["id"] == "narrate" and b["label"] == "narrating" for b in bars))
        self.assertEqual([b.get("done") for b in bars], [None] * 4 + [True])

    def test_wrong_input_types_are_jsonrpc_errors(self):  # R12
        s = self.server()
        for label, inputs in {"no file": {}, "file": {"file": 5}, "voice": {"file": "a.md", "voice": 3},
                              "speed": {"file": "a.md", "speed": 1.5}, "output": {"file": "a.md", "output": 7}}.items():
            with self.subTest(label):
                self.doc("Hi.")
                answer, _ = self.invoke(s.url, inputs)
                self.assertEqual(answer["error"]["code"], -32602)
        self.assertEqual(s.requests, [])

    def test_server_failures_are_shown_and_leave_no_file(self):  # R12 NFR2
        dead = self.server()
        dead_url = dead.url
        dead.close()
        cases = {
            "error body": (self.server([{"status": 400, "json": {"error": "boom"}}]).url, "boom"),
            "busy": (self.server([{"status": 503, "json": {"error": "busy: waiting"}}]).url, "busy"),
            "http 500 no body": (self.server([{"status": 500, "raw": b"<html>"}]).url, "500"),
            "broken json": (self.server([{"raw": b"{nope"}]).url, "broken"),
            "wrong rate": (self.server([{"json": ok(pcm(10), 22050)}]).url, "22050"),
            "bad base64": (self.server([{"json": {"audio": "!!!", "sample_rate": 24000}}]).url, "audio"),
            "odd length": (self.server([{"json": ok(b"abc")}]).url, "audio"),
            "unreachable": (dead_url, "DOCK_NARRATE_URL"),
        }
        for label, (url, hint) in cases.items():
            with self.subTest(label):
                self.doc("Hi.")
                answer, notes = self.invoke(url, {"file": "a.md"})
                self.assertRefused(answer, notes, None, hint)
        self.assertIn("plugins/narrate/server/README.md", self.said(notes))

    def test_a_failure_on_the_second_chunk_leaves_no_wav_or_part(self):  # NFR2
        s = self.server([{"json": ok(pcm(10))}, {"status": 500, "json": {"error": "model died"}}])
        _, answer, notes = self.narrate(sentence("a") + " " + sentence("b") + " " + sentence("c"), s)
        self.assertRefused(answer, notes, None, "model died")
        self.assertEqual(len(s.requests), 2)

    def test_another_model_is_refused(self):  # R14
        s = self.server()
        p = self.begin(s.url, {"file": self.doc("Hi.")}, model="other")
        self.send(p, id=3, method="shutdown", params={})
        answer, notes = self.collect(p)
        self.assertRefused(answer, notes, s)

    def test_cancel_after_the_first_chunk_stops_and_leaves_nothing(self):  # R13
        s = self.server([{"json": ok(pcm(10))}, {"delay": 3}, {}])
        p = self.begin(s.url, {"file": self.doc(" ".join(sentence(c) for c in "abc"))})
        self.wait_for_requests(s, 2)
        self.send(p, method="cancel", params={})
        self.send(p, id=3, method="shutdown", params={})
        answer, _ = self.collect(p)
        self.assertEqual(answer["error"]["code"], -32800)
        time.sleep(0.2)
        self.assertEqual(len(s.requests), 2)
        self.assertEqual(self.files(), [])

    def test_closing_stdin_mid_run_stops_the_plugin_and_leaves_nothing(self):  # R13
        s = self.server([{"delay": 3}])
        p = self.begin(s.url, {"file": self.doc("Hi.")})
        self.wait_for_requests(s, 1)
        p.stdin.close()
        p.communicate(timeout=10)
        self.assertEqual(self.files(), [])

    def test_a_cancel_with_nothing_running_is_ignored(self):  # R13
        s = self.server()
        self.doc("Hi.")
        p = self.start(s.url)
        self.send(p, method="cancel", params={})
        self.send(p, id=2, method="invoke", params={"command": "narrate", "cwd": str(self.dir),
                                                    "in_dir": str(self.inn), "inputs": {"file": "a.md"}})
        self.send(p, id=3, method="shutdown", params={})
        answer, _ = self.collect(p)
        self.assertEqual(answer["result"]["exit_code"], 0)

    def test_credentials_in_the_url_are_never_shown(self):  # shown()
        dead = self.server()
        port = dead.server.server_port
        dead.close()
        self.doc("Hi.")
        answer, notes = self.invoke(f"http://user:s3cret@127.0.0.1:{port}", {"file": "a.md"})
        self.assertNotIn("s3cret", json.dumps([answer, notes]))

    def test_unknown_command_is_32601(self):  # protocol
        p = self.start(self.server().url)
        self.send(p, id=2, method="invoke", params={"command": "nope", "cwd": str(self.dir)})
        self.send(p, id=3, method="shutdown", params={})
        answer, _ = self.collect(p)
        self.assertEqual(answer["error"]["code"], -32601)


if __name__ == "__main__":
    unittest.main()
