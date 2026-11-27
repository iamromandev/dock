"""The story plugin driven over its stdio protocol, against a local stand-in for Ollama."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
STORY = PLUGIN / "main.py"

TEXT = ["TITLE: The Brave Fox\n\n", "Once upon a time ", "there lived a fox.\n\n", "The end."]
BODY = "Once upon a time there lived a fox.\n\nThe end."


def chunk(text, done=False):
    return {"message": {"role": "assistant", "content": text}, "done": done}


def stream(texts=TEXT):
    return [chunk(t) for t in texts] + [chunk("", True)]


class Server:
    """A local HTTP server answering POST /api/chat with `status` and NDJSON `lines` (a delay after each)."""

    def __init__(self, lines=(), status=200, delay=0.0, raw=None):
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                size = int(self.headers.get("Content-Length", 0))
                outer.requests.append({"path": self.path, "body": json.loads(self.rfile.read(size))})
                try:
                    self.send_response(status)
                    self.send_header("Connection", "close")
                    self.end_headers()
                    for line in lines:
                        data = line.encode() if isinstance(line, str) else json.dumps(line).encode()
                        self.wfile.write(data + b"\n")
                        self.wfile.flush()
                        time.sleep(delay)
                except OSError:
                    pass

            def log_message(self, format, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Story(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="dock-story-")).resolve()
        self.servers: list[Server] = []

    def tearDown(self):
        for s in self.servers:
            s.close()

    def server(self, *a, **kw) -> Server:
        s = Server(*a, **kw)
        self.servers.append(s)
        return s

    def start(self, url, model=None, timeout=None):
        env = {**os.environ, "DOCK_STORY_URL": url}
        env.pop("DOCK_STORY_MODEL", None)
        if timeout:
            env["DOCK_STORY_TIMEOUT"] = timeout
        if model:
            env["DOCK_STORY_MODEL"] = model
        return subprocess.Popen([sys.executable, str(STORY)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env=env)

    @staticmethod
    def send(p, **msg):
        p.stdin.write(json.dumps({"jsonrpc": "2.0", **msg}) + "\n")
        p.stdin.flush()

    def begin(self, url, inputs=None):
        p = self.start(url)
        inputs = {"prompt": "a brave fox"} if inputs is None else inputs
        self.send(p, id=1, method="initialize", params={})
        self.send(p, id=2, method="invoke", params={"command": "write", "args": [], "cwd": str(self.dir),
                                                    "inputs": inputs})
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

    def invoke(self, url, inputs=None):
        """Returns (the invoke answer, the notifications seen before it)."""
        p = self.begin(url, inputs)
        self.send(p, id=3, method="shutdown", params={})
        return self.collect(p)

    def said(self, notes, level="error"):
        return " ".join(n["params"]["message"] for n in notes if n["method"] == "log" and n["params"]["level"] == level)

    def files(self):
        return sorted(p.name for p in self.dir.iterdir())

    def system_prompt(self, s):
        return s.requests[0]["body"]["messages"][0]["content"]

    def test_a_story_is_saved_as_markdown_and_reported(self):  # AC1
        s = self.server(stream())
        answer, notes = self.invoke(s.url)
        files = self.files()
        self.assertEqual(len(files), 1)
        self.assertRegex(files[0], r"^story-\d{8}-\d{6}\.md$")
        words = len(BODY.split())
        self.assertEqual(answer["result"], {"exit_code": 0, "outputs": {"files": [str(self.dir / files[0])],
                                                                        "words": words}})
        self.assertEqual((self.dir / files[0]).read_text(encoding="utf-8"), f"# The Brave Fox\n\n{BODY}\n")
        self.assertIn(f"saved {files[0]} ({words} words)", self.said(notes, "info"))
        req = s.requests[0]
        self.assertEqual(req["path"], "/api/chat")
        self.assertEqual(req["body"]["model"], "qwen2.5:7b")
        self.assertIs(req["body"]["stream"], True)
        self.assertEqual(req["body"]["options"], {"num_predict": 1200})
        self.assertEqual([m["role"] for m in req["body"]["messages"]], ["system", "user"])
        self.assertIn("a brave fox", req["body"]["messages"][1]["content"])

    def test_bad_inputs_are_refused_and_nothing_is_created(self):  # AC2
        (self.dir / "taken.md").write_text("old")
        s = self.server(stream())
        for label, inputs in {"empty prompt": {"prompt": "  "}, "missing prompt": {}, "prompt not a string": {"prompt": 5},
                              "too long": {"prompt": "x" * 2001},
                              "audience not a string": {"prompt": "x", "audience": 3}}.items():
            with self.subTest(label):
                answer, _ = self.invoke(s.url, inputs)
                self.assertEqual(answer["error"]["code"], -32602)
        for label, extra in {"audience": {"audience": "teen"}, "length": {"length": "huge"}}.items():
            with self.subTest(label):
                answer, notes = self.invoke(s.url, {"prompt": "x", **extra})
                self.assertEqual(answer["result"], {"exit_code": 1})
                self.assertTrue(self.said(notes))
        self.assertEqual(s.requests, [])
        self.assertEqual(self.files(), ["taken.md"])

    def test_a_prompt_of_2000_characters_is_accepted(self):  # AC2 boundary
        s = self.server(stream())
        answer, _ = self.invoke(s.url, {"prompt": "x" * 2000})
        self.assertEqual(answer["result"]["exit_code"], 0)

    def test_without_a_title_the_first_six_words_of_the_prompt_are_used(self):  # AC3
        s = self.server(stream(["Once upon a time.\n\nMore."]))
        answer, _ = self.invoke(s.url, {"prompt": "a little dragon who learns to share toys today"})
        path = Path(answer["result"]["outputs"]["files"][0])
        self.assertEqual(path.read_text(encoding="utf-8"),
                         "# A Little Dragon Who Learns To\n\nOnce upon a time.\n\nMore.\n")

    def test_a_leading_hash_line_is_the_title(self):  # AC3
        s = self.server(stream(["# Moon Tale\n\nHello there."]))
        answer, _ = self.invoke(s.url)
        path = Path(answer["result"]["outputs"]["files"][0])
        self.assertEqual(path.read_text(encoding="utf-8"), "# Moon Tale\n\nHello there.\n")

    def test_the_system_prompt_has_the_audience_rules_and_the_word_target(self):  # AC4
        rules = {"kids": "no violence", "adult": "no explicit sexual content"}
        targets = {"short": "300", "medium": "800", "long": "1500"}
        for audience, rule in rules.items():
            for length, target in targets.items():
                with self.subTest(audience=audience, length=length):
                    s = self.server(stream())
                    self.invoke(s.url, {"prompt": "x", "audience": audience, "length": length,
                                        "output": f"{audience}-{length}.md"})
                    system = self.system_prompt(s)
                    self.assertIn(rule, system.lower())
                    self.assertIn(target, system)
                    self.assertIn("TITLE:", system)
        s = self.server(stream())
        self.invoke(s.url, {"prompt": "x", "length": "long"})
        self.assertEqual(s.requests[0]["body"]["options"], {"num_predict": 6000})
        self.assertIn("no violence", self.system_prompt(s).lower())  # kids is the default

    def test_a_bad_or_taken_output_name_is_refused_and_never_replaced(self):  # AC5
        (self.dir / "taken.md").write_text("old")
        for name in ("../x.md", "a/b.md", "a\\b.md", "x.txt", "a\nb.md", "taken.md"):
            with self.subTest(name):
                s = self.server(stream())
                answer, notes = self.invoke(s.url, {"prompt": "x", "output": name})
                self.assertEqual(answer["result"], {"exit_code": 1})
                self.assertTrue(self.said(notes))
                self.assertEqual(s.requests, [])
        self.assertEqual(self.files(), ["taken.md"])
        self.assertEqual((self.dir / "taken.md").read_text(), "old")

    def test_the_output_name_is_used(self):  # AC5
        s = self.server(stream())
        answer, _ = self.invoke(s.url, {"prompt": "x", "output": "fox.md"})
        self.assertEqual(answer["result"]["outputs"]["files"], [str(self.dir / "fox.md")])

    def test_progress_starts_unknown_and_ends_done(self):  # AC6
        s = self.server(stream())
        _, notes = self.invoke(s.url)
        bars = [n["params"] for n in notes if n["method"] == "bar"]
        self.assertEqual(bars[0], {"id": "story", "label": "writing", "percent": None})
        self.assertEqual(bars[-1], {"id": "story", "label": "writing", "percent": 100, "done": True})
        middle = [b["percent"] for b in bars[1:-1]]
        self.assertTrue(middle and all(0 <= p <= 99 for p in middle), bars)

    def test_cancel_mid_stream_ends_with_32800_and_leaves_no_file(self):  # AC7
        s = self.server(stream(), delay=1)
        p = self.begin(s.url)
        for _ in range(100):
            if s.requests:
                break
            time.sleep(0.05)
        self.send(p, method="cancel", params={})
        self.send(p, id=3, method="shutdown", params={})
        answer, _ = self.collect(p)
        self.assertEqual(answer["error"]["code"], -32800)
        self.assertEqual(self.files(), [])

    def test_ollama_failures_are_reported_with_the_url_and_exit_1(self):  # AC8
        dead = self.server()
        dead_url = dead.url
        dead.close()
        cases = {
            "unreachable": (dead_url, ""),
            "http 500": (self.server([], status=500).url, ""),
            "model missing": (self.server([], status=404).url, "ollama pull qwen2.5:7b"),
            "broken line": (self.server([chunk("a"), "{not json"]).url, ""),
            "error line": (self.server([{"error": "boom"}]).url, "boom"),
            "no done": (self.server([chunk("TITLE: T\n\nText")]).url, ""),
            "empty story": (self.server([chunk("  ", True)]).url, ""),
        }
        for label, (url, extra) in cases.items():
            with self.subTest(label):
                answer, notes = self.invoke(url)
                self.assertEqual(answer["result"], {"exit_code": 1})
                said = self.said(notes)
                self.assertTrue(said)
                if label != "empty story":
                    self.assertIn(url, said)
                self.assertIn(extra, said)
                self.assertEqual(self.files(), [])

    def test_bars_are_throttled_for_a_fast_stream(self):  # finding 2
        s = self.server(stream(["TITLE: T\n\n"] + ["word " for _ in range(300)]))
        _, notes = self.invoke(s.url)
        bars = [n for n in notes if n["method"] == "bar"]
        self.assertLessEqual(len(bars), 8, len(bars))
        self.assertEqual(bars[-1]["params"]["percent"], 100)

    def test_cancel_sent_right_after_the_invoke_still_cancels_it(self):  # finding 3
        s = self.server(stream(), delay=1)
        p = self.begin(s.url)
        self.send(p, method="cancel", params={})  # no wait: the invoke may not have started yet
        self.send(p, id=3, method="shutdown", params={})
        answer, _ = self.collect(p)
        self.assertEqual(answer["error"]["code"], -32800)
        self.assertEqual(self.files(), [])

    def test_a_cancel_with_nothing_running_is_ignored(self):  # finding 3 guard
        s = self.server(stream())
        p = self.start(s.url)
        self.send(p, method="cancel", params={})
        self.send(p, id=2, method="invoke", params={"command": "write", "cwd": str(self.dir),
                                                    "inputs": {"prompt": "x"}})
        self.send(p, id=3, method="shutdown", params={})
        answer, _ = self.collect(p)
        self.assertEqual(answer["result"]["exit_code"], 0)

    def test_a_too_long_output_name_is_refused(self):  # finding 4
        s = self.server(stream())
        answer, notes = self.invoke(s.url, {"prompt": "x", "output": "a" * 238 + ".md"})
        self.assertEqual(answer["result"], {"exit_code": 1})
        self.assertTrue(self.said(notes))
        self.assertEqual(s.requests, [])

    def test_a_story_outside_the_word_range_is_saved_with_a_warning(self):  # finding 5
        short = self.server(stream(["TITLE: T\n\nOnly five words here ok."]))
        answer, notes = self.invoke(short.url)
        self.assertEqual(answer["result"]["exit_code"], 0)
        self.assertIn("outside", self.said(notes, "warning"))
        self.assertEqual(len(self.files()), 1)
        long = self.server(stream(["TITLE: T\n\n" + "word " * 700]))
        answer, notes = self.invoke(long.url, {"prompt": "x", "output": "long.md"})
        self.assertEqual(answer["result"]["exit_code"], 0)
        self.assertIn("outside", self.said(notes, "warning"))

    def test_a_stalled_server_times_out_with_exit_1(self):
        s = self.server([chunk("TITLE: T\n\nHi")], delay=30)
        p = self.start(s.url, timeout="1")
        self.send(p, id=2, method="invoke", params={"command": "write", "cwd": str(self.dir),
                                                    "inputs": {"prompt": "x"}})
        self.send(p, id=3, method="shutdown", params={})
        answer, notes = self.collect(p)
        self.assertEqual(answer["result"], {"exit_code": 1})
        self.assertTrue(self.said(notes))
        self.assertEqual(self.files(), [])

    def test_a_cut_off_story_gets_a_warning(self):  # finding 6
        lines = [chunk("TITLE: T\n\n" + "word " * 300), {**chunk("", True), "done_reason": "length"}]
        s = self.server(lines)
        answer, notes = self.invoke(s.url)
        self.assertEqual(answer["result"]["exit_code"], 0)
        self.assertIn("cut off", self.said(notes, "warning"))

    def test_credentials_in_the_url_are_never_shown(self):  # finding 7
        dead = self.server()
        port = dead.server.server_port
        dead.close()
        p = self.start(f"http://user:s3cret@127.0.0.1:{port}")
        self.send(p, id=2, method="invoke", params={"command": "write", "cwd": str(self.dir),
                                                    "inputs": {"prompt": "x"}})
        self.send(p, id=3, method="shutdown", params={})
        answer, notes = self.collect(p)
        self.assertNotIn("s3cret", json.dumps([answer, notes]))
        self.assertIn(f"http://127.0.0.1:{port}", self.said(notes))

    def test_closing_stdin_mid_stream_stops_the_plugin_and_leaves_no_file(self):  # finding 9
        s = self.server(stream(), delay=1)
        p = self.begin(s.url)
        for _ in range(100):
            if s.requests:
                break
            time.sleep(0.05)
        p.stdin.close()
        p.communicate(timeout=10)
        self.assertEqual(self.files(), [])

    def test_the_manifest_asks_for_exactly_network_and_cwd_write(self):  # NFR2
        manifest = tomllib.loads((PLUGIN / "plugin.toml").read_text(encoding="utf-8"))
        self.assertEqual(manifest["capabilities"]["permissions"], ["network", "fs:write:cwd"])
        self.assertEqual(manifest["id"], "dev.dock.story")


if __name__ == "__main__":
    unittest.main()
