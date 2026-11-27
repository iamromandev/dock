"""The checker itself: it must accept a good plugin and fail, with a clear reason, for each kind of bad one."""

import json
import os
import queue
import time
from pathlib import Path

import pytest

from conformance import checks, runtime
from conformance.checks import CheckFailed
from conformance.rules import Areas
from conformance.runtime import require_runtime
from conformance.session import PluginSession

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(scope="module")
def rt():
    return require_runtime()


def _lifecycle(rt, name):
    with PluginSession(FIXTURES / name, rt) as s:
        checks.run_lifecycle(s)


def test_good_plugin_passes_lifecycle_and_invoke(rt):
    # Bug caught: the checker rejects a correct plugin (false positive).
    with PluginSession(FIXTURES / "good", rt) as s:
        checks.run_lifecycle(s)
    with PluginSession(FIXTURES / "good", rt) as s:
        checks.initialize(s)
        checks.run_invoke(s, FIXTURES / "good" / "conformance.json")
        checks.shutdown(s)


@pytest.mark.parametrize(
    ("name", "reason"),
    [
        ("noisy", "non-JSON line"),
        ("badbar", "$.params: 'id' is a required property"),
        ("cancelreply", "unexpected message"),
        ("lingering", "did not exit within 5s after shutdown"),
        ("straystdout", "non-JSON line"),
        ("exitone", "exit code 1"),
        ("crasher", "KNOWN-CRASH-MARKER"),
        ("flood", "stdout lines were not checked"),
    ],
)
def test_bad_plugin_fails_with_reason(rt, name, reason):
    # Bug caught: a protocol violation (stdout noise, invalid message, reply to an idle cancel) goes unnoticed.
    with pytest.raises(CheckFailed) as e:
        _lifecycle(rt, name)
    assert reason in str(e.value)
    assert str(e.value).startswith(f"test.{name}: ")


def test_silent_plugin_fails_within_timeout(rt):
    # Bug caught: a plugin that never answers hangs the suite instead of failing.
    with PluginSession(FIXTURES / "silent", rt) as s:  # the image is built here, outside the timed part
        start = time.monotonic()
        with pytest.raises(CheckFailed) as e:
            checks.run_lifecycle(s)
        assert time.monotonic() - start < 30
    assert "initialize" in str(e.value)


def test_string_exit_code_fails_invoke(rt):
    # Bug caught: run_invoke accepts an invoke result whose exit_code is not an integer.
    with PluginSession(FIXTURES / "badexit", rt) as s:
        checks.initialize(s)
        with pytest.raises(CheckFailed) as e:
            checks.run_invoke(s, FIXTURES / "badexit" / "conformance.json")
    assert "exit_code" in str(e.value)


def test_chatty_plugin_fails_within_timeout(rt):
    # Bug caught: a plugin streaming notifications nonstop never hits the deadline, so the suite hangs.
    with PluginSession(FIXTURES / "chatty", rt) as s:
        start = time.monotonic()
        with pytest.raises(CheckFailed) as e:
            s.request("initialize", {}, timeout=2)
        assert time.monotonic() - start < 15
    assert "no response to initialize" in str(e.value)


def test_missing_runtime_fails_naming_docker(monkeypatch):
    # AC7: with no runtime the suite stops with a message naming Docker/podman.
    monkeypatch.setenv("DOCK_TEST_RUNTIME", "/nonexistent")
    monkeypatch.setattr(runtime, "_reachable", {})
    with pytest.raises(RuntimeError, match=r"Docker.*podman"):
        runtime.require_runtime()


def _bare_session() -> tuple[PluginSession, list[dict]]:
    s = object.__new__(PluginSession)
    sent: list[dict] = []
    s._send = sent.append  # ty: ignore[invalid-assignment]
    return s, sent


