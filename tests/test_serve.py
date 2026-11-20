"""`dock serve`: the JSON-RPC host mode. Real `python -m cli serve` children over pipes. Run `make test`."""

import io
import os
import signal
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from cli import SERVE_VERSION, __version__, serve  # noqa: E402
from cli.store import Store  # noqa: E402
from dockenv import container_running, kill_container, wait_container_gone  # noqa: E402
from serve_client import GRACE, ServeCase, make_home, require_images  # noqa: E402


class ServeSkeleton(ServeCase):
    def test_hello_returns_the_serve_and_dock_versions(self):
        c = self.client()
        self.assertEqual(c.hello(), {"serve_version": SERVE_VERSION, "dock_version": __version__})
        self.assertEqual(SERVE_VERSION, "1")

    def test_anything_before_hello_is_refused(self):
        c = self.client()
        reply = c.request("commands")
        self.assertEqual(reply["error"]["code"], -32600)
        self.assertEqual(reply["error"]["message"], "hello first")
        c.hello()  # and the connection is still usable
        self.assertIn("commands", c.request("commands")["result"])

    def test_commands_lists_installed_commands_with_plugin_and_inputs(self):
        c = self.hello_client()
        commands = {cmd["name"]: cmd for cmd in c.request("commands")["result"]["commands"]}
        sleep = commands["sleep"]
        self.assertEqual(sleep["plugin"], "dev.dock.pidprobe")
        self.assertEqual(sleep["inputs"][0], {"name": "seconds", "type": "string", "required": True})
        self.assertEqual(commands["hello"]["plugin"], "dev.dock.hello")
        self.assertEqual(commands["count"]["inputs"], [{"name": "n", "type": "int", "required": False}])

    def test_a_bad_line_is_refused_and_the_connection_carries_on(self):
        for name, line, code, wait_for, carries_on in (
                ("not json", b"this is not json\n", -32700, "the parse error", True),
                ("over one megabyte", b'{"id": 1, "method": "' + b"x" * (1024 * 1024 + 10) + b'"}\n', -32700,
                 "the too-long error", True),
                ("json value that is not an object", b"[1, 2]\n", -32600, "the invalid request error", False)):
            with self.subTest(name):
                c = self.hello_client()  # fresh: `None in c.replies` stays true once a case has run
                c.send_raw(line)
                c.wait_until(lambda: None in c.replies, what=wait_for)
                self.assertEqual(c.replies[None]["error"]["code"], code)
                if carries_on:
                    self.assertIn("commands", c.request("commands")["result"])

    def test_an_unknown_method_is_not_found(self):
        c = self.hello_client()
        self.assertEqual(c.request("frobnicate")["error"]["code"], -32601)

    def test_shutdown_is_answered_and_dock_exits_zero(self):
        c = self.hello_client()
        self.assertEqual(c.request("shutdown")["result"], {})
        self.assertEqual(c.wait(), 0)
        self.assertEqual(c.bad_lines, [])


