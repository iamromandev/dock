"""hello plugin: shows the Python SDK. `make build` copies dock_sdk.py next to this file."""

import time

from dock_sdk import Plugin, PluginFailure

plugin = Plugin("hello")


@plugin.command("hello")
def hello(p: Plugin, args: list[str]) -> int:
    name = " ".join(args) or p.prompt("what's your name? ")
    p.output(f"hello, {name or 'stranger'}!")
    return 0


@plugin.command("count")
def count(p: Plugin, args: list[str]) -> int:
    try:
        n = int(args[0]) if args else 10
    except ValueError:
        raise PluginFailure("count needs a number", -32602)
    for i in range(1, n + 1):
        p.check_cancelled()
        p.progress(i * 100 / n, f"{i}/{n}")
        time.sleep(0.05)
    p.output(f"counted to {n}")
    return 0


plugin.run()
