"""`dock serve` under stress: internal errors, SIGTERM while cleaning up, and the order of
reply and close. Run `make test`."""

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
from dockenv import container_running, wait_container_gone  # noqa: E402
from serve_client import GRACE, ServeCase, make_home, require_images  # noqa: E402
from test_serve_pool import FakeClientUI  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
PROBE = "dev.dock.pidprobe"


def slow_shutdown(home, seconds):
    """Make the pidprobe plugin take `seconds` to answer `shutdown` (see its fixture)."""
    (Store(home).data_dir(PROBE) / "slow-shutdown").write_text(str(seconds))


class ServeHardening(ServeCase):
    def setUp(self):
        super().setUp()
        self.cwd = tempfile.mkdtemp(prefix="dock-hardening-a-")
        self.other_cwd = tempfile.mkdtemp(prefix="dock-hardening-b-")

    def run_call(self, c, command, *args, cwd=None):
        return c.start("run", {"command": command, "args": list(args), "cwd": cwd or self.cwd})

    def started(self, c, call):
        c.wait_until(lambda: c.notes("output", call), what="the call to start")

    def test_an_unexpected_error_in_a_request_is_an_error_reply_and_serve_carries_on(self):
        c = self.hello_client()
        call = self.run_call(c, "pid")
        c.wait_reply(call)
        cid = c.cid_of(call)
        Path(self.home, "index.json").write_text("{ not json")
        reply = c.request("commands")
        self.assertEqual(reply["error"]["code"], -32603)
        self.assertEqual(c.wait_reply(self.run_call(c, "pid"))["error"]["code"], -32603)
        self.assertEqual(c.request("nope")["error"]["code"], -32601)  # still serving
        self.assertIn(b"JSONDecodeError", c.stderr)  # and the cause is logged
        c.close_stdin()
        self.assertEqual(c.wait(), 0)
        self.assertFalse(container_running(cid))

    def warm_two(self, c):
        cids = []
        for cwd in (self.cwd, self.other_cwd):
            call = self.run_call(c, "pid", cwd=cwd)
            c.wait_reply(call)
            cids.append(c.cid_of(call))
        return cids

    def test_sigterm_while_cleaning_up_after_end_of_input_still_finishes_the_cleanup(self):
        slow_shutdown(self.home, 1.2)
        c = self.hello_client()
        cids = self.warm_two(c)
        c.close_stdin()
        time.sleep(0.5)  # inside the first shutdown
        self.assertTrue(any(container_running(p) for p in cids), "the close was not slow")
        c.proc.send_signal(signal.SIGTERM)
        self.assertEqual(c.wait(timeout=30), 0)
        for cid in cids:
            self.assertFalse(container_running(cid))

    def test_sigterm_while_cleaning_up_after_shutdown_still_finishes_the_cleanup(self):
        slow_shutdown(self.home, 1.2)
        c = self.hello_client()
        cids = self.warm_two(c)
        c.start("shutdown")
        time.sleep(0.5)
        self.assertTrue(any(container_running(p) for p in cids), "the close was not slow")
        c.proc.send_signal(signal.SIGTERM)
        self.assertEqual(c.wait(timeout=30), 0)
        for cid in cids:
            self.assertFalse(container_running(cid))

    def test_a_cancelled_call_is_answered_before_its_slow_close_is_done(self):
        slow_shutdown(self.home, 6)
        c = self.hello_client()
        call = self.run_call(c, "sleep", "60")
        self.started(c, call)
        cid = c.cid_of(call)
        began = time.monotonic()
        c.notify("cancel", {"id": call})
        self.assertEqual(c.wait_reply(call)["error"]["code"], -32800)
        self.assertLess(time.monotonic() - began, 4)  # not 6 s of shutdown later
        time.sleep(0.5)
        self.assertTrue(container_running(cid), "the close was not slow: the plugin is already gone")
        self.assertTrue(wait_container_gone(cid, timeout=20), "the close still happens, after the reply")

    def test_the_host_asks_a_plugin_only_to_initialize_invoke_and_shutdown(self):
        c = self.hello_client()
        call = self.run_call(c, "pid")
        c.wait_reply(call)
        c.close_stdin()
        self.assertEqual(c.wait(), 0)
        log = (Store(self.home).data_dir(PROBE) / "methods.log").read_text().split()
        self.assertEqual(log, ["initialize", "invoke", "shutdown"])  # never provision or unprovision



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
    @classmethod
    def setUpClass(cls):
        require_images()

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
        """Lease, run to the end, release as reusable; returns (session, cid)."""
        session = pool.lease(PROBE, cwd or tempfile.mkdtemp(prefix="dock-hardening-cwd-"), FakeClientUI([]))
        result = session.invoke_result(self.manifest, "pid", [])
        pool.release(session, True)
        return session, result["cid"]

    def test_close_can_be_resumed_after_an_interrupt_and_still_stops_the_rest(self):
        pool = self.pool()
        cids = [self.run_once(pool)[1] for _ in range(3)]
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
        for cid in cids:
            self.assertTrue(wait_container_gone(cid), f"{cid} was left running")

    def test_close_waits_for_a_session_the_reaper_is_closing(self):
        clock = [100.0]
        pool = self.pool(idle_timeout=10, reap_interval=3600, clock=lambda: clock[0])
        _, cid = self.run_once(pool)
        clock[0] += 60
        real_close = Session.close

        def slow_close(session):  # a close that takes a while, so the reaper is still in it when close() comes
            time.sleep(1.5)
            real_close(session)

        reaper = threading.Thread(target=pool.reap)
        with mock.patch.object(Session, "close", slow_close):
            reaper.start()
            time.sleep(0.4)  # the reaper is in the middle of closing it
            pool.close()
        self.assertFalse(container_running(cid), "close returned while the reaper was still closing a session")
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
