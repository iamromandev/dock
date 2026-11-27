"""The checker itself: it must accept a good plugin and fail, with a clear reason, for each kind of bad one."""

import time
from pathlib import Path

import pytest

from conformance import checks, runtime
from conformance.checks import CheckFailed
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
