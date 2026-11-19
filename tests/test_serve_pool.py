"""The warm pool of `dock serve`: Sessions are reused per (plugin, cwd), and closed when they should be."""

import io
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from cli import serve  # noqa: E402
from cli.store import Store  # noqa: E402
import dockenv  # noqa: E402
from dockenv import container_running, kill_container, wait_container_gone  # noqa: E402
from serve_client import GRACE, ServeCase, make_home, require_images  # noqa: E402


class PoolOverServe(ServeCase):
    """Reuse as a client sees it: which container served each call."""

    def setUp(self):
        super().setUp()
        self.cwd = tempfile.mkdtemp(prefix="dock-pool-a-")
        self.other_cwd = tempfile.mkdtemp(prefix="dock-pool-b-")

    def call(self, c, command, *args, cwd=None):
        return c.start("run", {"command": command, "args": list(args), "cwd": cwd or self.cwd})

    def cid(self, c, *args, command="pid", cwd=None):
        """Run a call to the end and return the cid of the process that served it."""
        call = self.call(c, command, *args, cwd=cwd)
        reply = c.wait_reply(call)
        self.assertIn("result", reply)
        return reply["result"]["cid"]

    def test_runs_one_after_the_other_with_the_same_cwd_share_one_process(self):
        c = self.hello_client()
        first, second, third = (self.cid(c) for _ in range(3))
        self.assertEqual(first, second)
        self.assertEqual(second, third)

    def test_two_hello_runs_with_the_same_cwd_start_one_container_only(self):
        # AC16: the second run reuses the warm container; the runtime was asked for one `run`, not two
        wrapper, log = dockenv.recording_runtime(Path(tempfile.mkdtemp(prefix="dock-pool-rec-")))
        self.addCleanup(shutil.rmtree, wrapper.parent, True)
        dockenv.write_config(self.home, wrapper)
        c = self.hello_client()
        for name in ("Ada", "Bob"):
            reply = c.wait_reply(self.call(c, "hello", name))
            self.assertEqual(reply["result"]["exit_code"], 0, reply)
        self.assertEqual("".join(n["params"]["text"] for n in c.notes("output")), "hello, Ada!\nhello, Bob!\n")
        runs = [argv for argv in dockenv.calls_in(log) if argv[0] == "run"]
        self.assertEqual(len(runs), 1, runs)

    def test_runs_with_different_cwds_get_different_processes(self):
        c = self.hello_client()
        a, b = self.cid(c, cwd=self.cwd), self.cid(c, cwd=self.other_cwd)
        self.assertNotEqual(a, b)
        self.assertEqual(self.cid(c, cwd=self.cwd), a)  # each cwd has its own warm one
        self.assertEqual(self.cid(c, cwd=self.other_cwd), b)
        self.assertTrue(container_running(a) and container_running(b))

    def test_the_plugin_is_told_the_cwd_of_the_call_it_serves(self):
        c = self.hello_client()
        call = self.call(c, "pid", cwd=self.other_cwd)
        self.assertEqual(c.wait_reply(call)["result"]["cwd"], self.other_cwd)

    def test_runs_at_the_same_time_get_two_processes_and_both_go_back_to_the_pool(self):
        c = self.hello_client()
        a, b = self.call(c, "sleep", "1"), self.call(c, "sleep", "1")
        cids = {c.wait_reply(a)["result"]["cid"], c.wait_reply(b)["result"]["cid"]}
        self.assertEqual(len(cids), 2)
        both = [self.call(c, "sleep", "1"), self.call(c, "sleep", "1")]
        later = {c.wait_reply(x)["result"]["cid"] for x in both}
        self.assertEqual(later, cids)  # the same two warm processes served the next pair

    def test_a_run_after_a_cancel_gets_a_new_process(self):
        c = self.hello_client()
        warm = self.cid(c)
        victim = self.call(c, "sleep", "60")
        c.wait_until(lambda: c.notes("output", victim), what="the call to start")
        self.assertEqual(c.cid_of(victim), warm)  # it was served by the warm one
        c.notify("cancel", {"id": victim})
        self.assertEqual(c.wait_reply(victim)["error"]["code"], -32800)
        fresh = self.cid(c)
        self.assertNotEqual(fresh, warm)
        self.assertTrue(wait_container_gone(warm), "the cancelled call's process is still running")

    def test_a_run_after_a_crash_gets_a_new_process(self):
        c = self.hello_client()
        warm = self.cid(c)
        crashed = self.call(c, "crash")
        self.assertIn("error", c.wait_reply(crashed))
        self.assertNotEqual(self.cid(c), warm)

    def test_a_process_killed_while_idle_is_not_handed_out_again(self):
        c = self.hello_client()
        warm = self.cid(c)
        kill_container(warm)
        self.assertTrue(wait_container_gone(warm))
        self.assertTrue(dockenv.wait_clients_gone(self.home), "dock was never going to notice: the client is still up")
        self.assertNotEqual(self.cid(c), warm)

    def test_an_idle_process_is_closed_after_the_idle_timeout_and_the_next_run_starts_afresh(self):
        dockenv.add_config(self.home, "[serve]\nidle_timeout = 1\n")
        c = self.hello_client()
        old = self.cid(c)
        time.sleep(2)
        fresh = self.cid(c)
        self.assertNotEqual(fresh, old)
        self.assertTrue(wait_container_gone(old), "the idle process is still running")

    def test_shutdown_stops_every_pooled_process(self):
        c = self.hello_client()
        cids = [self.cid(c, cwd=self.cwd), self.cid(c, cwd=self.other_cwd)]
        self.assertEqual(c.request("shutdown", timeout=float(GRACE) + 10)["result"], {})
        self.assertEqual(c.wait(), 0)
        for cid in cids:
            self.assertFalse(container_running(cid))

    def test_end_of_stdin_stops_every_pooled_process_too(self):
        c = self.hello_client()
        cids = [self.cid(c, cwd=self.cwd), self.cid(c, cwd=self.other_cwd)]
        c.close_stdin()
        self.assertEqual(c.wait(), 0)
        for cid in cids:
            self.assertFalse(container_running(cid))

    def test_a_stray_message_from_a_pooled_process_with_no_call_does_not_hurt_serve(self):
        c = self.hello_client()
        self.cid(c, command="late-log")  # the plugin sends a log 0.3 s after answering
        time.sleep(1)
        self.assertEqual(c.request("shutdown", timeout=float(GRACE) + 10)["result"], {})
        self.assertEqual(c.wait(), 0)
        self.assertEqual(c.bad_lines, [])
        self.assertEqual(c.notes("log"), [])  # it belonged to no call: nothing is sent to the client
        self.assertIn(b"late message", c.stderr)


