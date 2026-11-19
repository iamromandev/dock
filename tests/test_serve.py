"""`dock serve`: the JSON-RPC host mode. Real `python -m cli serve` children over pipes. Run `make test`."""

import hashlib
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
from cli.manifest import platform_key  # noqa: E402
from cli.store import Store  # noqa: E402
from serve_client import GRACE, ServeCase, make_home, pid_alive, wait_gone  # noqa: E402


class ServeSkeleton(ServeCase):
    def test_hello_returns_the_serve_and_dock_versions(self):
        c = self.client()
        self.assertEqual(c.hello(), {"serve_version": SERVE_VERSION, "dock_version": __version__})
        self.assertEqual(SERVE_VERSION, "1")

    def test_stdout_holds_only_json_lines(self):
        c = self.client()
        c.hello()
        c.request("commands")
        c.close_stdin()
        self.assertEqual(c.wait(), 0)
        self.assertEqual(c.bad_lines, [])

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
        download = commands["download"]
        self.assertEqual(download["plugin"], "dev.dock.download")
        self.assertIn("Download", download["summary"])
        self.assertEqual(download["inputs"][0], {"name": "url", "type": "string", "required": True})
        self.assertEqual(download["inputs"][1],
                         {"name": "output", "type": "string", "required": False, "flag": "--output", "short": "-o"})
        self.assertEqual(commands["hello"]["plugin"], "dev.dock.hello")
        self.assertEqual(commands["count"]["inputs"], [{"name": "n", "type": "int", "required": False}])

    def test_a_line_that_is_not_json_is_refused_and_the_connection_carries_on(self):
        c = self.hello_client()
        c.send_raw(b"this is not json\n")
        c.wait_until(lambda: None in c.replies, what="the parse error")
        error = c.replies[None]["error"]
        self.assertEqual(error["code"], -32700)
        self.assertIn("commands", c.request("commands")["result"])

    def test_a_line_over_one_megabyte_is_refused_and_the_connection_carries_on(self):
        c = self.hello_client()
        c.send_raw(b'{"id": 1, "method": "' + b"x" * (1024 * 1024 + 10) + b'"}\n')
        c.wait_until(lambda: None in c.replies, what="the too-long error")
        self.assertEqual(c.replies[None]["error"]["code"], -32700)
        self.assertIn("commands", c.request("commands")["result"])

    def test_a_json_value_that_is_not_an_object_is_an_invalid_request(self):
        c = self.hello_client()
        c.send_raw(b"[1, 2]\n")
        c.wait_until(lambda: None in c.replies, what="the invalid request error")
        self.assertEqual(c.replies[None]["error"]["code"], -32600)

    def test_an_unknown_method_is_not_found(self):
        c = self.hello_client()
        self.assertEqual(c.request("frobnicate")["error"]["code"], -32601)

    def test_end_of_stdin_exits_zero(self):
        c = self.hello_client()
        c.close_stdin()
        self.assertEqual(c.wait(), 0)

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

    def state_files(self):
        return sorted(p.name for p in Path(self.home, "state").iterdir())

    # refused before anything starts (R3) -------------------------------------
    def test_a_missing_required_input_is_an_error_and_no_plugin_starts(self):
        c = self.hello_client()
        before = self.starts("dev.dock.download")
        error = c.wait_reply(self.run_call(c, "download"))["error"]
        self.assertEqual(error["code"], -32602)
        self.assertEqual(error["message"], "missing required input 'url'")
        self.assertEqual(self.starts("dev.dock.download"), before)

    def test_an_unknown_command_is_not_found(self):
        c = self.hello_client()
        self.assertEqual(c.wait_reply(self.run_call(c, "nonesuch"))["error"]["code"], -32601)

    def test_a_cwd_that_is_not_an_existing_absolute_folder_is_refused(self):
        c = self.hello_client()
        for cwd in ("relative/dir", os.path.join(self.cwd, "missing"), 7, None):
            with self.subTest(cwd=cwd):
                req = c.start("run", {"command": "pid", "args": [], "cwd": cwd})
                self.assertEqual(c.wait_reply(req)["error"]["code"], -32602)

    def test_args_must_be_a_list_of_strings_and_inputs_are_reserved(self):
        c = self.hello_client()
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
        self.assertEqual(reply["result"], {"exit_code": 0, "pid": c.pid_of(call), "cwd": self.cwd})
        progress = c.notes("progress", call)
        self.assertEqual([round(n["params"]["percent"], 1) for n in progress], [33.3, 66.7, 100.0])
        self.assertEqual(progress[0]["params"]["message"], "1/3")
        self.assertEqual(progress[-1]["params"]["done"], 3)  # a plugin's extra fields pass through
        self.assertEqual(progress[-1]["params"]["total"], 3)
        self.assertEqual(len(c.notes("progress")), 3)  # and nothing came untagged

    def test_two_calls_keep_their_notifications_apart(self):
        c = self.hello_client()
        a, b = self.run_call(c, "work", "4"), self.run_call(c, "work", "2")
        c.wait_reply(a)
        c.wait_reply(b)
        self.assertEqual(len(c.notes("progress", a)), 4)
        self.assertEqual(len(c.notes("progress", b)), 2)
        self.assertNotEqual(c.pid_of(a), c.pid_of(b))  # a process per call that runs

    def test_plugins_without_the_new_fields_still_work(self):
        c = self.hello_client()
        call = self.run_call(c, "count", "2")
        self.assertEqual(c.wait_reply(call)["result"], {"exit_code": 0})
        self.assertEqual([n["params"]["percent"] for n in c.notes("progress", call)], [50.0, 100.0])
        self.assertEqual(c.output_text(call), "counted to 2\n")

    def test_a_plugin_error_comes_back_with_its_code(self):
        c = self.hello_client()
        error = c.wait_reply(self.run_call(c, "crash"))["error"]
        self.assertEqual(error["code"], -32000)
        self.assertIn("exited during 'invoke'", error["message"])
        again = self.run_call(c, "pid")  # serve is unharmed
        self.assertEqual(c.wait_reply(again)["result"]["exit_code"], 0)

    def test_download_a_file_through_serve(self):
        binary = ROOT / "plugin" / "download" / "bin" / f"download-{platform_key()}"
        if not binary.exists():
            self.skipTest("the download plugin is not built (make build)")
        source = Path(self.cwd, "source.bin")
        source.write_bytes(os.urandom(300_000))
        dest = Path(self.cwd, "copy.bin")
        c = self.hello_client()
        call = self.run_call(c, "download", source.as_uri(), "-o", str(dest))
        self.assertEqual(c.wait_reply(call)["result"]["exit_code"], 0)
        self.assertEqual(hashlib.sha256(dest.read_bytes()).hexdigest(), hashlib.sha256(source.read_bytes()).hexdigest())
        for n in c.notes("progress", call):
            self.assertIn("percent", n["params"])

    # cancel (R6, R7) ---------------------------------------------------------
    def test_cancelling_one_of_three_calls_leaves_the_others_and_leaves_no_process(self):
        c = self.hello_client()
        keep1 = self.run_call(c, "work", "20", "--delay", "0.1")
        victim = self.run_call(c, "sleep", "60")
        keep2 = self.run_call(c, "work", "20", "--delay", "0.1")
        c.wait_until(lambda: all(c.notes("output", k) for k in (keep1, victim, keep2)), what="the calls to start")
        pids = [c.pid_of(k) for k in (keep1, victim, keep2)]
        self.assertEqual(len(set(pids)), 3)
        started = time.monotonic()
        c.notify("cancel", {"id": victim})
        error = c.wait_reply(victim, timeout=10)["error"]
        self.assertEqual(error["code"], -32800)
        self.assertLess(time.monotonic() - started, float(GRACE) + 1)
        for keep in (keep1, keep2):
            self.assertEqual(c.wait_reply(keep)["result"]["exit_code"], 0)
            self.assertEqual(len(c.notes("progress", keep)), 20)
        self.assertTrue(wait_gone(pids[1]), "the cancelled call's process is still running")
        c.close_stdin()
        self.assertEqual(c.wait(), 0)
        for pid in pids:
            self.assertTrue(wait_gone(pid), f"plugin process {pid} is still running")

    def test_a_plugin_that_ignores_cancel_is_killed_and_the_call_still_ends_in_time(self):
        c = self.hello_client()
        call = self.run_call(c, "stubborn", "60")
        c.wait_until(lambda: c.notes("output", call), what="the call to start")
        pid = c.pid_of(call)
        started = time.monotonic()
        c.notify("cancel", {"id": call})
        self.assertEqual(c.wait_reply(call, timeout=10)["error"]["code"], -32800)
        self.assertLess(time.monotonic() - started, float(GRACE) + 1)
        self.assertTrue(wait_gone(pid), "the stuck plugin is still running")

    def test_cancelling_an_unknown_id_is_ignored(self):
        c = self.hello_client()
        c.notify("cancel", {"id": 12345})
        c.notify("cancel", {})
        c.notify("cancel", {"id": [1]})
        call = self.run_call(c, "pid")
        self.assertEqual(c.wait_reply(call)["result"]["exit_code"], 0)

    # one call's death is only its own (R7) -------------------------------------
    def test_a_killed_plugin_fails_its_own_call_only(self):
        c = self.hello_client()
        doomed = self.run_call(c, "sleep", "3")
        other = self.run_call(c, "sleep", "3")
        c.wait_until(lambda: c.notes("output", doomed) and c.notes("output", other), what="both to start")
        os.kill(c.pid_of(doomed), signal.SIGKILL)
        self.assertIn("error", c.wait_reply(doomed))
        self.assertEqual(c.wait_reply(other)["result"]["exit_code"], 0)

    # leaving (R8) ----------------------------------------------------------------
    def test_closing_stdin_during_calls_cancels_them_and_leaves_nothing_behind(self):
        c = self.hello_client()
        calls = [self.run_call(c, "sleep", "60") for _ in range(2)]
        c.wait_until(lambda: all(c.notes("output", k) for k in calls), what="the calls to start")
        pids = [c.pid_of(k) for k in calls]
        started = time.monotonic()
        c.close_stdin()
        self.assertEqual(c.wait(timeout=float(GRACE) + 3), 0)
        self.assertLess(time.monotonic() - started, float(GRACE) + 3)
        for pid in pids:
            self.assertFalse(pid_alive(pid), f"plugin process {pid} is still running")
        self.assertEqual(self.state_files(), [])
        self.assertEqual(c.bad_lines, [])

    def test_shutdown_answers_after_cancelling_the_calls(self):
        c = self.hello_client()
        call = self.run_call(c, "sleep", "60")
        c.wait_until(lambda: c.notes("output", call), what="the call to start")
        pid = c.pid_of(call)
        self.assertEqual(c.request("shutdown", timeout=float(GRACE) + 3)["result"], {})
        self.assertEqual(c.replies[call]["error"]["code"], -32800)  # the call was answered before shutdown was
        self.assertEqual(c.wait(), 0)
        self.assertFalse(pid_alive(pid))
        self.assertEqual(self.state_files(), [])

    def test_sigterm_leaves_like_end_of_input(self):
        c = self.hello_client()
        call = self.run_call(c, "sleep", "60")
        c.wait_until(lambda: c.notes("output", call), what="the call to start")
        pid = c.pid_of(call)
        c.proc.send_signal(signal.SIGTERM)
        self.assertEqual(c.wait(timeout=float(GRACE) + 3), 0)
        self.assertFalse(pid_alive(pid))
        self.assertEqual(self.state_files(), [])

    def test_sigint_does_not_stop_serve(self):
        c = self.hello_client()
        c.proc.send_signal(signal.SIGINT)  # a Ctrl-C aimed at the client's whole process group
        time.sleep(0.3)
        self.assertIsNone(c.proc.poll())
        self.assertIn("commands", c.request("commands")["result"])

    def test_after_a_sigkilled_serve_the_next_one_cleans_up(self):
        first = self.hello_client()
        call = self.run_call(first, "sleep", "60")
        first.wait_until(lambda: first.notes("output", call), what="the call to start")
        pid = first.pid_of(call)
        self.addCleanup(lambda: pid_alive(pid) and os.kill(pid, signal.SIGKILL))
        self.assertEqual(self.state_files(), ["dev.dock.pidprobe.provisioned"])
        first.kill()
        second = self.hello_client()  # recovery runs before it answers
        self.assertEqual(self.state_files(), [])
        second.close_stdin()
        self.assertEqual(second.wait(), 0)

    # permissions are the CLI's (NFR2) -----------------------------------------------
    def test_a_plugin_without_a_declared_host_has_its_http_request_refused(self):
        c = self.hello_client()
        call = self.run_call(c, "fetch", "http://127.0.0.1:9/")
        self.assertEqual(c.wait_reply(call)["result"]["exit_code"], 1)  # the plugin reports its failed request
        self.assertIn("error:-32001", c.output_text(call))


