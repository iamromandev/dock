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
            r = cls.dock("plugin", "install", str(ROOT / "plugin" / name))
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

    def starts(self, plugin_id):
        log = Path(self.home, "logs", f"{plugin_id}.log")
        return log.read_text().count(" start ") if log.exists() else 0

    def assert_usage_error(self, plugin_id, message, *args):
        """dock refuses the arguments itself: exit 2, its message, and no plugin process started."""
        before = self.starts(plugin_id)
        r = self.dock(*args)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertEqual(r.stderr.strip(), f"dock: {args[0]}: {message}")
        self.assertEqual(self.starts(plugin_id), before, "the plugin was started")

    def test_bad_args_become_clean_error(self):
        self.assert_usage_error("dev.dock.hello", "input 'n' must be an integer, got 'abc'", "count", "abc")

    def test_hello_greets_a_quoted_multi_word_name(self):
        r = self.dock("hello", "Ada Lovelace")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "hello, Ada Lovelace!")

    def test_hello_with_two_words_unquoted_is_a_usage_error(self):
        self.assert_usage_error("dev.dock.hello", "unexpected argument 'Lovelace'", "hello", "Ada", "Lovelace")

    def test_hello_with_an_empty_name_greets_it_instead_of_prompting(self):
        r = self.dock("hello", "")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("your name", r.stdout)
        self.assertEqual(r.stdout.strip(), "hello, stranger!")

    def test_count_counts_to_the_given_number(self):
        r = self.dock("count", "3")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("counted to 3", r.stdout)

    def test_count_defaults_to_ten(self):
        r = self.dock("count")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("counted to 10", r.stdout)

    def test_count_negative_needs_double_dash(self):
        # dock reads a leading '-' as a flag, so a negative number goes after `--`
        self.assert_usage_error("dev.dock.hello", "unknown flag '-3'", "count", "-3")
        r = self.dock("count", "--", "-3")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("counted to -3", r.stdout)

    def test_download_without_a_url_is_a_usage_error(self):
        self.assert_usage_error("dev.dock.download", "missing required input 'url'", "download")

    def test_download_with_a_repeated_output_flag_is_a_usage_error(self):
        self.assert_usage_error("dev.dock.download", "flag '-o' given more than once",
                                "download", "file:///x", "-o", "a", "-o", "b")

    def test_chat_with_an_argument_is_a_usage_error(self):
        self.assert_usage_error("dev.dock.chat", "unexpected argument 'extra'", "chat", "extra")

    def test_chat_without_arguments_still_works(self):
        r = self.dock("chat", stdin="/exit\n")
        self.assertEqual(r.returncode, 0, r.stderr)

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
        r = self.dock("plugin", "install", str(ROOT / "plugin" / "chat"), env=env)
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

    def test_download_file_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp, "src.bin")
            src.write_bytes(os.urandom(300_000))
            r = self.dock("download", src.as_uri(), "-o", "copy.bin", cwd=tmp)
            log = Path(self.home, "logs", "dev.dock.download.log")
            self.assertEqual(r.returncode, 0, r.stderr + (log.read_text()[-2000:] if log.exists() else ""))
            self.assertEqual(Path(tmp, "copy.bin").read_bytes(), src.read_bytes())
            r = self.dock("download", src.as_uri(), "--output=copy2.bin", cwd=tmp)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(Path(tmp, "copy2.bin").read_bytes(), src.read_bytes())

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
