"""`dock serve` under stress: cancel reaching a capability call, shared markers, internal errors, SIGTERM while
cleaning up, and the order of reply and close. Run `make test`."""

import io
import json
import os
import signal
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

from cli import serve  # noqa: E402
from cli.lifecycle import Session  # noqa: E402
from cli.store import Store  # noqa: E402
from serve_client import GRACE, ServeCase, make_home, pid_alive, wait_gone  # noqa: E402
from test_serve_pool import FakeClientUI  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
PROBE = "dev.dock.pidprobe"


def slow_unprovision(home, seconds):
    """Make the pidprobe plugin take `seconds` to unprovision (see its fixture)."""
    (Store(home).data_dir(PROBE) / "slow-unprovision").write_text(str(seconds))


def state_files(home):
    return sorted(p.name for p in Path(home, "state").iterdir())


class ServeHardening(ServeCase):
    def setUp(self):
        super().setUp()
        store = Store(self.home)
        for name in ("capcaller", "capprovider"):
            store.install(FIXTURES / name)
        self.cwd = tempfile.mkdtemp(prefix="dock-hardening-a-")
        self.other_cwd = tempfile.mkdtemp(prefix="dock-hardening-b-")

    def run_call(self, c, command, *args, cwd=None):
        return c.start("run", {"command": command, "args": list(args), "cwd": cwd or self.cwd})

    def started(self, c, call):
        c.wait_until(lambda: c.notes("output", call), what="the call to start")

    def test_cancel_reaches_a_command_that_is_blocked_on_a_capability_provider(self):
        c = self.hello_client()
        call = self.run_call(c, "callcap")
        c.wait_until(lambda: "tick" in c.output_text(call), what="the provider to stream")
        began = time.monotonic()
        c.notify("cancel", {"id": call})
        reply = c.wait_reply(call, timeout=float(GRACE) + 6)
        self.assertEqual(reply["error"]["code"], -32800)
        self.assertLess(time.monotonic() - began, float(GRACE) + 1)

    # finding 2 ------------------------------------------------------------------
    def test_a_cancelled_call_does_not_remove_the_marker_of_a_call_that_is_still_provisioned(self):
        c = self.hello_client()
        keep = self.run_call(c, "sleep", "60", cwd=self.cwd)
        victim = self.run_call(c, "sleep", "60", cwd=self.other_cwd)
        self.started(c, keep)
        self.started(c, victim)
        victim_pid = c.pid_of(victim)
        c.notify("cancel", {"id": victim})
        self.assertEqual(c.wait_reply(victim)["error"]["code"], -32800)
        self.assertTrue(wait_gone(victim_pid))
        time.sleep(0.5)  # past the end of the victim's close
        self.assertEqual(state_files(self.home), [f"{PROBE}.provisioned"])
        c.close_stdin()
        self.assertEqual(c.wait(timeout=float(GRACE) + 6), 0)
        self.assertEqual(state_files(self.home), [])

    # finding 3 ------------------------------------------------------------------
    def test_an_unexpected_error_in_a_request_is_an_error_reply_and_serve_carries_on(self):
        c = self.hello_client()
        call = self.run_call(c, "pid")
        c.wait_reply(call)
        pid = c.pid_of(call)
        Path(self.home, "index.json").write_text("{ not json")
        reply = c.request("commands")
        self.assertEqual(reply["error"]["code"], -32603)
        self.assertEqual(c.wait_reply(self.run_call(c, "pid"))["error"]["code"], -32603)
        self.assertEqual(c.request("nope")["error"]["code"], -32601)  # still serving
        self.assertIn(b"JSONDecodeError", c.stderr)  # and the cause is logged
        c.close_stdin()
        self.assertEqual(c.wait(), 0)
        self.assertFalse(pid_alive(pid))
        self.assertEqual(state_files(self.home), [])

    # finding 4 ------------------------------------------------------------------
    def warm_two(self, c):
        pids = []
        for cwd in (self.cwd, self.other_cwd):
            call = self.run_call(c, "pid", cwd=cwd)
            c.wait_reply(call)
            pids.append(c.pid_of(call))
        return pids

    def test_sigterm_while_cleaning_up_after_end_of_input_still_finishes_the_cleanup(self):
        slow_unprovision(self.home, 1.2)
        c = self.hello_client()
        pids = self.warm_two(c)
        c.close_stdin()
        time.sleep(0.5)  # inside the first unprovision
        c.proc.send_signal(signal.SIGTERM)
        self.assertEqual(c.wait(timeout=20), 0)
        for pid in pids:
            self.assertFalse(pid_alive(pid))
        self.assertEqual(state_files(self.home), [])

    def test_sigterm_while_cleaning_up_after_shutdown_still_finishes_the_cleanup(self):
        slow_unprovision(self.home, 1.2)
        c = self.hello_client()
        pids = self.warm_two(c)
        c.start("shutdown")
        time.sleep(0.5)
        c.proc.send_signal(signal.SIGTERM)
        self.assertEqual(c.wait(timeout=20), 0)
        for pid in pids:
            self.assertFalse(pid_alive(pid))
        self.assertEqual(state_files(self.home), [])

    # finding 5 ------------------------------------------------------------------
    def test_a_cancelled_call_is_answered_before_its_slow_close_is_done(self):
        slow_unprovision(self.home, 3)
        c = self.hello_client()
        call = self.run_call(c, "sleep", "60")
        self.started(c, call)
        began = time.monotonic()
        c.notify("cancel", {"id": call})
        self.assertEqual(c.wait_reply(call)["error"]["code"], -32800)
        self.assertLess(time.monotonic() - began, 1.5)  # not 3 s of unprovision later
        c.wait_until(lambda: not Path(self.home, "state", f"{PROBE}.provisioned").exists(), timeout=10,
                     what="the close to finish")  # the close still happens, after the reply