class FakeClientUI:
    """Stands in for a call's ClientUI: the pool only needs to hand it to the Session."""

    def __init__(self, texts):
        self.texts = texts

    def output(self, p):
        self.texts.append(p.get("text"))

    def log(self, p):
        pass

    def progress(self, p):
        pass

    def prompt_user(self, p):
        return {"eof": True}

    def warn(self, message):
        pass


class PoolUnit(unittest.TestCase):
    """The Pool itself, with real Sessions and plugin containers."""

    @classmethod
    def setUpClass(cls):
        require_images()

    def setUp(self):
        self.home = make_home("dock-pool-unit-")
        self.store = Store(self.home)
        self.manifest = self.store.get("dev.dock.pidprobe")
        self.cwd = tempfile.mkdtemp(prefix="dock-pool-unit-cwd-")
        self.pools = []

    def pool(self, **kw):
        pool = serve.Pool(self.store, False, **kw)
        self.pools.append(pool)
        self.addCleanup(pool.close)
        return pool

    def run_once(self, pool, cwd=None, command="pid", args=(), reusable=True):
        """Lease, run to the end, release. Returns (session, cid)."""
        session = pool.lease(self.manifest.id, cwd or self.cwd, FakeClientUI([]))
        result = session.invoke_result(self.manifest, command, list(args))
        proc = session.commands[self.manifest.id].proc
        pool.release(session, reusable and session.commands[self.manifest.id].alive())
        return session, result["cid"], proc

    def test_a_released_session_is_leased_again_for_the_same_plugin_and_cwd(self):
        pool = self.pool()
        first, cid1, _ = self.run_once(pool)
        second, cid2, _ = self.run_once(pool)
        self.assertIs(first, second)
        self.assertEqual(cid1, cid2)

    def test_a_session_for_another_cwd_or_plugin_is_not_handed_out(self):
        pool = self.pool()
        first, _, _ = self.run_once(pool)
        other, _, _ = self.run_once(pool, cwd=tempfile.mkdtemp(prefix="dock-pool-unit-other-"))
        self.assertIsNot(first, other)

    def test_a_session_that_is_not_reusable_is_closed_on_release(self):
        pool = self.pool()
        first, cid, proc = self.run_once(pool, reusable=False)
        self.assertEqual(first.procs, [])
        self.assertTrue(wait_container_gone(cid))
        second, _, _ = self.run_once(pool)
        self.assertIsNot(first, second)

    def test_a_leased_session_is_never_handed_to_a_second_call(self):
        pool = self.pool()
        one = pool.lease(self.manifest.id, self.cwd, FakeClientUI([]))
        two = pool.lease(self.manifest.id, self.cwd, FakeClientUI([]))
        self.assertIsNot(one, two)

    def test_reap_closes_sessions_idle_longer_than_the_timeout_and_keeps_the_others(self):
        clock = [100.0]
        pool = self.pool(idle_timeout=10, reap_interval=3600, clock=lambda: clock[0])
        old, old_cid, _ = self.run_once(pool, cwd=self.cwd)
        clock[0] += 8
        recent, recent_cid, _ = self.run_once(pool, cwd=tempfile.mkdtemp(prefix="dock-pool-unit-r-"))
        clock[0] += 5  # old: idle 13 s, recent: idle 5 s
        pool.reap()
        self.assertTrue(wait_container_gone(old_cid))
        self.assertEqual(old.procs, [])
        self.assertTrue(container_running(recent_cid))

    def test_an_idle_timeout_of_zero_never_reaps(self):
        clock = [100.0]
        pool = self.pool(idle_timeout=0, reap_interval=3600, clock=lambda: clock[0])
        _, cid, _ = self.run_once(pool)
        clock[0] += 10**6
        pool.reap()
        self.assertTrue(container_running(cid))
        _, again, _ = self.run_once(pool)
        self.assertEqual(again, cid)

    def test_the_reaper_thread_closes_idle_sessions_by_itself(self):
        pool = self.pool(idle_timeout=0.5, reap_interval=0.1)
        _, cid, _ = self.run_once(pool)
        self.assertTrue(wait_container_gone(cid, timeout=5), "the reaper did not stop the idle process")

    def test_past_max_idle_the_oldest_idle_session_is_closed(self):
        pool = self.pool(max_idle=1)
        _, oldest, _ = self.run_once(pool, cwd=self.cwd)
        _, newest, _ = self.run_once(pool, cwd=tempfile.mkdtemp(prefix="dock-pool-unit-n-"))
        self.assertTrue(wait_container_gone(oldest))
        self.assertTrue(container_running(newest))

    def test_close_stops_idle_and_leased_sessions_and_refuses_new_leases(self):
        pool = self.pool()
        _, idle_cid, _ = self.run_once(pool, cwd=self.cwd)
        leased = pool.lease(self.manifest.id, tempfile.mkdtemp(prefix="dock-pool-unit-l-"), FakeClientUI([]))
        leased_cid = leased.invoke_result(self.manifest, "pid", [])["cid"]
        pool.close()
        self.assertTrue(wait_container_gone(idle_cid))
        self.assertTrue(wait_container_gone(leased_cid))
        from cli.process import PluginError

        with self.assertRaises(PluginError):
            pool.lease(self.manifest.id, self.cwd, FakeClientUI([]))
        pool.release(leased, True)  # a call that ends after close: closed, not pooled
        pool.close()  # and closing twice is harmless

    def test_messages_reach_the_ui_of_the_call_that_holds_the_session_not_the_previous_one(self):
        pool = self.pool()
        first_texts, second_texts = [], []
        session = pool.lease(self.manifest.id, self.cwd, FakeClientUI(first_texts))
        session.invoke_result(self.manifest, "pid", [])
        pool.release(session, True)
        again = pool.lease(self.manifest.id, self.cwd, FakeClientUI(second_texts))
        self.assertIs(again, session)
        again.invoke_result(self.manifest, "pid", [])
        self.assertEqual(len(first_texts), 1)
        self.assertEqual(len(second_texts), 1)


