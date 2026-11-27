"""R4: Session.cancel() stops a running invoke from another thread, with the effect of Ctrl-C."""

import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import dockenv
from src import isolation, lifecycle
from src.lifecycle import Session
from src.process import PluginProcess
from src.store import Store
from test_lifecycle_seams import RecordingUI

PROBE = ROOT / "tests" / "fixtures" / "probe"


@pytest.fixture(scope="module")
def home():
    dockenv.prebuild_plugins(PROBE)
    return dockenv.make_home("dock-cancel-", PROBE)


@pytest.fixture
def run(home):
    events = []
    session = Session(Store(home), lambda pid: RecordingUI(pid, events))
    yield session, events, session.store.get("dev.dock.probe")
    session.close()


def test_cancel_from_another_thread_ends_the_invoke_like_ctrl_c(run, capfd):
    # bug: cancel() is ignored, or kills the plugin instead of asking it to stop, or leaks text to the real streams
    session, events, manifest = run

    def cancel_once_running():  # the container start is not instant: cancel when the plugin has spoken
        deadline = time.monotonic() + 60
        while not any(kind == "output" for _, kind, _ in events) and time.monotonic() < deadline:
            time.sleep(0.02)
        session.cancel()

    timer = threading.Thread(target=cancel_once_running)
    timer.start()
    started = time.monotonic()
    code = session.invoke(manifest, "sleep", ["60"])
    timer.join()
    assert code == 130
    assert time.monotonic() - started < 5  # the plugin honoured `cancel`: nobody waited out the grace period
    proc = session.commands[manifest.id]
    assert proc.alive()  # not killed: it answered -32800 itself
    assert ("dev.dock.probe", "output", "sleeping\n") in events
    assert ("dev.dock.probe", "warn", "interrupted, cancelling…") in events
    assert not any(kind == "warn" and "did not stop" in str(text) for _, kind, text in events)
    seen = capfd.readouterr()
    assert (seen.out, seen.err) == ("", "")  # the UI owns all output


def test_cancel_with_nothing_running_is_ignored(run):
    # bug: a stale cancel is kept and aborts the next invoke (or counts for the next flow)
    session, events, manifest = run
    session.cancel()
    assert session.invoke(manifest, "code", ["0"]) == 0
    assert not session.cancelled
    assert session.invoke(manifest, "code", ["3"]) == 3
    session.begin()
    session.cancel()
    assert session.cancelled
    session.begin()  # a new operation: the cancel before it is stale
    assert not session.cancelled


def test_cancel_before_the_plugin_runs_says_interrupted_and_leaves_no_plugin(run, monkeypatch):
    # bug: a cancel that lands while nothing waits (between flow steps, during the image build) is dropped and the
    # step runs anyway; or a cancel during initialize leaks the half-started plugin
    session, events, manifest = run
    real_prepare, real_request, started = isolation.prepare, PluginProcess.request, []

    def prepare_then_cancel(*args, **kw):
        launch = real_prepare(*args, **kw)
        session.cancel()  # the build itself is not interrupted; the host stops once it is done
        return launch

    def cancel_on_initialize(self, method, *args, **kw):
        if method == "initialize":
            session.cancel()
        return real_request(self, method, *args, **kw)

    def start_nothing(self):
        started.append(self)

    def step_then_cancel():
        assert session.invoke_inputs(manifest, "noargs", None) == {"exit_code": 0}  # the previous step: the plugin runs
        session.cancel()  # lands after it ended, before the next step drains its inbox

    stages = {  # name -> (what is patched, what is called before the step)
        "between flow steps": ({}, step_then_cancel),
        "while the image is prepared": ({(isolation, "prepare"): prepare_then_cancel,
                                         (PluginProcess, "start"): start_nothing}, None),
        "during initialize": ({(PluginProcess, "request"): cancel_on_initialize}, None),
    }
    for stage, (patches, before) in stages.items():
        events.clear()
        session.close()
        session.begin()  # the flow begins
        with monkeypatch.context() as m:
            for (owner, name), replacement in patches.items():
                m.setattr(owner, name, replacement)
            if before:
                before()
            running = len(session.procs)  # a plugin of an earlier step stays: only this step does not start
            assert session.invoke_inputs(manifest, "code", None) == {"exit_code": 130}, stage
        assert ("dev.dock.probe", "warn", "interrupted") in events, stage
        assert len(session.procs) == running and started == [], stage  # nothing left half-started or started late


def cancel_when_stubborn_speaks(session, events, times):
    """Cancel `times` times (0.5 s apart) once the stubborn plugin has started."""
    def go():
        deadline = time.monotonic() + 60
        while not any(text == "stubborn started\n" for _, _, text in events) and time.monotonic() < deadline:
            time.sleep(0.02)
        for _ in range(times):
            session.cancel()
            time.sleep(0.5)

    t = threading.Thread(target=go)
    t.start()
    return t


def test_plugin_that_ignores_cancel_is_killed_after_the_grace_period(run, monkeypatch):
    # bug: the grace period is skipped or never ends, or the stuck plugin is left running without a word
    session, events, manifest = run
    monkeypatch.setattr(lifecycle, "CANCEL_GRACE", 1.5)
    t = cancel_when_stubborn_speaks(session, events, 1)
    code = session.invoke(manifest, "stubborn", ["60"])
    t.join()
    assert code == 130
    assert ("dev.dock.probe", "warn", "did not stop on cancel; stopped it") in events
    assert session.procs == []  # killed, not kept for reuse


def test_second_cancel_ends_the_grace_period_early(run, monkeypatch):
    # bug: only Ctrl-C ends the grace early; a host calling cancel() twice waits out the whole period
    session, events, manifest = run
    monkeypatch.setattr(lifecycle, "CANCEL_GRACE", 60)
    t = cancel_when_stubborn_speaks(session, events, 2)
    started = time.monotonic()
    code = session.invoke(manifest, "stubborn", ["120"])
    t.join()
    assert code == 130
    assert time.monotonic() - started < 30
    assert ("dev.dock.probe", "warn", "did not stop on cancel; stopped it") in events
