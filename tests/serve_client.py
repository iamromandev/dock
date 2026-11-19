"""A test client for `dock serve`: spawns it, writes request lines, collects replies and notifications."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import dockenv  # noqa: E402
from cli.store import Store  # noqa: E402


class ServeClient:
    """One `python -m cli serve` child. Everything it prints is recorded; stdout lines must all be JSON.

    `answer_prompts`: a callable (params) -> result dict, used for every `prompt` request dock sends;
    None leaves them pending (see `prompts`).
    """

    def __init__(self, home, env=None, answer_prompts=None, cwd=ROOT):
        self.home = str(home)
        full_env = {**os.environ, "DOCK_HOME": self.home, "PYTHONPATH": str(ROOT), **(env or {})}
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "cli", "serve"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=full_env, cwd=cwd, **dockenv.SIGINT_DEFAULT,
        )
        self.answer_prompts = answer_prompts
        self.cond = threading.Condition()
        self.replies = {}        # request id -> message
        self.notifications = []  # messages without an id
        self.prompts = []        # dock's requests to us: messages with an id and a method
        self.bad_lines = []      # stdout lines that are not JSON: there must be none
        self.stderr = b""
        self._write_lock = threading.Lock()
        self._next = 0
        self._threads = [
            threading.Thread(target=self._read_stdout, daemon=True),
            threading.Thread(target=self._read_stderr, daemon=True),
        ]
        for t in self._threads:
            t.start()

    # reading -----------------------------------------------------------------
    def _read_stdout(self):
        for raw in self.proc.stdout:
            try:
                msg = json.loads(raw)
            except ValueError:
                with self.cond:
                    self.bad_lines.append(raw)
                    self.cond.notify_all()
                continue
            reply_to = None
            with self.cond:
                if "method" in msg and "id" in msg:
                    self.prompts.append(msg)
                    reply_to = msg
                elif "method" in msg:
                    self.notifications.append(msg)
                else:
                    self.replies[msg.get("id")] = msg
                self.cond.notify_all()
            if reply_to is not None and self.answer_prompts is not None:
                self.send({"id": reply_to["id"], "result": self.answer_prompts(reply_to["params"])})

    def _read_stderr(self):
        for chunk in iter(lambda: self.proc.stderr.read1(4096), b""):
            self.stderr += chunk

    # writing -----------------------------------------------------------------
    def send_raw(self, data: bytes):
        with self._write_lock:
            self.proc.stdin.write(data)
            self.proc.stdin.flush()

    def send(self, msg: dict):
        self.send_raw((json.dumps({"jsonrpc": "2.0", **msg}) + "\n").encode())

    def start(self, method, params=None) -> int:
        """Send a request and return its id without waiting."""
        self._next += 1
        self.send({"id": self._next, "method": method, "params": params or {}})
        return self._next

    def notify(self, method, params=None):
        self.send({"method": method, "params": params or {}})

    def wait_reply(self, req_id, timeout=15.0) -> dict:
        end = time.monotonic() + timeout
        with self.cond:
            while req_id not in self.replies:
                left = end - time.monotonic()
                if left <= 0:
                    raise AssertionError(f"no reply to {req_id} in {timeout}s; stderr: {self.stderr!r}")
                self.cond.wait(left)
            return self.replies[req_id]

    def request(self, method, params=None, timeout=15.0) -> dict:
        return self.wait_reply(self.start(method, params), timeout)

    def hello(self) -> dict:
        reply = self.request("hello", {"client": {"name": "test", "version": "0"}})
        assert "result" in reply, reply
        return reply["result"]

    def wait_until(self, predicate, timeout=15.0, what="condition") -> None:
        end = time.monotonic() + timeout
        with self.cond:
            while not predicate():
                left = end - time.monotonic()
                if left <= 0:
                    raise AssertionError(f"timed out waiting for {what}; stderr: {self.stderr!r}")
                self.cond.wait(left)

    def notes(self, method=None, call=None):
        with self.cond:
            return [n for n in self.notifications
                    if (method is None or n["method"] == method) and (call is None or n["params"].get("call") == call)]

    def output_text(self, call) -> str:
        return "".join(n["params"]["text"] for n in self.notes("output", call))

    def cid_of(self, call) -> str:
        """The container id (hostname) a pidprobe command printed through `output`."""
        return self.output_text(call).split(" cid ")[1].split()[0]

    # ending ------------------------------------------------------------------
    def close_stdin(self):
        try:
            self.proc.stdin.close()
        except OSError:
            pass

    def wait(self, timeout=15.0) -> int:
        try:
            code = self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
            raise AssertionError(f"dock serve did not exit in {timeout}s; stderr: {self.stderr!r}") from None
        for t in self._threads:
            t.join(timeout=2)
        return code

    def kill(self):
        self.proc.kill()
        self.proc.wait()

    def cleanup(self):
        """For addCleanup: never leave a child behind, whatever the test did."""
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        for t in self._threads:
            t.join(timeout=2)
        for pipe in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            try:
                pipe.close()
            except OSError:
                pass


PLUGINS = ("hello",)
FIXTURES = ("pidprobe", "prompter")
GRACE = "2"  # DOCK_CANCEL_GRACE for the children: a stuck plugin is killed after 2 s, so tests stay quick


PLUGIN_IDS = ("dev.dock.hello", "dev.dock.pidprobe", "dev.dock.prompter")
_images_ready = False


def make_home(prefix="dock-serve-home-"):
    """A throwaway DOCK_HOME with hello and the serve fixtures, set to run them in the real runtime."""
    home = Path(tempfile.mkdtemp(prefix=prefix))
    store = Store(home)
    for name in PLUGINS:
        store.install(ROOT / "plugin" / name)
    for name in FIXTURES:
        store.install(ROOT / "tests" / "fixtures" / name)
    rt = dockenv.runtime()
    if rt:  # classes that start a plugin call require_images() first, which fails clearly without one
        dockenv.write_config(home, rt)  # a first install asks the runtime nothing, so setting the runtime here is only the last step of the setup
    return home


def require_images():
    """For setUpClass: a runtime is there (else an error, never a skip) and the plugin images are built."""
    global _images_ready
    dockenv.require_runtime()
    if not _images_ready:
        home = make_home("dock-serve-prebuild-")
        try:
            dockenv.prebuild(home, list(PLUGIN_IDS))
        finally:
            shutil.rmtree(home, ignore_errors=True)
        _images_ready = True


class ServeCase(unittest.TestCase):
    """A throwaway DOCK_HOME per test, and a client factory that always cleans up."""

    @classmethod
    def setUpClass(cls):
        require_images()

    def setUp(self):
        self.home = make_home()

    def client(self, **kw) -> ServeClient:
        kw["env"] = {"DOCK_CANCEL_GRACE": GRACE, **kw.get("env", {})}
        c = ServeClient(self.home, **kw)
        self.addCleanup(c.cleanup)
        return c

    def hello_client(self, **kw) -> ServeClient:
        c = self.client(**kw)
        c.hello()
        return c