class ServeRun(ServeCase):
    def setUp(self):
        super().setUp()
        self.cwd = tempfile.mkdtemp(prefix="dock-serve-cwd-")

    def run_call(self, c, command, *args, cwd=None):
        return c.start("run", {"command": command, "args": list(args), "cwd": cwd or self.cwd})

    def starts(self, plugin_id):
        log = Path(self.home, "logs", f"{plugin_id}.log")
        return log.read_text().count(" start ") if log.exists() else 0

    # refused before anything starts (R3) -------------------------------------
    def test_a_missing_required_input_is_an_error_and_no_plugin_starts(self):
        c = self.hello_client()
        before = self.starts("dev.dock.pidprobe")
        error = c.wait_reply(self.run_call(c, "sleep"))["error"]
        self.assertEqual(error["code"], -32602)
        self.assertEqual(error["message"], "missing required input 'seconds'")
        self.assertEqual(self.starts("dev.dock.pidprobe"), before)

    def test_an_unknown_command_is_not_found(self):
        c = self.hello_client()
        self.assertEqual(c.wait_reply(self.run_call(c, "nonesuch"))["error"]["code"], -32601)

    def test_bad_run_params_are_refused(self):
        c = self.hello_client()
        for cwd in ("relative/dir", os.path.join(self.cwd, "missing"), 7, None):
            with self.subTest(cwd=cwd):
                req = c.start("run", {"command": "pid", "args": [], "cwd": cwd})
                self.assertEqual(c.wait_reply(req)["error"]["code"], -32602)
        # args must be a list of strings, and inputs are reserved
        for params in ({"command": "pid", "args": "x", "cwd": self.cwd},
                       {"command": "pid", "args": [1], "cwd": self.cwd},
                       {"command": 5, "args": [], "cwd": self.cwd},
                       {"command": "pid", "args": [], "cwd": self.cwd, "inputs": {}}):
            with self.subTest(params=params):
                self.assertEqual(c.wait_reply(c.start("run", params))["error"]["code"], -32602)

    def test_a_run_id_that_is_already_running_is_refused_and_the_first_call_is_unharmed(self):
        c = self.hello_client()
        first = self.run_call(c, "sleep", "30")
        c.wait_until(lambda: c.notes("output", first), what="the first call to start")
        c.send({"id": first, "method": "run", "params": {"command": "pid", "args": [], "cwd": self.cwd}})
        c.wait_until(lambda: first in c.replies, what="the duplicate error")  # the first call is still asleep
        self.assertEqual(c.replies[first]["error"]["code"], -32600)
        c.notify("cancel", {"id": first})  # the real call is still there to cancel
        c.wait_until(lambda: c.replies[first]["error"]["code"] == -32800, what="the first call to end")

    # results and notifications (R4, R5, R10, R11) ------------------------------
    def test_progress_output_and_result_are_tagged_with_the_call_id(self):
        c = self.hello_client()
        call = self.run_call(c, "work", "3", "--delay", "0.05")
        reply = c.wait_reply(call)
        result = reply["result"]  # the plugin's own extra fields ride along; its pid is whatever the container gives it
        self.assertEqual({k: result[k] for k in ("exit_code", "cid", "cwd")},
                         {"exit_code": 0, "cid": c.cid_of(call), "cwd": self.cwd})
        progress = c.notes("progress", call)
        self.assertEqual([round(n["params"]["percent"], 1) for n in progress], [33.3, 66.7, 100.0])
        self.assertEqual(progress[0]["params"]["message"], "1/3")
        self.assertEqual(len(c.notes("progress")), 3)  # and nothing came untagged

    def test_two_calls_keep_their_notifications_apart(self):
        c = self.hello_client()
        a, b = self.run_call(c, "work", "4"), self.run_call(c, "work", "2")
        c.wait_reply(a)
        c.wait_reply(b)
        self.assertEqual(len(c.notes("progress", a)), 4)
        self.assertEqual(len(c.notes("progress", b)), 2)
        self.assertNotEqual(c.cid_of(a), c.cid_of(b))  # a process per call that runs

    def test_a_plugin_error_comes_back_with_its_code(self):
        c = self.hello_client()
        error = c.wait_reply(self.run_call(c, "crash"))["error"]
        self.assertEqual(error["code"], -32000)
        self.assertIn("exited during 'invoke'", error["message"])
        again = self.run_call(c, "pid")  # serve is unharmed
        self.assertEqual(c.wait_reply(again)["result"]["exit_code"], 0)

    # cancel (R6, R7) ---------------------------------------------------------
    def test_cancelling_one_of_three_calls_leaves_the_others_and_leaves_no_process(self):
        c = self.hello_client()
        keep1 = self.run_call(c, "work", "20", "--delay", "0.1")
        victim = self.run_call(c, "sleep", "60")
        keep2 = self.run_call(c, "work", "20", "--delay", "0.1")
        c.wait_until(lambda: all(c.notes("output", k) for k in (keep1, victim, keep2)), what="the calls to start")
        cids = [c.cid_of(k) for k in (keep1, victim, keep2)]
        self.assertEqual(len(set(cids)), 3)
        started = time.monotonic()
        c.notify("cancel", {"id": victim})
        error = c.wait_reply(victim, timeout=10)["error"]
        self.assertEqual(error["code"], -32800)
        self.assertLess(time.monotonic() - started, float(GRACE) + 5)
        for keep in (keep1, keep2):
            self.assertEqual(c.wait_reply(keep)["result"]["exit_code"], 0)
            self.assertEqual(len(c.notes("progress", keep)), 20)
        self.assertTrue(wait_container_gone(cids[1]), "the cancelled call's process is still running")
        c.close_stdin()
        self.assertEqual(c.wait(), 0)
        for cid in cids:
            self.assertTrue(wait_container_gone(cid), f"plugin process {cid} is still running")

    def test_a_plugin_that_ignores_cancel_is_killed_and_the_call_still_ends_in_time(self):
        c = self.hello_client()
        call = self.run_call(c, "stubborn", "60")
        c.wait_until(lambda: c.notes("output", call), what="the call to start")
        cid = c.cid_of(call)
        started = time.monotonic()
        c.notify("cancel", {"id": call})
        self.assertEqual(c.wait_reply(call, timeout=10)["error"]["code"], -32800)
        self.assertLess(time.monotonic() - started, float(GRACE) + 5)
        self.assertTrue(wait_container_gone(cid), "the stuck plugin is still running")

    # one call's death is only its own (R7) -------------------------------------
    def test_a_killed_plugin_fails_its_own_call_only(self):
        c = self.hello_client()
        doomed = self.run_call(c, "sleep", "3")
        other = self.run_call(c, "sleep", "3")
        c.wait_until(lambda: c.notes("output", doomed) and c.notes("output", other), what="both to start")
        kill_container(c.cid_of(doomed))
        self.assertIn("error", c.wait_reply(doomed))
        self.assertEqual(c.wait_reply(other)["result"]["exit_code"], 0)

    # leaving (R8) ----------------------------------------------------------------
    def test_leaving_cancels_the_calls_and_leaves_nothing_behind(self):
        def close_stdin(c, calls):
            c.close_stdin()

        def shutdown(c, calls):
            self.assertEqual(c.request("shutdown", timeout=float(GRACE) + 10)["result"], {})
            self.assertEqual(c.replies[calls[0]]["error"]["code"], -32800)  # the call was answered before shutdown was

        def sigterm(c, calls):
            c.proc.send_signal(signal.SIGTERM)

        for name, leave, count in (("closing stdin", close_stdin, 2), ("shutdown", shutdown, 1),
                                   ("sigterm", sigterm, 1)):
            with self.subTest(name):
                c = self.hello_client()
                calls = [self.run_call(c, "sleep", "60") for _ in range(count)]
                c.wait_until(lambda: all(c.notes("output", k) for k in calls), what="the calls to start")
                cids = [c.cid_of(k) for k in calls]
                started = time.monotonic()
                leave(c, calls)
                self.assertEqual(c.wait(timeout=float(GRACE) + 10), 0)
                self.assertLess(time.monotonic() - started, float(GRACE) + 10)
                for cid in cids:
                    self.assertFalse(container_running(cid), f"plugin process {cid} is still running")
                self.assertEqual(c.bad_lines, [])

    def test_sigint_does_not_stop_serve(self):
        c = self.hello_client()
        c.proc.send_signal(signal.SIGINT)  # a Ctrl-C aimed at the client's whole process group
        time.sleep(0.3)
        self.assertIsNone(c.proc.poll())
        self.assertIn("commands", c.request("commands")["result"])

    # permissions are the CLI's (NFR2) -----------------------------------------------