class InternalErrors(unittest.TestCase):
    def test_the_pool_is_closed_when_the_loop_dies_of_a_bug(self):
        home = make_home("dock-hardening-loop-")
        hello = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "hello", "params": {}}).encode() + b"\n"
        lines = iter([hello])

        def read_line(stdin):
            try:
                return next(lines).rstrip(b"\n")
            except StopIteration:
                raise RuntimeError("boom") from None

        with mock.patch.object(serve, "read_line", read_line), \
                mock.patch.object(serve.Server, "close", autospec=True, side_effect=serve.Server.close) as closer:
            with self.assertRaises(RuntimeError):
                serve.serve(Store(home), stdin=io.BytesIO(), stdout=io.BytesIO())
        closer.assert_called_once()

    def test_a_handler_that_raises_is_answered_with_internal_error_and_the_loop_goes_on(self):
        home = make_home("dock-hardening-handle-")
        out = io.BytesIO()
        requests = b"".join(
            (json.dumps({"jsonrpc": "2.0", **m}) + "\n").encode() for m in (
                {"id": 1, "method": "hello", "params": {}},
                {"id": 2, "method": "commands"},
                {"id": 3, "method": "shutdown"},
            ))

        def boom(self, msg_id, is_request):
            raise RuntimeError("boom")

        with mock.patch.object(serve.Server, "_commands", boom), mock.patch("sys.stderr", new=io.StringIO()) as err:
            code = serve.serve(Store(home), stdin=io.BytesIO(requests), stdout=out)
        self.assertEqual(code, 0)
        replies = {m["id"]: m for m in map(json.loads, out.getvalue().splitlines())}
        self.assertEqual(replies[2]["error"]["code"], -32603)
        self.assertEqual(replies[3]["result"], {})  # the shutdown after it was still served
        self.assertIn("boom", err.getvalue())


class PoolHardening(unittest.TestCase):
    def setUp(self):
        self.home = make_home("dock-hardening-pool-")
        self.store = Store(self.home)
        self.manifest = self.store.get(PROBE)
        self.pools = []

    def pool(self, **kw):
        pool = serve.Pool(self.store, False, **kw)
        self.pools.append(pool)
        self.addCleanup(pool.close)
        return pool

    def run_once(self, pool, cwd=None):
        """Lease, run to the end, release as reusable; returns (session, pid)."""
        session = pool.lease(PROBE, cwd or tempfile.mkdtemp(prefix="dock-hardening-cwd-"), FakeClientUI([]))
        result = session.invoke_result(self.manifest, "pid", [])
        pool.release(session, True)
        return session, result["pid"]

    def test_close_can_be_resumed_after_an_interrupt_and_still_stops_the_rest(self):
        pool = self.pool()
        pids = [self.run_once(pool)[1] for _ in range(3)]
        real_close = Session.close
        interrupted = []

        def close(session):
            real_close(session)
            if not interrupted:
                interrupted.append(session)
                raise KeyboardInterrupt  # SIGTERM landing inside the first close

        with mock.patch.object(Session, "close", close):
            with self.assertRaises(KeyboardInterrupt):
                pool.close()
            pool.close()  # run again, as the server does
        for pid in pids:
            self.assertTrue(wait_gone(pid), f"{pid} was left running")

    def test_close_waits_for_a_session_the_reaper_is_closing(self):
        slow_unprovision(self.home, 1.5)
        clock = [100.0]
        pool = self.pool(idle_timeout=10, reap_interval=3600, clock=lambda: clock[0])
        _, pid = self.run_once(pool)
        clock[0] += 60
        reaper = threading.Thread(target=pool.reap)
        reaper.start()
        time.sleep(0.4)  # the reaper is in the middle of closing it
        pool.close()
        self.assertFalse(pid_alive(pid), "close returned while the reaper was still closing a session")
        self.assertEqual(state_files(self.home), [])
        reaper.join(5)

    def test_a_session_that_is_closing_after_its_call_is_closed_only_after_the_reply_hook_ran(self):
        pool = self.pool()
        session = pool.lease(PROBE, tempfile.mkdtemp(prefix="dock-hardening-cwd-"), FakeClientUI([]))
        session.invoke_result(self.manifest, "pid", [])
        seen = []
        pool.release(session, False, before_close=lambda: seen.append(list(session.procs)))
        self.assertEqual(len(seen), 1)
        self.assertEqual(len(seen[0]), 1)  # still running when the hook (the reply) ran
        self.assertEqual(session.procs, [])  # and closed by the time release returns

    def test_a_reusable_session_is_back_in_the_pool_before_the_reply_hook_would_run(self):
        pool = self.pool()
        session = pool.lease(PROBE, tempfile.mkdtemp(prefix="dock-hardening-cwd-"), FakeClientUI([]))
        session.invoke_result(self.manifest, "pid", [])
        called = []
        pool.release(session, True, before_close=lambda: called.append(1))
        self.assertEqual(called, [])  # nothing to close: the caller replies after release returns
        again = pool.lease(PROBE, session.cwd, FakeClientUI([]))
        self.assertIs(again, session)


if __name__ == "__main__":
    unittest.main()
