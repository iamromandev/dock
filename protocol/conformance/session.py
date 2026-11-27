"""One plugin container driven over stdin/stdout, with every incoming message validated."""

from __future__ import annotations

import collections
import json
import queue
import subprocess
import threading
import time
from pathlib import Path

from .checks import CheckFailed
from .runtime import build, load_manifest, next_name, run_argv
from .schema import validate

_EOF = object()
MAX_LINES = 1000  # incoming lines buffered; past that the reader drops lines and the session fails
MAX_STDERR = 200  # stderr lines kept


class PluginSession:
    def __init__(self, plugin_dir: Path, rt: str):
        self.rt = rt
        self.manifest = load_manifest(plugin_dir)
        self.plugin = self.manifest.id
        self.step = "start"
        self.name = next_name()
        self.stderr: collections.deque[str] = collections.deque(maxlen=MAX_STDERR)
        self._stdout_eof = False
        self.dropped = 0  # stdout lines lost because the queue was full; any loss fails the session
        self._reader_done = False
        self._next_id = 0
        self._lines: queue.Queue = queue.Queue(maxsize=MAX_LINES)
        tag = build(rt, plugin_dir)
        self._proc = subprocess.Popen(
            run_argv(rt, self.manifest, tag, self.name),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        threading.Thread(target=self._read_stdout, daemon=True).start()
        self._stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self._stderr_thread.start()

    def __enter__(self) -> PluginSession:
        return self

    def __exit__(self, *exc) -> None:
        self.kill()
        self._proc.wait()
        for pipe in (self._proc.stdin, self._proc.stdout, self._proc.stderr):
            if pipe:
                try:
                    pipe.close()
                except OSError:
                    pass  # a broken pipe must not mask the CheckFailed being raised

    def fail(self, step: str, reason: str, line: str | None = None) -> CheckFailed:
        if self._proc.poll() is not None:  # the plugin is gone: let the stderr reader finish so the traceback is included
            self._stderr_thread.join(timeout=1)
        return CheckFailed(self.plugin, step, reason, line, list(self.stderr))

    def _read_stdout(self) -> None:
        assert self._proc.stdout is not None
        # Never block: a full queue would stop draining the pipe, and a plugin stuck writing to it stalls the
        # runtime's kill. Lines past the cap are counted and dropped, and the session then fails on them.
        for line in self._proc.stdout:
            try:
                self._lines.put_nowait(line.rstrip("\n"))
            except queue.Full:
                self.dropped += 1
        self._reader_done = True  # read by _incoming when _EOF does not fit, so this thread never blocks
        try:
            self._lines.put_nowait(_EOF)
        except queue.Full:
            pass

    def _read_stderr(self) -> None:
        assert self._proc.stderr is not None
        for line in self._proc.stderr:
            self.stderr.append(line.rstrip("\n"))

    def kill(self) -> None:
        if self._proc.poll() is None:
            self._rt_quietly("kill")
            self._proc.kill()
            self._proc.wait()
            # a container created after the kill attempt would otherwise stay behind
            self._rt_quietly("rm", "-f")

    def _rt_quietly(self, *args: str) -> None:
        try:
            subprocess.run([self.rt, *args, self.name], capture_output=True, timeout=30, check=False)
        except OSError, subprocess.TimeoutExpired:
            pass

    def _send(self, msg: dict) -> None:
        assert self._proc.stdin is not None
        try:
            self._proc.stdin.write(json.dumps(msg) + "\n")
            self._proc.stdin.flush()
        except OSError:
            pass  # the plugin exited; the next read reports it

    def notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _incoming(self, timeout: float) -> dict | None:
        """The next validated message, or None when nothing arrives within timeout."""
        self._check_dropped()
        try:
            line = self._lines.get(timeout=timeout)
        except queue.Empty:
            self._check_dropped()
            if not self._reader_done:
                return None
            line = _EOF
        if line is _EOF:
            self._stdout_eof = True
            raise self.fail(self.step, "plugin closed stdout (exited)")
        assert isinstance(line, str)
        return self._parse(line)

    def _check_dropped(self) -> None:
        if self.dropped:
            self.kill()
            raise self.fail(
                self.step,
                f"{self.dropped} stdout lines were not checked: the plugin wrote more than {MAX_LINES} lines "
                "faster than the checker read them",
            )

    def _parse(self, line: str) -> dict:
        try:
            msg = json.loads(line)
        except ValueError:
            raise self.fail(self.step, "non-JSON line", line) from None
        errors = validate(msg)
        if errors:
            raise self.fail(self.step, "; ".join(errors), line)
        if isinstance(msg, dict) and "method" in msg and "id" in msg:  # a prompt_user or form request
            # validate() only lets those two through, so there is nothing else to refuse
            self._send({"jsonrpc": "2.0", "id": msg["id"], "result": {"eof": True}})
        return msg

    def request(self, method: str, params: dict, timeout: float = 10) -> dict:
        """Send a request and return its response (a result or an error), ignoring notifications meanwhile."""
        self._next_id += 1
        rid = self._next_id
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        end = time.monotonic() + timeout
        while True:
            msg = self._incoming(max(0.0, end - time.monotonic()))
            if msg is None or time.monotonic() >= end:  # checked every pass: a chatty plugin must still time out
                self.kill()
                raise self.fail(self.step, f"no response to {method} within {timeout:g}s")
            if "method" in msg:
                continue
            if msg.get("id") != rid:
                raise self.fail(self.step, f"response id {msg.get('id')!r}, expected {rid}", json.dumps(msg))
            return msg

    def expect_silence(self, seconds: float) -> None:
        msg = self._incoming(seconds)
        if msg is not None:
            raise self.fail(self.step, "unexpected message", json.dumps(msg))

    def wait_exit(self, timeout: float) -> int | None:
        """The exit code, waiting with stdin still open; None (after killing it) when it stays up past timeout."""
        try:
            return self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.kill()
            return None

    def drain_after_exit(self) -> None:
        """Fail on anything the plugin wrote to stdout after its shutdown reply."""
        while True:
            try:
                line = self._lines.get(timeout=2)
            except queue.Empty:
                self._check_dropped()
                return
            if line is _EOF:
                self._stdout_eof = True
                return
            msg = self._parse(line)
            raise self.fail(self.step, "unexpected message after shutdown", json.dumps(msg))
