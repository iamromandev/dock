"""Messages a plugin sends to the host while it works: log lines, progress bars and tables."""

import json
import sys

_bar = {"id": "plugin", "label": "working"}


def configure(bar_id, label):
    """Sets the id and label `bar` sends. rpc.run calls this."""
    _bar["id"], _bar["label"] = bar_id, label


def send(msg):
    try:
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", **msg}) + "\n")
        sys.stdout.flush()
    except OSError:  # the host is gone (broken pipe); nobody to tell
        pass


def notify(method, **params):
    send({"method": method, "params": params})


def say(level, text):
    notify("log", level=level, message=text)


def bar(percent, **extra):
    notify("bar", id=_bar["id"], label=_bar["label"], percent=percent, **extra)


def table(id, title, columns, rows):
    notify("table", id=id, title=title, columns=columns, rows=rows)
