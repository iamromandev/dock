"""Child of tests/test_live_pty.py: a LiveUI on the pty it runs in, fed by a script instead of a plugin container.

argv[1] is the script; DOCK_DRIVER_RESULT, when set, is a file the script writes its numbers or answers to (JSON).
The messages go through `ui.handlers()`, exactly as a PluginProcess would call them.

  basic     2 bars, a table and a panel updated 100 times, with output lines in between, then `end()`
  hold      one bar, a table and a panel, then wait for a signal (resize, SIGTERM) or `q` on stdin
  many      31 bars and panels and a table of 500 rows (32 areas), then wait for `q` on stdin
  hostile   hostile bytes in every kind of text, in every message
  flood     10,000 updates a second for DOCK_DRIVER_SECONDS (default 5); writes frames and CPU use
  form      a form with text, secret, confirm and choice fields; writes the values
  broken    a renderer that raises, then the run goes on
  raise     an exception in the main thread while a region is drawn
"""

import json
import os
import random
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from cli import live  # noqa: E402
from cli.live import LiveUI  # noqa: E402
from cli.term import Terminal  # noqa: E402

script = sys.argv[1]
term = Terminal()
term.install()
ui = LiveUI("probe", False, term)
h = ui.handlers()


def result(**values):
    path = os.environ.get("DOCK_DRIVER_RESULT")
    if path:
        with open(path, "w") as f:
            json.dump(values, f)


def basic():
    for i in range(1, 101):
        h["bar"](dict(id="a", label="first", percent=i, message=f"{i} of 100", done=i == 100))
        h["bar"](dict(id="b", label="second", percent=i / 2))
        h["table"](dict(id="t", title="peers", columns=["peer", "down"], rows=[["1.2.3.4", i], ["5.6.7.8", 2 * i]]))
        h["panel"](dict(id="p", title="stats", items=[["speed", f"{i} MB/s"], ["peers", 2]]))
        if i % 25 == 0:
            h["output"](dict(text=f"output line {i}\n"))
            h["log"](dict(level="info", message=f"log line {i}"))
        time.sleep(0.01)
    h["output"](dict(text="basic done\n"))


def hold():
    h["bar"](dict(id="a", label="a long label for the bar " * 3, percent=40, message="some message " * 6))
    h["table"](dict(id="t", title="peers", columns=["peer", "client"], rows=[["1.2.3.4:6881", "qBittorrent 4.6.5"]] * 3))
    h["panel"](dict(id="p", title="stats", items=[["speed", "4.2 MB/s"], ["peers", "8 of 31"]]))
    latencies = []

    def watch():  # every resize's latency, not only the last one
        seen = None
        while True:
            value = ui.resize_latency
            if value is not None and value != seen:
                seen = value
                latencies.append(value)
            time.sleep(0.002)

    threading.Thread(target=watch, daemon=True).start()
    wait_for_q()
    result(resize_latency=ui.resize_latency, resize_latencies=latencies)


def wait_for_q():
    with open("/dev/stdin", "rb", buffering=0) as stdin:
        while stdin.read(1) != b"q":
            pass


def many():
    for i in range(31):
        if i % 2:
            h["panel"](dict(id=f"p{i}", title=f"panel {i}", items=[["speed", "4.2 MB/s"], ["peers", i], ["eta", "1m"]]))
        else:
            h["bar"](dict(id=f"b{i}", label=f"bar number {i}", percent=i * 3, message="a message that is rather long"))
    h["table"](dict(id="big", title="big table", columns=["n", "name"], rows=[[i, f"row {i}"] for i in range(500)]))
    wait_for_q()


