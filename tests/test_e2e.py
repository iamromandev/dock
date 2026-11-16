"""End-to-end: real host, real plugin processes, throwaway DOCK_HOME. Run `make test`."""

import http.server
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class DockE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.mkdtemp(prefix="dock-home-")
        cls.env = {**os.environ, "DOCK_HOME": cls.home, "PYTHONPATH": str(ROOT)}
        for name in ("hello", "llm", "chat", "download"):
            r = cls.dock("plugins", "install", str(ROOT / "plugins" / name))
            assert r.returncode == 0, r.stderr

    @classmethod
    def dock(cls, *args, stdin="", env=None, **kw):
        return subprocess.run(
            [sys.executable, "-m", "cli", *args],
            input=stdin, capture_output=True, text=True, check=False,
            env=env or cls.env, cwd=kw.get("cwd", ROOT), timeout=30,
        )

    def test_help_lists_plugin_commands_without_running_them(self):
        r = self.dock("help")
        for cmd in ("hello", "count", "chat", "download"):
            self.assertIn(cmd, r.stdout)

    def test_python_plugin_with_args(self):
        r = self.dock("hello", "Roman")
        self.assertEqual(r.stdout.strip(), "hello, Roman!")

    def test_python_plugin_prompts_through_host(self):
        r = self.dock("hello", stdin="Ada\n")
        self.assertIn("hello, Ada!", r.stdout)

    def test_bad_args_become_clean_error(self):
        r = self.dock("count", "abc")
        self.assertEqual(r.returncode, 1)
        self.assertIn("count needs a number", r.stderr)

    def test_chat_uses_llm_plugin_and_releases_it(self):
        r = self.dock("chat", stdin="hello\nwhat is dock?\nthird\n/exit\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("toy: toy-1", r.stdout)
        self.assertIn("toy model served by the dock llm plugin", r.stdout)
        self.assertIn("message 3 of our conversation", r.stdout)  # history reached the llm
        self.assertFalse(Path(self.home, "data", "dev.dock.llm", "model.lock").exists(), "model not released")
        self.assertFalse(Path(self.home, "state", "dev.dock.llm.provisioned").exists())

    def test_chat_with_openai_compatible_server(self):
        seen = []

        class FakeLLM(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                seen.append(body)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for tok in ("Hi", " from", " the", " server"):
                    chunk = {"choices": [{"delta": {"content": tok}}]}
                    self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")

            def log_message(self, *a):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeLLM)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            env = {
                **self.env,
                "DOCK_LLM_BASE_URL": f"http://127.0.0.1:{server.server_port}/v1",
                "DOCK_LLM_MODEL": "test-model",
                "NO_PROXY": "127.0.0.1,localhost",
                "no_proxy": "127.0.0.1,localhost",
            }
            r = self.dock("chat", stdin="one\ntwo\n/exit\n", env=env)
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("openai-compatible: test-model", r.stdout)
        self.assertIn("Hi from the server", r.stdout)
        self.assertEqual(seen[0]["model"], "test-model")
        self.assertTrue(seen[0]["stream"])
        self.assertEqual(
            [m["role"] for m in seen[1]["messages"]], ["user", "assistant", "user"]
        )  # second turn carries the first answer

    def test_tokens_stream_live_through_two_plugins(self):
        class SlowLLM(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for i in range(10):
                    chunk = {"choices": [{"delta": {"content": f"tok{i} "}}]}
                    self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                    self.wfile.flush()
                    time.sleep(0.2)
                self.wfile.write(b"data: [DONE]\n\n")

            def log_message(self, *a):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), SlowLLM)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        env = {
            **self.env,
            "DOCK_LLM_BASE_URL": f"http://127.0.0.1:{server.server_port}/v1",
            "DOCK_LLM_MODEL": "slow",
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        }
        p = subprocess.Popen(
            [sys.executable, "-m", "cli", "chat"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=env, cwd=ROOT,
        )
        try:
            p.stdin.write(b"go\n")
            p.stdin.flush()
            seen, deadline = b"", time.monotonic() + 10
            while b"tok0" not in seen and time.monotonic() < deadline:
                seen += os.read(p.stdout.fileno(), 4096)
            self.assertIn(b"tok0", seen)
            self.assertNotIn(b"tok9", seen, "reply was buffered, not streamed")
            p.stdin.write(b"/exit\n")
            p.stdin.close()
            p.wait(timeout=15)
            self.assertEqual(p.returncode, 0)
        finally:
            if p.poll() is None:
                p.kill()
            p.stdout.close()
            p.stderr.close()
            server.shutdown()
            server.server_close()

    def test_chat_fails_cleanly_without_llm(self):
        home = tempfile.mkdtemp(prefix="dock-home-")
        env = {**self.env, "DOCK_HOME": home}
        r = self.dock("plugins", "install", str(ROOT / "plugins" / "chat"), env=env)
        self.assertIn("nothing installed provides 'llm'", r.stdout)
        r = self.dock("chat", env=env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("no installed plugin provides 'llm'", r.stderr)

    def test_bad_llm_config_fails_before_chat_starts(self):
        env = {**self.env, "DOCK_LLM_BASE_URL": "http://127.0.0.1:9/v1"}  # no DOCK_LLM_MODEL
        r = self.dock("chat", env=env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("DOCK_LLM_MODEL must be set", r.stderr)
        self.assertFalse(Path(self.home, "state", "dev.dock.llm.provisioned").exists())

    def test_go_download_file_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp, "src.bin")
            src.write_bytes(os.urandom(300_000))
            r = self.dock("download", src.as_uri(), "-o", "copy.bin", cwd=tmp)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(Path(tmp, "copy.bin").read_bytes(), src.read_bytes())

    def test_unknown_command(self):
        r = self.dock("nope")
        self.assertEqual(r.returncode, 1)
        self.assertIn("unknown command", r.stderr)

    def test_ctrl_c_cancels_and_cleans_up(self):
        p = subprocess.Popen(
            [sys.executable, "-m", "cli", "chat"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, env=self.env, cwd=ROOT,
        )
        time.sleep(1.0)  # let it provision and wait at the prompt
        p.send_signal(signal.SIGINT)
        _, err = p.communicate(timeout=15)
        self.assertEqual(p.returncode, 130, err)
        self.assertFalse(Path(self.home, "state", "dev.dock.llm.provisioned").exists())
        self.assertFalse(Path(self.home, "data", "dev.dock.llm", "model.lock").exists())

    def test_recovery_after_hard_crash(self):
        # Simulate a crash: provisioned marker + leftover resource, no running process.
        data = Path(self.home, "data", "dev.dock.llm")
        data.mkdir(parents=True, exist_ok=True)
        (data / "model.lock").write_text("12345")
        marker = Path(self.home, "state", "dev.dock.llm.provisioned")
        marker.write_text(json.dumps({"version": "0.1.0"}))

        r = self.dock("hello", "x")  # any command triggers recovery first
        self.assertIn("left provisioned", r.stderr)
        self.assertFalse(marker.exists())
        self.assertFalse((data / "model.lock").exists())


if __name__ == "__main__":
    unittest.main()