class RunIdReuse(unittest.TestCase):
    """The reply is out before the call is forgotten; the id must still be free the moment a client reads it."""

    @classmethod
    def setUpClass(cls):
        require_images()

    def test_a_run_id_may_be_used_again_from_the_moment_its_reply_is_sent(self):
        home = make_home("dock-serve-reuse-")
        store = Store(home)
        cwd = tempfile.mkdtemp(prefix="dock-serve-reuse-cwd-")
        seen = []
        done = threading.Event()
        request = {"id": 7, "method": "run", "params": {"command": "pid", "args": [], "cwd": cwd}}

        class InstantClient(serve.Writer):
            """Answers a reply by sending the same request again at once, as a fast client could."""

            def send(self, msg):
                seen.append(msg)
                if msg.get("id") != 7:
                    return
                if len([m for m in seen if m.get("id") == 7]) == 1:
                    server.handle(request)
                else:
                    done.set()

        server = serve.Server(store, False, InstantClient(io.BytesIO()), serve.Pool(store, False, 0))
        server.handle({"id": 1, "method": "hello"})
        seen.clear()
        server.handle(request)
        self.assertTrue(done.wait(15), seen)
        server.close()
        replies = [m for m in seen if "id" in m]
        self.assertEqual([m["id"] for m in replies], [7, 7])
        self.assertTrue(all("result" in m for m in replies), replies)  # the second was not refused as a duplicate