def test_reply_validates_its_own_reply():
    # Bug caught: the checker answers a prompt with a reply the protocol does not allow, and nothing notices.
    s, sent = _bare_session()
    s._reply(3, {"eof": True}, "form")
    assert sent == [{"jsonrpc": "2.0", "id": 3, "result": {"eof": True}}]
    with pytest.raises(AssertionError, match="checker sent an invalid reply"):
        s._reply(3, {"text": 5}, "prompt_user")


def test_form_with_repeated_name_fails_invoke(rt):
    # Bug caught: the checker validates a form against the schema only, so a repeated field name goes unnoticed.
    with PluginSession(FIXTURES / "dupform", rt) as s:
        checks.initialize(s)
        with pytest.raises(CheckFailed) as e:
            checks.run_invoke(s, FIXTURES / "dupform" / "conformance.json")
    assert "form field name 'x' repeated" in str(e.value)


def test_33_areas_in_one_invoke_fail(rt):
    # Bug caught: a plugin opening more than 32 areas in one invoke passes the checker.
    with PluginSession(FIXTURES / "areas33", rt) as s:
        checks.initialize(s)
        with pytest.raises(CheckFailed) as e:
            checks.run_invoke(s, FIXTURES / "areas33" / "conformance.json")
    assert "limit is 32 areas" in str(e.value)


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="needs POSIX uid/gid")
def test_plugin_runs_as_host_user_with_writable_data_dir_that_is_removed(rt):
    # Bug caught: the container runs as root or with HOME unset, or the data dir is unwritable or leaks.
    with PluginSession(FIXTURES / "whoami", rt) as s:
        result = checks.initialize(s)["result"]
        data_dir = Path(s.data_dir)
        assert data_dir.is_dir()
    assert (result["uid"], result["gid"], result["home"], result["data_writable"]) == (
        os.getuid(),
        os.getgid(),
        "/tmp",
        True,
    )
    assert not data_dir.exists()


def test_await_returns_a_reply_already_queued_when_the_deadline_has_passed():
    # Bug caught: a reply that arrived just before the deadline is dropped and reported as a timeout.
    s = object.__new__(PluginSession)
    s.dropped, s._reader_done, s.step, s.plugin = 0, False, "invoke", "t"
    s.areas = Areas()
    s._lines = queue.Queue()
    s._lines.put('{"jsonrpc": "2.0", "id": 1, "result": {}}')
    assert s._await(1, time.monotonic() - 1)["id"] == 1


def test_await_finds_a_queued_reply_behind_notifications():
    # Bug caught: the last look after the deadline reads one line, so a reply queued behind a log is reported as a timeout.
    s = object.__new__(PluginSession)
    s.dropped, s._reader_done, s.step, s.plugin = 0, False, "invoke", "t"
    s.areas = Areas()
    s._lines = queue.Queue()
    for text in ("a", "b"):
        s._lines.put(json.dumps({"jsonrpc": "2.0", "method": "log", "params": {"level": "info", "message": text}}))
    s._lines.put('{"jsonrpc": "2.0", "id": 1, "result": {}}')
    assert s._await(1, time.monotonic() - 1)["id"] == 1


def test_data_dir_is_removed_when_the_container_fails_to_start(monkeypatch, tmp_path):
    # Bug caught: the data folder is made before the container starts, and a failed start leaves it behind.
    import conformance.session as session_mod

    made = []
    real_mkdtemp = session_mod.tempfile.mkdtemp
    monkeypatch.setattr(session_mod, "build", lambda rt, d: "tag")
    monkeypatch.setattr(session_mod.tempfile, "mkdtemp", lambda **kw: made.append(real_mkdtemp(dir=tmp_path)) or made[-1])

    def no_start(*a, **kw):
        raise FileNotFoundError("runtime vanished")

    monkeypatch.setattr(session_mod.subprocess, "Popen", no_start)
    with pytest.raises(FileNotFoundError):
        PluginSession(FIXTURES / "good", "/nonexistent-runtime")
    assert made and not Path(made[0]).exists()
