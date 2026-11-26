"""The planner plugin driven over its stdio protocol, against a local stand-in for the model's HTTP endpoint."""

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

PLANNER = Path(__file__).resolve().parent.parent / "main.py"
FLOW = {"params": {}, "steps": [{"plugin": "dev.dock.dm", "command": "dm", "inputs": {"url": "http://x", "dir": "{work}"}}]}


class Model:
    """A local HTTP server that answers /api/chat with `reply` (status, body) after `delay` seconds."""

    def __init__(self, status=200, body=b"", delay=0.0):
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                size = int(self.headers.get("Content-Length", 0))
                outer.requests.append({"path": self.path, "body": json.loads(self.rfile.read(size))})
                time.sleep(delay)
                try:
                    self.send_response(status)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
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


def chat(content) -> bytes:
    return json.dumps({"message": {"role": "assistant", "content": content}}).encode()


class Planner(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="dock-planner-"))
        self.catalog = self.dir / "catalog.json"
        self.catalog.write_text(json.dumps({"plugins": [{"id": "dev.dock.dm", "commands": []}]}), encoding="utf-8")
        self.models: list[Model] = []

    def tearDown(self):
        for m in self.models:
            m.close()

    def model(self, **kw) -> Model:
        m = Model(**kw)
        self.models.append(m)
        return m

    def start(self, url, model_name=None):
        env = {**os.environ, "DOCK_PLANNER_URL": url}
        env.pop("DOCK_PLANNER_MODEL", None)
        if model_name:
            env["DOCK_PLANNER_MODEL"] = model_name
        return subprocess.Popen([sys.executable, str(PLANNER)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env=env)

    @staticmethod
    def send(p, **msg):
        p.stdin.write(json.dumps({"jsonrpc": "2.0", **msg}) + "\n")
        p.stdin.flush()

    def invoke(self, url, inputs=None, model_name=None):
        """Returns (the invoke answer, the notifications seen before it)."""
        p = self.start(url, model_name)
        inputs = {"prompt": "get x", "catalog": str(self.catalog)} if inputs is None else inputs
        self.send(p, id=1, method="initialize", params={})
        self.send(p, id=2, method="invoke", params={"command": "plan", "args": [], "cwd": "/", "inputs": inputs})
        self.send(p, id=3, method="shutdown", params={})
        out, _ = p.communicate(timeout=30)
        msgs = [json.loads(x) for x in out.splitlines() if x.startswith("{")]
        return next(m for m in msgs if m.get("id") == 2), [m for m in msgs if "method" in m]

    def test_success_answers_with_the_flow_and_sends_the_catalog_and_prompt_to_the_model(self):
        m = self.model(body=chat(json.dumps(FLOW)))
        answer, _ = self.invoke(m.url, model_name="tiny:1b")
        self.assertEqual(answer["result"], {"exit_code": 0, "outputs": {}, "flow": FLOW})
        req = m.requests[0]
        self.assertEqual(req["path"], "/api/chat")
        body = req["body"]
        self.assertEqual((body["model"], body["stream"]), ("tiny:1b", False))
        self.assertIn("steps", body["format"]["properties"])
        text = json.dumps(body["messages"])
        self.assertIn("get x", text)
        self.assertIn("dev.dock.dm", text)

    def test_the_model_is_told_results_go_to_the_current_folder_and_work_is_scratch(self):
        # {work} is deleted after a successful run: a download put there is lost, and a step the request did not ask
        # for runs unseen with --yes
        m = self.model(body=chat(json.dumps(FLOW)))
        self.invoke(m.url)
        system = next(x["content"] for x in m.requests[0]["body"]["messages"] if x["role"] == "system")
        self.assertIn("deleted", system)
        self.assertIn("current folder", system)
        self.assertNotIn("must go into {work}", system)
        self.assertIn("Do not add steps", system)

    def test_unreachable_model_says_where_and_gives_no_flow(self):
        dead = Model()
        url = dead.url
        dead.close()
        answer, notes = self.invoke(url)
        self.assertEqual(answer["result"], {"exit_code": 1})
        said = " ".join(json.dumps(n["params"]) for n in notes)
        self.assertIn("cannot reach the model at " + url, said)

    def test_a_reply_that_is_not_json_gives_no_flow(self):
        m = self.model(body=chat("sure, here is your flow"))
        answer, notes = self.invoke(m.url)
        self.assertEqual(answer["result"], {"exit_code": 1})
        self.assertIn("not JSON", " ".join(json.dumps(n["params"]) for n in notes))

    def test_an_http_error_gives_no_flow(self):
        m = self.model(status=404, body=b'{"error":"model not found"}')
        answer, notes = self.invoke(m.url)
        self.assertEqual(answer["result"], {"exit_code": 1})
        self.assertIn("404", " ".join(json.dumps(n["params"]) for n in notes))

    def test_an_unreadable_catalog_gives_no_flow(self):
        m = self.model(body=chat(json.dumps(FLOW)))
        answer, _ = self.invoke(m.url, {"prompt": "x", "catalog": str(self.dir / "missing.json")})
        self.assertEqual(answer["result"], {"exit_code": 1})
        self.assertEqual(m.requests, [])

    def test_cancel_during_the_model_call_ends_with_32800(self):
        m = self.model(body=chat(json.dumps(FLOW)), delay=5)
        p = self.start(m.url)
        self.send(p, id=1, method="initialize", params={})
        self.send(p, id=2, method="invoke", params={"command": "plan", "args": [], "cwd": "/",
                                                    "inputs": {"prompt": "x", "catalog": str(self.catalog)}})
        for _ in range(100):
            if m.requests:
                break
            time.sleep(0.05)
        self.send(p, method="cancel", params={})
        self.send(p, id=3, method="shutdown", params={})
        out, _ = p.communicate(timeout=3)
        answers = {json.loads(x).get("id"): json.loads(x) for x in out.splitlines() if x.startswith("{")}
        self.assertEqual(answers[2]["error"]["code"], -32800)


if __name__ == "__main__":
    unittest.main()
