"""The conformance checks: what a plugin must do from initialize to shutdown."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from .schema import validate

if TYPE_CHECKING:
    from .session import PluginSession

UNKNOWN_COMMAND = {"command": "__conformance_unknown__", "args": [], "cwd": "/tmp"}


class CheckFailed(Exception):
    def __init__(self, plugin: str, step: str, reason: str, line: str | None = None, stderr: list[str] | None = None):
        self.plugin, self.step, self.reason, self.line = plugin, step, reason, line
        self.stderr = list(stderr or [])
        super().__init__(f"{plugin}: {step}: {reason}")

    def __str__(self) -> str:
        out = f"{self.plugin}: {self.step}: {self.reason}"
        if self.line is not None:
            out += f"\n  line: {self.line}"
        if self.stderr:
            out += "\n  stderr (last 20 lines):\n" + "\n".join(f"    {s}" for s in self.stderr[-20:])
        return out


def _expect(s: PluginSession, step: str, resp: dict, schema: str) -> dict:
    errors = validate(resp, expect=schema)
    if errors:
        raise s.fail(step, "; ".join(errors), json.dumps(resp))
    return resp


def _expect_unknown(s: PluginSession, step: str) -> None:
    s.step = step
    resp = _expect(s, step, s.request("invoke", UNKNOWN_COMMAND), "error")
    if resp["error"]["code"] != -32601:
        raise s.fail(step, f"expected error -32601, got {resp['error']['code']}", json.dumps(resp))


def initialize(s: PluginSession) -> dict:
    s.step = "initialize"
    params = {"api_version": "2", "host": {"name": "dock-conformance", "version": "0"}, "data_dir": "/tmp/data"}
    return _expect(s, "initialize", s.request("initialize", params), "initialize_result")


def shutdown(s: PluginSession) -> None:
    s.step = "shutdown"
    _expect(s, "shutdown", s.request("shutdown", {}), "shutdown_result")
    # stdin stays open: a plugin that only exits when stdin closes does not honour shutdown
    code = s.wait_exit(5)
    if code is None:
        raise s.fail("shutdown", "did not exit within 5s after shutdown")
    s.step = "exit"
    if code != 0:
        raise s.fail("exit", f"exit code {code}, expected 0")
    s.drain_after_exit()


def run_lifecycle(s: PluginSession) -> None:
    """initialize, an unknown command, an idle cancel, the unknown command again, shutdown."""
    initialize(s)
    _expect_unknown(s, "unknown command")
    s.step = "idle cancel"
    s.notify("cancel", {})
    s.expect_silence(1.0)
    _expect_unknown(s, "invoke after idle cancel")
    shutdown(s)


def run_invoke(s: PluginSession, fixture: Path) -> dict:
    """Run the invoke described by <plugin_dir>/conformance.json; its result must be valid with an int exit_code."""
    params = json.loads(Path(fixture).read_text())["invoke"]
    s.step = "invoke"
    return _expect(s, "invoke", s.request("invoke", params), "invoke_result")