def hostile():
    rnd = random.Random(int(os.environ.get("DOCK_DRIVER_SEED", "1")))
    # every C0 and C1 control, DEL and lone surrogates, alone and in the sequences that do harm
    pieces = [*map(chr, range(0x20)), "\x7f", *map(chr, range(0x80, 0xA0)), "\ud800", "\udbff", "\udc00", "\udfff",
              "\x1b[31m", "\x1b]0;x\x07", "\x1bc", "\x9b2J", "\x1b[?1049h", "\x1bP1$r\x1b\\", "\x1b[6n", "\x1b[",
              "日本", "é", "plain", "plain", "plain"]

    def evil():
        return "".join(rnd.choice(pieces) for _ in range(rnd.randint(1, 8)))

    for i in range(60):
        h["output"](dict(text=evil() + ("\n" if i % 2 else "")))
        h["log"](dict(level=evil(), message=evil()))
        h["bar"](dict(id=f"b{i % 5}", label=evil(), percent=rnd.choice([None, 0, 50, 100, 1e9, -5]), message=evil()))
        h["table"](dict(id="t", title=evil(), columns=[evil(), evil()], rows=[[evil(), evil()] for _ in range(3)]))
        h["panel"](dict(id="p", title=evil(), items=[[evil(), evil()], [evil(), evil()]]))
        ui.warn(evil())
        time.sleep(0.02)
    h["output"](dict(text="hostile done\n"))
    # questions: hostile labels, choices, defaults and prompts. Enter takes every default (the test sends it).
    choices = [evil() or "x" for _ in range(3)]
    text_default = evil()
    answers = h["form"](dict(title=evil(), fields=[
        {"name": "t", "label": evil(), "type": "text", "default": text_default},
        {"name": "c", "label": evil(), "type": "confirm", "default": True},
        {"name": "k", "label": evil(), "type": "choice", "choices": choices, "default": choices[1]},
    ]))
    asked = h["prompt_user"](dict(prompt=evil()))
    result(ok=answers == {"values": {"t": text_default, "c": True, "k": choices[1]}} and asked == {"text": ""})


def flood():
    seconds = float(os.environ.get("DOCK_DRIVER_SECONDS", "5"))
    cpu0, t0 = time.process_time(), time.monotonic()
    sent = 0
    deadline = t0 + seconds
    while time.monotonic() < deadline:
        due = int((time.monotonic() - t0) * 10_000) - sent  # catch up with 10,000 a second
        if due <= 0:
            time.sleep(0.001)
            continue
        for _ in range(min(due, 500)):
            sent += 1
            kind = sent % 4
            if kind == 0:
                h["bar"](dict(id="a", label="a", percent=(sent / 100) % 100, message=str(sent)))
            elif kind == 1:
                h["bar"](dict(id="b", label="b", percent=(sent / 50) % 100))
            elif kind == 2:
                h["table"](dict(id="t", columns=["n", "v"], rows=[[i, sent] for i in range(20)]))
            else:
                h["panel"](dict(id="p", title="stats", items=[["sent", sent], ["peers", 2]]))
    elapsed, cpu = time.monotonic() - t0, time.process_time() - cpu0
    result(sent=sent, seconds=elapsed, cpu=cpu, frames=ui.frames)


def form():
    try:
        values = h["form"](dict(title="Settings", fields=[
            {"name": "name", "label": "Your name", "type": "text", "default": "Ada"},
            {"name": "token", "label": "Token", "type": "text", "secret": True},
            {"name": "ok", "label": "Overwrite?", "type": "confirm", "default": False},
            {"name": "mirror", "label": "Mirror", "type": "choice", "choices": ["eu", "us", "asia"], "default": "eu"},
        ]))
    except KeyboardInterrupt:
        ui.end()
        term.close()
        sys.exit(130)
    result(**values)


def late_form():
    h["output"](dict(text="READY\n"))
    time.sleep(float(os.environ.get("DOCK_DRIVER_DELAY", "1.5")))  # keys typed meanwhile wait in the tty queue
    result(**h["form"](dict(fields=[{"name": "name", "label": "Name?", "type": "text"}])))


def late_prompt():
    h["output"](dict(text="READY\n"))
    time.sleep(float(os.environ.get("DOCK_DRIVER_DELAY", "1.5")))
    result(**h["prompt_user"](dict(prompt="Answer: ")))


def broken():
    def fail(*a, **k):
        raise RuntimeError("renderer bug")

    live.layout = fail
    h["bar"](dict(id="a", label="a", percent=10))
    time.sleep(0.3)  # the render thread dies here
    h["bar"](dict(id="a", label="a", percent=20, done=True))
    h["output"](dict(text="run went on\n"))


def broken_form():
    def fail(*a, **k):
        raise RuntimeError("renderer bug")

    live.layout = fail
    h["bar"](dict(id="a", label="a", percent=10))
    time.sleep(0.3)  # the render thread dies here
    result(**h["form"](dict(fields=[{"name": "name", "label": "Name?", "type": "text"}])))


def raise_():
    h["bar"](dict(id="a", label="working", percent=10))
    time.sleep(0.3)
    raise RuntimeError("boom in the main thread")


ui.begin()
{"basic": basic, "hold": hold, "many": many, "hostile": hostile, "flood": flood, "form": form, "late_form": late_form,
 "late_prompt": late_prompt, "broken": broken, "broken_form": broken_form,
 "raise": raise_}[script]()
ui.end()
term.close()