class ServePrompts(ServeCase):
    def setUp(self):
        super().setUp()
        self.cwd = tempfile.mkdtemp(prefix="dock-serve-prompt-")

    def ask(self, c, *args):
        return c.start("run", {"command": "ask", "args": list(args), "cwd": self.cwd})

    def test_a_prompt_is_forwarded_with_the_call_id_and_the_answer_reaches_the_plugin(self):
        c = self.hello_client(answer_prompts=lambda params: {"text": "Ada Lovelace"})
        call = self.ask(c, "who? ")
        self.assertEqual(c.wait_reply(call)["result"]["exit_code"], 0)
        self.assertEqual(len(c.prompts), 1)
        prompt = c.prompts[0]
        self.assertEqual((prompt["method"], prompt["params"]), ("prompt", {"call": call, "prompt": "who? "}))
        self.assertRegex(prompt["id"], r"^p\d+$")
        self.assertEqual(c.output_text(call), "answer: Ada Lovelace\n")

    def test_an_eof_an_error_or_malformed_answer_reaches_the_plugin_as_eof(self):
        def eof(c, call):
            pass  # answered by answer_prompts

        def error_reply(c, call):
            c.wait_until(lambda: c.prompts, what="the prompt")
            c.send({"id": c.prompts[0]["id"], "error": {"code": -32000, "message": "no terminal"}})

        def malformed(c, call_):
            c.wait_until(lambda: c.prompts, what="the prompt")
            c.send({"id": c.prompts[0]["id"], "result": {"text": 5}})

        for name, answer, kw in (("eof answer", eof, {"answer_prompts": lambda params: {"eof": True}}),
                                 ("error reply", error_reply, {}), ("malformed answer", malformed, {})):
            with self.subTest(name):
                c = self.hello_client(**kw)
                call = self.ask(c)
                answer(c, call)
                self.assertEqual(c.wait_reply(call)["result"]["exit_code"], 0)
                self.assertEqual(c.output_text(call), "answer: <eof>\n")

    def test_prompts_of_two_calls_are_answered_each_to_its_own_plugin(self):
        c = self.hello_client()
        a, b = self.ask(c, "a? "), self.ask(c, "b? ")
        c.wait_until(lambda: len(c.prompts) == 2, what="both prompts")
        self.assertEqual(len({p["id"] for p in c.prompts}), 2)
        for prompt in reversed(c.prompts):  # answered in the other order than asked
            c.send({"id": prompt["id"], "result": {"text": "re " + prompt["params"]["prompt"]}})
        c.wait_reply(a)
        c.wait_reply(b)
        self.assertEqual(c.output_text(a), "answer: re a? \n")
        self.assertEqual(c.output_text(b), "answer: re b? \n")

    def test_cancelling_while_a_prompt_is_pending_gives_the_plugin_eof(self):
        c = self.hello_client()
        call = self.ask(c)
        c.wait_until(lambda: c.prompts, what="the prompt")
        started = time.monotonic()
        c.notify("cancel", {"id": call})
        self.assertEqual(c.wait_reply(call)["error"]["code"], -32800)
        self.assertLess(time.monotonic() - started, float(GRACE) + 5)
        self.assertEqual(c.output_text(call), "answer: <eof>\n")  # the plugin was given eof, not left waiting

    def test_stray_messages_are_ignored(self):
        def late_answer(c):
            call = self.ask(c)
            c.wait_until(lambda: c.prompts, what="the prompt")
            c.notify("cancel", {"id": call})
            c.wait_reply(call)
            c.send({"id": c.prompts[0]["id"], "result": {"text": "too late"}})

        def unknown_prompt_ids(c):
            c.send({"id": "p99", "result": {"text": "x"}})
            c.send({"id": 5, "result": {}})

        def unknown_cancel(c):
            c.notify("cancel", {"id": 12345})
            c.notify("cancel", {})
            c.notify("cancel", {"id": [1]})
            call = c.start("run", {"command": "pid", "args": [], "cwd": self.cwd})
            self.assertEqual(c.wait_reply(call)["result"]["exit_code"], 0)

        for name, stray in (("answer after the prompt was withdrawn", late_answer),
                            ("replies to unknown prompt ids", unknown_prompt_ids),
                            ("cancelling an unknown id", unknown_cancel)):
            with self.subTest(name):
                c = self.hello_client()
                stray(c)
                self.assertIn("commands", c.request("commands")["result"])  # still serving; nothing crashed

    def test_closing_stdin_while_a_prompt_is_pending_ends_the_call_and_dock(self):
        c = self.hello_client()
        call = self.ask(c)
        c.wait_until(lambda: c.prompts, what="the prompt")
        c.close_stdin()
        self.assertEqual(c.wait(timeout=float(GRACE) + 10), 0)
        self.assertEqual(c.replies[call]["error"]["code"], -32800)

class ClientUIProgress(unittest.TestCase):
    """Progress to a serve client is `{call, percent, message}`; whatever else a plugin sends is dropped."""

    def notified(self, p: dict) -> dict:
        sent = []

        class Writer:
            def notify(self, method, params):
                sent.append((method, params))

        serve.ClientUI(7, Writer(), None, threading.Event()).progress(p)
        self.assertEqual([m for m, _ in sent], ["progress"])
        return sent[0][1]

    def test_only_percent_and_message_remain(self):
        with self.subTest("transfer fields are dropped"):
            got = self.notified({"percent": 100.0, "message": "seeding", "phase": "seeding", "uploaded": 5,
                                 "peers": 2, "done": 9, "total": 9, "speed": 1.5, "name": "x"})
            self.assertEqual(got, {"call": 7, "percent": 100.0, "message": "seeding"})
        with self.subTest("a field dock does not know is dropped"):
            got = self.notified({"percent": None, "message": "m", "surprise": 1})
            self.assertEqual(got, {"call": 7, "percent": None, "message": "m"})


if __name__ == "__main__":
    unittest.main()
