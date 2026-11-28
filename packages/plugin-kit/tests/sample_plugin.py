"""A tiny plugin built on dock_plugin.rpc, and `Plugin`, the host side the tests use to drive it over stdio."""

import json
import os
import subprocess
import sys
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dock_plugin import cancel, files, net, rpc, ui


def echo(inputs, ctx):
    return {"text": rpc.require_str(inputs, "text"), "tag": rpc.optional_str(inputs, "tag")}


def fail(inputs, ctx):
    raise rpc.Failure(inputs["message"], inputs.get("code"))


def boom(inputs, ctx):
    raise RuntimeError("kaboom")


def hold(inputs, ctx):
    """Says it is ready, then waits for a cancel (or a closed stdin)."""
    ui.say("info", "ready")
    cancel.cancelled.wait(30)
    raise rpc.Cancelled()


def where(inputs, ctx):
    return {"cwd": ctx.cwd, "in_dir": ctx.in_dir, "work": ctx.work}


def ask(inputs, ctx):
    return {"reply": ctx.ask("prompt", {"q": inputs.get("q")})}


def show(inputs, ctx):
    ui.bar(5, done=False)
    ui.table("t", "title", ["a", "b"], [[1, 2]])
    return {}


def source(inputs, ctx):
    return {"path": files.resolve_source(rpc.require_str(inputs, "file"), ctx.in_dir, ctx.work, "sample")}


def post(inputs, ctx):
    """POSTs `size` bytes to `url` in a worker thread."""
    def go():
        status, data = net.request(inputs["url"], "/x", bytes(inputs["size"]), ctx.call, service="sample",
                                   var="SAMPLE_URL", start="start-it", read_timeout=30, max_reply=1000)
        return {"status": status, "data": data.decode()}

    ui.say("info", "ready")
    return cancel.run_worker(go, (), ctx.call, "sample")


COMMANDS = {"echo": echo, "fail": fail, "boom": boom, "hold": hold, "where": where, "ask": ask, "show": show,
            "source": source, "post": post}


class Plugin:
    """The sample plugin as a child process; `send` writes a line, `read` returns the next message."""

    def __init__(self):
        self.proc = subprocess.Popen([sys.executable, os.path.abspath(__file__)], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, text=True, bufsize=1)
        self.watchdog = threading.Timer(30, self.proc.kill)  # a hung plugin fails the test instead of stalling it
        self.watchdog.start()
        self.next_id = 0

    def send(self, **msg):
        assert self.proc.stdin
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", **msg}) + "\n")
        self.proc.stdin.flush()

    def read(self):
        assert self.proc.stdout
        line = self.proc.stdout.readline()
        return json.loads(line) if line else None

    def start(self, command, **inputs):
        """Sends an invoke and returns its id."""
        return self.start_with(command, inputs)

    def start_with(self, command, inputs, **params):
        self.next_id += 1
        self.send(id=self.next_id, method="invoke", params={"command": command, "inputs": inputs, **params})
        return self.next_id

    def until(self, match):
        """Reads messages until match(msg) and returns it; the messages before it are in `self.seen`."""
        self.seen = []
        while (msg := self.read()) is not None:
            if match(msg):
                return msg
            self.seen.append(msg)
        raise AssertionError(f"plugin ended; saw {self.seen}")

    def answer(self, req_id):
        return self.until(lambda m: m.get("id") == req_id and "method" not in m)

    def invoke(self, command, **inputs):
        """Invokes and returns the answer; the notifications on the way are in `self.seen`."""
        return self.answer(self.start(command, **inputs))

    def close(self):
        self.watchdog.cancel()
        if self.proc.stdin and not self.proc.stdin.closed:
            self.proc.stdin.close()
        self.proc.kill()
        self.proc.wait()
        if self.proc.stdout:
            self.proc.stdout.close()


if __name__ == "__main__":
    rpc.run(COMMANDS, "sample", "sampling")
