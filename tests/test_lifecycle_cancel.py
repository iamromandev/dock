"""Cancelling an invoke from another thread: Session.invoke_result(cancel=) and PluginProcess.wait_for(stop=)."""

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import dockenv  # noqa: E402
from cli import lifecycle  # noqa: E402
from cli.inputs import UsageError  # noqa: E402
from cli.lifecycle import Session  # noqa: E402
from cli.process import CANCELLED, Interrupted, PluginError  # noqa: E402
from cli.store import Store  # noqa: E402


class QuietUI:
    """Keeps the output text and swallows the rest, so a test can read the container id the plugin printed."""

    def __init__(self, plugin_id, texts):
        self.texts = texts

    def output(self, p):
        self.texts.append(p.get("text", ""))

    def log(self, p):
        pass

    def progress(self, p):
        pass

    def prompt_user(self, p):
        return {"eof": True}

    def warn(self, message):
        pass

    def handlers(self):
        return {"output": self.output, "log": self.log, "progress": self.progress, "prompt_user": self.prompt_user}


PIDPROBE = ROOT / "tests" / "fixtures" / "pidprobe"


def store_with(prefix, *plugin_dirs):
    return Store(dockenv.make_home(prefix, *plugin_dirs))


class InvokeResultCancel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        dockenv.prebuild_plugins(PIDPROBE)

    def setUp(self):
        self.store = store_with("dock-cancel-", PIDPROBE)
        self.manifest = self.store.get("dev.dock.pidprobe")
        self.texts = []
        self.session = Session(self.store, ui_factory=lambda pid: QuietUI(pid, self.texts))
        self.addCleanup(self.session.close)
        self.cancel = threading.Event()

    def cid(self):
        return "".join(self.texts).split(" cid ")[1].split()[0]

    def cancel_once_started(self):
        """Set the cancel event once the plugin has printed (so it runs in its container); note when."""
        def watch():
            end = time.monotonic() + 60
            while " cid " not in "".join(self.texts) and time.monotonic() < end:
                time.sleep(0.02)
            self.cancelled_at = time.monotonic()
            self.cancel.set()

        threading.Thread(target=watch, daemon=True).start()

    def test_returns_the_whole_result_including_extra_fields(self):
        project = tempfile.mkdtemp(prefix="dock-cancel-cwd-")
        self.session.cwd = project
        result = self.session.invoke_result(self.manifest, "pid", [])
        self.assertEqual({k: result[k] for k in ("exit_code", "cid", "cwd")},
                         {"exit_code": 0, "cid": self.cid(), "cwd": project})

    def test_invoke_still_returns_just_the_exit_code(self):
        self.assertEqual(self.session.invoke(self.manifest, "pid", []), 0)

    def test_a_usage_error_is_raised_before_anything_starts(self):
        with self.assertRaises(UsageError):
            self.session.invoke_result(self.manifest, "sleep", [])
        self.assertEqual(self.session.procs, [])

    def test_unset_event_changes_nothing(self):
        result = self.session.invoke_result(self.manifest, "work", ["2"], cancel=self.cancel)
        self.assertEqual(result["exit_code"], 0)

    def test_event_set_during_the_call_cancels_it_and_no_process_is_left(self):
        self.cancel_once_started()
        with self.assertRaises(PluginError) as caught:
            self.session.invoke_result(self.manifest, "sleep", ["30"], cancel=self.cancel)
        self.assertEqual(caught.exception.code, CANCELLED)
        self.assertLess(time.monotonic() - self.cancelled_at, lifecycle.CANCEL_GRACE + 5)
        cid = self.cid()
        self.session.close()
        self.assertTrue(dockenv.wait_container_gone(cid), "the plugin container is still running")

    def test_a_plugin_that_ignores_cancel_is_killed_after_the_grace_period(self):
        self.cancel_once_started()
        with mock.patch.object(lifecycle, "CANCEL_GRACE", 0.5):
            with self.assertRaises(PluginError) as caught:
                self.session.invoke_result(self.manifest, "stubborn", ["30"], cancel=self.cancel)
        self.assertEqual(caught.exception.code, CANCELLED)
        self.assertLess(time.monotonic() - self.cancelled_at, 0.5 + 5)
        self.assertTrue(dockenv.wait_container_gone(self.cid()), "the stuck plugin was not killed")
        self.assertEqual(self.session.procs, [])  # force-stopped: nothing to reuse

    def test_an_event_that_is_already_set_cancels_at_once(self):
        self.cancel.set()
        started = time.monotonic()
        with self.assertRaises(PluginError) as caught:
            self.session.invoke_result(self.manifest, "sleep", ["30"], cancel=self.cancel)
        self.assertEqual(caught.exception.code, CANCELLED)
        self.assertLess(time.monotonic() - started, lifecycle.CANCEL_GRACE + 5)

    def test_a_crash_is_an_error_not_a_hang(self):
        with self.assertRaises(PluginError) as caught:
            self.session.invoke_result(self.manifest, "crash", [], cancel=self.cancel)
        self.assertNotEqual(caught.exception.code, CANCELLED)
        self.assertEqual(self.session.procs, [])  # the dead process was dropped

    def test_the_session_serves_another_call_after_a_normal_one(self):
        first = self.session.invoke_result(self.manifest, "pid", [], cancel=self.cancel)
        second = self.session.invoke_result(self.manifest, "pid", [], cancel=self.cancel)
        self.assertEqual(first["cid"], second["cid"])