class RoutingUIUnit(unittest.TestCase):
    def test_a_message_with_no_call_goes_to_stderr_and_never_to_stdout(self):
        ui = serve.RoutingUI("dev.dock.x", serve.Slot())
        with mock.patch("sys.stdout", new=io.StringIO()) as out, mock.patch("sys.stderr", new=io.StringIO()) as err:
            ui.output({"text": "hi"})
            ui.log({"level": "info", "message": "late"})
            ui.progress({"percent": 5})
            ui.warn("careful")
        self.assertEqual(out.getvalue(), "")
        for text in ("hi", "late", "careful"):
            self.assertIn(text, err.getvalue())

    def test_a_prompt_with_no_call_is_answered_with_eof(self):
        self.assertEqual(serve.RoutingUI("dev.dock.x", serve.Slot()).prompt_user({"prompt": "?"}), {"eof": True})


class ServeIdleTimeoutConfig(unittest.TestCase):
    def store(self, toml=None):
        home = Path(tempfile.mkdtemp(prefix="dock-pool-config-"))
        if toml is not None:
            (home / "config.toml").write_text(toml)
        return Store(home)

    def test_default_is_300_seconds(self):
        self.assertEqual(self.store().serve_idle_timeout(), 300)

    def test_it_is_read_from_the_serve_table_and_zero_means_never(self):
        self.assertEqual(self.store("[serve]\nidle_timeout = 42\n").serve_idle_timeout(), 42)
        self.assertEqual(self.store("[serve]\nidle_timeout = 0\n").serve_idle_timeout(), 0)

    def test_the_shell_setting_is_separate(self):
        store = self.store("[shell]\nidle_timeout = 7\n")
        self.assertEqual(store.serve_idle_timeout(), 300)
        self.assertEqual(store.idle_timeout(), 7)

    def test_an_invalid_value_falls_back_to_300_and_is_reported_once(self):
        store = self.store('[serve]\nidle_timeout = "soon"\n')
        with mock.patch("sys.stderr", new=io.StringIO()) as err:
            self.assertEqual(store.serve_idle_timeout(), 300)
            self.assertEqual(store.serve_idle_timeout(), 300)
        self.assertEqual(err.getvalue().count("invalid [serve] idle_timeout"), 1)


if __name__ == "__main__":
    unittest.main()
