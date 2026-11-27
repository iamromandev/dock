"""The image plugin driven over its stdio protocol, against a local stand-in for the host image server."""

import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

IMAGE = Path(__file__).resolve().parent.parent / "main.py"
PNG = b"\x89PNG\r\n\x1a\n" + b"fake image bytes"


def png_line(seed=7, data=PNG) -> dict:
    return {"png": base64.b64encode(data).decode(), "seed": seed}


STEPS = [{"step": n, "steps": 4} for n in (1, 2, 3, 4)]


class Server:
    """A local HTTP server answering POST /generate with `status` and NDJSON `lines` (a delay after each line)."""

    def __init__(self, lines=(), status=200, delay=0.0, lead=0.0, hold=False):
        """`lead` waits before answering; `hold` keeps the connection open after the lines until the client closes it."""
        self.requests: list[dict] = []
        self.closed = threading.Event()  # the client closed a held connection
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                size = int(self.headers.get("Content-Length", 0))
                outer.requests.append({"path": self.path, "body": json.loads(self.rfile.read(size))})
                try:
                    time.sleep(lead)
                    self.send_response(status)
                    self.send_header("Connection", "close")
                    self.end_headers()
                    for line in lines:
                        self.wfile.write(json.dumps(line).encode() + b"\n")
                        self.wfile.flush()
                        time.sleep(delay)
                    if hold:
                        self.connection.settimeout(10)
                        try:
                            if self.connection.recv(1) == b"":
                                outer.closed.set()
                        except TimeoutError:
                            pass
                except OSError:
                    outer.closed.set()

            def log_message(self, format, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Image(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="dock-image-")).resolve()
        self.servers: list[Server] = []

    def tearDown(self):
        for s in self.servers:
            s.close()

    def server(self, *a, **kw) -> Server:
        s = Server(*a, **kw)
        self.servers.append(s)
        return s

    def start(self, url, model_env=None):
        env = {**os.environ, "DOCK_IMAGE_URL": url}
        env.pop("DOCK_IMAGE_MODEL", None)
        if model_env:
            env["DOCK_IMAGE_MODEL"] = model_env
        return subprocess.Popen([sys.executable, str(IMAGE)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env=env)

    @staticmethod
    def send(p, **msg):
        p.stdin.write(json.dumps({"jsonrpc": "2.0", **msg}) + "\n")
        p.stdin.flush()

    def invoke(self, url, inputs=None, model_env=None):
        """Returns (the invoke answer, the notifications seen before it)."""
        p = self.start(url, model_env)
        inputs = {"prompt": "a cat"} if inputs is None else inputs
        self.send(p, id=1, method="initialize", params={})
        self.send(p, id=2, method="invoke", params={"command": "image", "args": [], "cwd": str(self.dir),
                                                    "inputs": inputs})
        self.send(p, id=3, method="shutdown", params={})
        out, _ = p.communicate(timeout=30)
        msgs = [json.loads(x) for x in out.splitlines() if x.startswith("{")]
        return next(m for m in msgs if m.get("id") == 2), [m for m in msgs if "method" in m]

    def said(self, notes, level="error"):
        return " ".join(n["params"]["message"] for n in notes if n["method"] == "log" and n["params"]["level"] == level)

    def files(self):
        return sorted(p.name for p in self.dir.iterdir())

    def test_default_request_writes_a_png_and_returns_its_path(self):
        s = self.server(STEPS + [png_line()])
        answer, notes = self.invoke(s.url)
        files = self.files()
        self.assertEqual(len(files), 1)
        self.assertRegex(files[0], r"^image-\d{8}-\d{6}\.png$")
        self.assertEqual(answer["result"], {"exit_code": 0, "outputs": {"files": [str(self.dir / files[0])]}})
        self.assertEqual((self.dir / files[0]).read_bytes(), PNG)
        self.assertEqual(s.requests[0]["path"], "/generate")
        self.assertEqual(s.requests[0]["body"], {"prompt": "a cat", "width": 512, "height": 512,
                                                 "steps": None, "seed": None, "model": "z-image-turbo"})
        self.assertIn("seed 7", self.said(notes, "info"))

    def test_explicit_values_are_sent_and_output_names_the_file(self):
        s = self.server(STEPS + [png_line()])
        inputs = {"prompt": "a dog", "output": "cat.png", "width": 512, "height": 768, "steps": 8, "seed": 5,
                  "model": "z-image-turbo"}
        answer, _ = self.invoke(s.url, inputs)
        self.assertEqual(answer["result"]["outputs"], {"files": [str(self.dir / "cat.png")]})
        self.assertEqual(s.requests[0]["body"], {"prompt": "a dog", "width": 512, "height": 768, "steps": 8,
                                                 "seed": 5, "model": "z-image-turbo"})

    def test_sizes_are_rounded_to_16_and_clamped(self):
        cases = {(1920, 1080): (1920, 1088), (1000, 1010): (1008, 1008), (100, 4096): (256, 2048)}
        for (w, h), want in cases.items():
            with self.subTest(f"{w}x{h}"):
                s = self.server(STEPS + [png_line()])
                answer, _ = self.invoke(s.url, {"prompt": "x", "output": f"{w}x{h}.png", "width": w, "height": h})
                self.assertEqual(answer["result"]["exit_code"], 0)
                self.assertEqual((s.requests[0]["body"]["width"], s.requests[0]["body"]["height"]), want)

    def test_environment_model_must_be_z_image_turbo(self):
        s = self.server(STEPS + [png_line()])
        self.invoke(s.url, model_env="z-image-turbo")
        self.assertEqual(s.requests[0]["body"]["model"], "z-image-turbo")
        answer, notes = self.invoke(s.url, model_env="dev")
        self.assertEqual(answer["result"], {"exit_code": 1})
        self.assertTrue(self.said(notes))

    def test_progress_bar_starts_unknown_then_follows_steps_and_finishes(self):
        s = self.server(STEPS + [png_line()])
        _, notes = self.invoke(s.url)
        bars = [n["params"] for n in notes if n["method"] == "bar"]
        self.assertEqual([b["percent"] for b in bars], [None, 25, 50, 75, 100, 100])
        self.assertEqual({b["id"] for b in bars}, {"gen"})
        self.assertTrue(bars[-1]["done"])
        self.assertFalse(any(b.get("done") for b in bars[:-1]))

    def test_cancel_ends_with_32800_and_leaves_no_file(self):
        s = self.server(STEPS + [png_line()], delay=1)
        p = self.start(s.url)
        self.send(p, id=1, method="initialize", params={})
        self.send(p, id=2, method="invoke", params={"command": "image", "args": [], "cwd": str(self.dir),
                                                    "inputs": {"prompt": "x"}})
        for _ in range(100):
            if s.requests:
                break
            time.sleep(0.05)
        self.send(p, method="cancel", params={})
        self.send(p, id=3, method="shutdown", params={})
        out, _ = p.communicate(timeout=10)
        answers = {json.loads(x).get("id"): json.loads(x) for x in out.splitlines() if x.startswith("{")}
        self.assertEqual(answers[2]["error"]["code"], -32800)
        self.assertEqual(self.files(), [])

    def begin(self, url, output=None):
        p = self.start(url)
        self.send(p, id=1, method="initialize", params={})
        inputs = {"prompt": "x", **({"output": output} if output else {})}
        self.send(p, id=2, method="invoke", params={"command": "image", "args": [], "cwd": str(self.dir),
                                                    "inputs": inputs})
        return p

    @staticmethod
    def wait_for(cond):
        for _ in range(200):
            if cond():
                return
            time.sleep(0.05)
        raise AssertionError("timed out")

    @staticmethod
    def answers(p):
        out, _ = p.communicate(timeout=10)
        return {json.loads(x).get("id"): json.loads(x) for x in out.splitlines() if x.startswith("{")}

    def finish(self, p):
        self.send(p, id=3, method="shutdown", params={})
        return self.answers(p)

    def test_file_created_while_generating_is_not_overwritten(self):
        s = self.server([png_line()], lead=0.5)
        p = self.begin(s.url, "x.png")
        self.wait_for(lambda: s.requests)
        (self.dir / "x.png").write_bytes(b"mine")
        answers = self.finish(p)
        self.assertEqual(answers[2]["result"], {"exit_code": 1})
        self.assertEqual((self.dir / "x.png").read_bytes(), b"mine")
        self.assertEqual(self.files(), ["x.png"])

    def test_existing_part_file_is_left_untouched(self):
        (self.dir / "x.png.part").write_bytes(b"mine")
        s = self.server([png_line()])
        p = self.begin(s.url, "x.png")
        answers = self.finish(p)
        self.assertEqual(answers[2]["result"], {"exit_code": 1})
        self.assertEqual(self.files(), ["x.png.part"])
        self.assertEqual((self.dir / "x.png.part").read_bytes(), b"mine")

    def test_cancel_before_the_result_arrives_publishes_nothing(self):
        s = self.server([png_line()], lead=0.4, hold=True)
        p = self.begin(s.url)
        self.wait_for(lambda: s.requests)
        self.send(p, method="cancel", params={})
        answers = self.finish(p)
        self.assertEqual(answers[2]["error"]["code"], -32800)
        self.assertEqual(self.files(), [])

    def test_cancel_with_nothing_in_flight_does_not_cancel_the_next_invoke(self):
        s = self.server(STEPS + [png_line()])
        p = self.start(s.url)
        self.send(p, id=1, method="initialize", params={})
        self.send(p, method="cancel", params={})
        time.sleep(0.3)
        self.send(p, id=2, method="invoke", params={"command": "image", "args": [], "cwd": str(self.dir),
                                                    "inputs": {"prompt": "x"}})
        answers = self.finish(p)
        self.assertEqual(answers[2]["result"]["exit_code"], 0)

    def test_cancel_closes_the_server_connection_and_the_next_invoke_is_prompt(self):
        slow = self.server([STEPS[0]], hold=True)
        p = self.begin(slow.url)
        self.wait_for(lambda: slow.requests)
        self.send(p, method="cancel", params={})
        fast = self.server(STEPS + [png_line()])
        self.wait_for(slow.closed.is_set)
        self.assertTrue(slow.closed.is_set())
        self.send(p, id=3, method="shutdown", params={})
        self.assertEqual(self.answers(p)[2]["error"]["code"], -32800)
        started = time.monotonic()
        answer, _ = self.invoke(fast.url)
        self.assertEqual(answer["result"]["exit_code"], 0)
        self.assertLess(time.monotonic() - started, 2)

    def test_bad_progress_lines_are_a_stream_error(self):
        cases = {
            "zero steps": {"step": 0, "steps": 0},
            "text step": {"step": "a", "steps": 4},
            "step past steps": {"step": 5, "steps": 4},
            "negative step": {"step": -1, "steps": 4},
        }
        for label, line in cases.items():
            with self.subTest(label):
                s = self.server([line, png_line()])
                answer, notes = self.invoke(s.url)
                self.assertEqual(answer["result"], {"exit_code": 1})
                self.assertIn("bad progress line", self.said(notes))
                self.assertEqual(self.files(), [])

    def test_bad_requests_get_json_rpc_errors(self):
        cases = {
            "inputs a list": ({"command": "image", "inputs": [1]}, -32602),
            "params a list": ([1], -32602),
            "unknown command": ({"command": "nope", "inputs": {}}, -32601),
            "cwd not a string": ({"command": "image", "inputs": {"prompt": "x"}, "cwd": 5}, -32602),
            "unexpected crash": ({"command": "image", "inputs": {"prompt": "x"}, "cwd": "\x00"}, -32603),
        }
        for label, (params, code) in cases.items():
            with self.subTest(label):
                p = self.start(self.server([png_line()]).url)
                self.send(p, id=2, method="invoke", params=params)
                self.assertEqual(self.finish(p)[2]["error"]["code"], code)

    def test_unreachable_server_names_the_url(self):
        dead = Server()
        url = dead.url
        dead.close()
        answer, notes = self.invoke(url)
        self.assertEqual(answer["result"], {"exit_code": 1})
        self.assertIn(url, self.said(notes))
        self.assertEqual(self.files(), [])

    def test_error_line_is_told_and_leaves_no_file(self):
        s = self.server([STEPS[0], {"error": "out of memory"}])
        answer, notes = self.invoke(s.url)
        self.assertEqual(answer["result"], {"exit_code": 1})
        self.assertIn("out of memory", self.said(notes))
        self.assertEqual(self.files(), [])

    def test_http_400_body_error_is_surfaced(self):
        s = self.server([{"error": "prompt too long"}], status=400)
        answer, notes = self.invoke(s.url)
        self.assertEqual(answer["result"], {"exit_code": 1})
        self.assertIn("prompt too long", self.said(notes))

    def test_stream_without_png_fails_and_leaves_no_file(self):
        s = self.server(STEPS)
        answer, _ = self.invoke(s.url)
        self.assertEqual(answer["result"], {"exit_code": 1})
        self.assertEqual(self.files(), [])

    def test_data_that_is_not_a_png_is_not_written(self):
        s = self.server(STEPS + [png_line(data=b"GIF89a nope")])
        answer, _ = self.invoke(s.url)
        self.assertEqual(answer["result"], {"exit_code": 1})
        self.assertEqual(self.files(), [])

    def test_bad_inputs_are_refused_before_calling_the_server(self):
        (self.dir / "taken.png").write_bytes(b"old")
        cases = {
            "existing output": {"output": "taken.png"},
            "output outside cwd": {"output": "../x.png"},
            "output in a folder": {"output": "a/x.png"},
            "output not png": {"output": "x.jpg"},
            "output with NUL": {"output": "a\x00b.png"},
            "output with newline": {"output": "a\nb.png"},
            "width not an integer": {"width": "wide"},
            "height a bool": {"height": True},
            "bad model": {"model": "schnell"},
            "steps too many": {"steps": 51},
            "seed negative": {"seed": -1},
        }
        for label, extra in cases.items():
            with self.subTest(label):
                s = self.server(STEPS + [png_line()])
                answer, notes = self.invoke(s.url, {"prompt": "x", **extra})
                self.assertEqual(answer["result"], {"exit_code": 1})
                self.assertTrue(self.said(notes))
                self.assertEqual(s.requests, [])
        self.assertEqual(self.files(), ["taken.png"])
        self.assertEqual((self.dir / "taken.png").read_bytes(), b"old")

    def test_missing_prompt_is_invalid_params(self):
        s = self.server()
        answer, _ = self.invoke(s.url, {})
        self.assertEqual(answer["error"]["code"], -32602)


if __name__ == "__main__":
    unittest.main()
