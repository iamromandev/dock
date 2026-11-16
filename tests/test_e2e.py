"""End-to-end: real host, real plugin processes, throwaway DOCK_HOME. Run `make test`."""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class DockE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.mkdtemp(prefix="dock-home-")
        cls.env = {**os.environ, "DOCK_HOME": cls.home, "PYTHONPATH": str(ROOT)}
        for name in ("hello", "chat", "download"):
            r = cls.dock("plugins", "install", str(ROOT / "plugins" / name))
            assert r.returncode == 0, r.stderr

    @classmethod
    def dock(cls, *args, stdin="", **kw):
        return subprocess.run(
            [sys.executable, "-m", "cli", *args],
            input=stdin, capture_output=True, text=True, check=False, env=cls.env, cwd=kw.get("cwd", ROOT), timeout=30,
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

    def test_node_chat_provisions_and_unprovisions(self):
        r = self.dock("chat", stdin="hello\nwhat is dock?\n/exit\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("toy model", r.stdout)
        data = Path(self.home, "data", "dev.dock.chat")
        self.assertFalse((data / "model.lock").exists(), "model was not released")
        self.assertFalse(Path(self.home, "state", "dev.dock.chat.provisioned").exists())

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
        self.assertFalse(Path(self.home, "state", "dev.dock.chat.provisioned").exists())

    def test_recovery_after_hard_crash(self):
        # Simulate a crash: provisioned marker + leftover resource, no running process.
        data = Path(self.home, "data", "dev.dock.chat")
        data.mkdir(parents=True, exist_ok=True)
        (data / "model.lock").write_text("12345")
        marker = Path(self.home, "state", "dev.dock.chat.provisioned")
        marker.write_text(json.dumps({"version": "0.1.0"}))

        r = self.dock("hello", "x")  # any command triggers recovery first
        self.assertIn("left provisioned", r.stderr)
        self.assertFalse(marker.exists())
        self.assertFalse((data / "model.lock").exists())


if __name__ == "__main__":
    unittest.main()