class WaitForStop(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        dockenv.prebuild_plugins(PIDPROBE)

    def setUp(self):
        self.store = store_with("dock-stop-", PIDPROBE)
        self.manifest = self.store.get("dev.dock.pidprobe")
        self.session = Session(self.store, ui_factory=lambda pid: QuietUI(pid, []))
        self.addCleanup(self.session.close)

    def start_sleep(self, seconds="30"):
        proc = self.session.start(self.manifest, self.session.ui_factory(self.manifest.id))
        params = {"command": "sleep", "args": [seconds], "cwd": os.getcwd(), "inputs": {"seconds": seconds}}
        req = proc.send_request("invoke", params)
        self.addCleanup(proc.notify, "cancel")  # so the close that follows does not wait for the sleep
        return proc, req

    def test_raises_interrupted_soon_after_stop_turns_true(self):
        proc, req = self.start_sleep()
        stop = threading.Event()
        threading.Timer(0.3, stop.set).start()
        started = time.monotonic()
        with self.assertRaises(Interrupted) as caught:
            proc.wait_for(req, "invoke", timeout=None, stop=stop.is_set)
        self.assertEqual(caught.exception.code, CANCELLED)
        self.assertIsInstance(caught.exception, PluginError)
        self.assertLess(time.monotonic() - started, 3.0)

    def test_a_reply_that_is_already_there_wins_over_stop(self):
        proc, req = self.start_sleep("0")
        deadline = time.monotonic() + 5
        while (proc, req) not in self.session.hub.replies and time.monotonic() < deadline:
            self.session.hub.pump(0.1)
        result = proc.wait_for(req, "invoke", timeout=None, stop=lambda: True)
        self.assertEqual(result["exit_code"], 0)

    def test_the_timeout_still_applies_with_stop(self):
        proc, req = self.start_sleep()
        with self.assertRaises(PluginError) as caught:
            proc.wait_for(req, "invoke", timeout=0.3, stop=lambda: False)
        self.assertNotIsInstance(caught.exception, Interrupted)
        self.assertIn("did not answer", str(caught.exception))


class CancelTestLifetime(unittest.TestCase):
    """The running call's cancel test is only kept while the call runs."""

    @classmethod
    def setUpClass(cls):
        dockenv.prebuild_plugins(ROOT / "plugin" / "hello")

    def setUp(self):
        self.store = store_with("dock-capcancel-", ROOT / "plugin" / "hello")
        self.session = Session(self.store, ui_factory=lambda pid: QuietUI(pid, []))
        self.addCleanup(self.session.close)

    def test_the_cancel_test_is_forgotten_when_the_call_ends(self):
        self.session.invoke_result(self.store.get("dev.dock.hello"), "count", ["1"], cancel=threading.Event())
        self.assertIsNone(self.session._stop)


class CountingLog:
    """Stands in for a plugin's log file and counts how often it is closed."""

    def __init__(self, real):
        self.real = real
        self.closed_times = 0
        self.lock = threading.Lock()

    def close(self):
        with self.lock:
            self.closed_times += 1
        self.real.close()

    def __getattr__(self, name):
        return getattr(self.real, name)


class StopTwice(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        dockenv.prebuild_plugins(PIDPROBE)

    def setUp(self):
        self.store = store_with("dock-stoptwice-", PIDPROBE)
        self.manifest = self.store.get("dev.dock.pidprobe")
        self.session = Session(self.store, ui_factory=lambda pid: QuietUI(pid, []))
        self.addCleanup(self.session.close)
        self.proc = self.session.start(self.manifest, self.session.ui_factory(self.manifest.id))
        self.log = CountingLog(self.proc._log)
        self.proc._log = self.log

    def test_stopping_and_killing_one_after_the_other_tears_down_once(self):
        self.proc.stop()
        self.proc.stop()
        self.proc.kill()
        self.assertEqual(self.log.closed_times, 1)

    def test_a_stop_and_a_kill_from_two_threads_tear_down_once_and_neither_fails(self):
        errors = []

        def run(fn):
            try:
                fn()
            except BaseException as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=run, args=(fn,)) for fn in (self.proc.stop, self.proc.kill, self.proc.stop)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(errors, [])
        self.assertEqual(self.log.closed_times, 1)
        self.assertFalse(self.proc.alive())

    def test_stop_proc_from_two_threads_leaves_nothing_registered(self):
        threads = [threading.Thread(target=self.session.stop_proc, args=(self.proc,)) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(self.session.procs, [])


if __name__ == "__main__":
    unittest.main()
