"""One plugin container driven over stdin/stdout, with every incoming message validated."""

from __future__ import annotations

import collections
import json
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from .checks import CheckFailed
from .rules import Areas, check_form
from .runtime import build, host_uid_gid, load_manifest, next_name, run_argv
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
        self.areas = Areas()
        self._lines: queue.Queue = queue.Queue(maxsize=MAX_LINES)
        tag = build(rt, plugin_dir)
        self.data_dir = tempfile.mkdtemp(prefix="dock-conformance-")
        try:
            self._proc = subprocess.Popen(
                run_argv(rt, self.manifest, tag, self.name, data_dir=self.data_dir, uid_gid=host_uid_gid()),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except BaseException:  # __exit__ never runs for a session that did not start
            shutil.rmtree(self.data_dir, ignore_errors=True)
            raise
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
        shutil.rmtree(self.data_dir, ignore_errors=True)

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

    def _reply(self, msg_id, result: dict, method: str) -> None:
        """Answer a plugin's request, after checking the reply against its own result schema."""
        reply = {"jsonrpc": "2.0", "id": msg_id, "result": result}
        assert not validate(reply, expect=f"{method}_result"), "checker sent an invalid reply"
        self._send(reply)

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
        if msg.get("method") == "form":
            errors = check_form(msg["params"])
            if errors:
                raise self.fail(self.step, "; ".join(errors), line)
        if "method" in msg and "id" not in msg:
            problem = self.areas.see(msg)
            if problem:
                raise self.fail(self.step, problem, line)
        if "method" in msg and "id" in msg:  # a prompt_user or form request
            # validate() only lets those two through, so there is nothing else to refuse
            self._reply(msg["id"], {"eof": True}, msg["method"])
        return msg

    def request(self, method: str, params: dict, timeout: float = 10) -> dict:
        """Send a request and return its response (a result or an error), ignoring notifications meanwhile."""
        self._next_id += 1
        rid = self._next_id
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        return self._await(rid, time.monotonic() + timeout, f"{method} within {timeout:g}s")

    def _await(self, rid: int, end: float, what: str = "the request") -> dict:
        """The response with id rid, ignoring notifications; fails when it has not arrived by `end` (monotonic)."""
        while True:
            msg = self._incoming(max(0.0, end - time.monotonic()))
            if msg is not None and "method" not in msg:
                return self._check_id(msg, rid)
            if msg is None or time.monotonic() >= end:  # checked every pass: a chatty plugin must still time out
                late = self._queued_reply(rid)
                if late is not None:
                    return late
                self.kill()
                raise self.fail(self.step, f"no response to {what}")

    def _check_id(self, msg: dict, rid: int) -> dict:
        if msg.get("id") != rid:
            raise self.fail(self.step, f"response id {msg.get('id')!r}, expected {rid}", json.dumps(msg))
        return msg

    def _queued_reply(self, rid: int) -> dict | None:
        """One last look at the queue after the deadline: a reply that has already arrived still counts, even
        behind notifications (each one is still validated)."""
        while True:
            try:
                line = self._lines.get_nowait()
            except queue.Empty:
                return None
            if not isinstance(line, str):  # _EOF: put it back so the next read reports the exit
                self._lines.put_nowait(line)
                return None
            msg = self._parse(line)
            if "method" not in msg:
                return self._check_id(msg, rid)

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