class RunIdReuse(unittest.TestCase):
    """The reply is out before the call is forgotten; the id must still be free the moment a client reads it."""

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

    def test_an_eof_answer_reaches_the_plugin_as_eof(self):
        c = self.hello_client(answer_prompts=lambda params: {"eof": True})
        call = self.ask(c)
        self.assertEqual(c.wait_reply(call)["result"]["exit_code"], 0)
        self.assertEqual(c.output_text(call), "answer: <eof>\n")

    def test_an_error_reply_counts_as_eof(self):
        c = self.hello_client()
        call = self.ask(c)
        c.wait_until(lambda: c.prompts, what="the prompt")
        c.send({"id": c.prompts[0]["id"], "error": {"code": -32000, "message": "no terminal"}})
        self.assertEqual(c.wait_reply(call)["result"]["exit_code"], 0)
        self.assertEqual(c.output_text(call), "answer: <eof>\n")

    def test_a_malformed_answer_counts_as_eof(self):
        c = self.hello_client()
        call = self.ask(c)
        c.wait_until(lambda: c.prompts, what="the prompt")
        c.send({"id": c.prompts[0]["id"], "result": {"text": 5}})
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
        self.assertLess(time.monotonic() - started, float(GRACE) + 1)
        self.assertEqual(c.output_text(call), "answer: <eof>\n")  # the plugin was given eof, not left waiting

    def test_an_answer_that_comes_after_the_prompt_was_withdrawn_is_ignored(self):
        c = self.hello_client()
        call = self.ask(c)
        c.wait_until(lambda: c.prompts, what="the prompt")
        c.notify("cancel", {"id": call})
        c.wait_reply(call)
        c.send({"id": c.prompts[0]["id"], "result": {"text": "too late"}})
        self.assertIn("commands", c.request("commands")["result"])  # still serving; nothing crashed

    def test_closing_stdin_while_a_prompt_is_pending_ends_the_call_and_dock(self):
        c = self.hello_client()
        call = self.ask(c)
        c.wait_until(lambda: c.prompts, what="the prompt")
        c.close_stdin()
        self.assertEqual(c.wait(timeout=float(GRACE) + 3), 0)
        self.assertEqual(c.replies[call]["error"]["code"], -32800)

    def test_replies_to_unknown_prompt_ids_are_ignored(self):
        c = self.hello_client()
        c.send({"id": "p99", "result": {"text": "x"}})
        c.send({"id": 5, "result": {}})
        self.assertIn("commands", c.request("commands")["result"])


if __name__ == "__main__":
    unittest.main()
