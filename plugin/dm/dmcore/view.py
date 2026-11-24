"""What a torrent run puts on the host's screen: bars, a panel and a table, at most 5 updates a second.

Peer addresses go only into the `peers` table, which the host draws; nothing here writes them to a log.
"""

import time
import unicodedata

from .fmt import Throttle, human_size

INTERVAL = 0.2  # seconds between updates of one area: 5 a second
MAX_FILE_BARS_SHOWN = 8  # at the same time
MAX_FILE_BARS_EVER = 28  # a torrent of thousands of files must not flood the output; the total bar still counts them
MAX_PEERS = 10
LABEL_CELLS = 28  # the host cuts a label from the right to a share of the width: a long name keeps its tail here


def human_rate(bytes_per_s):
    return f"{human_size(int(bytes_per_s))}/s"


def human_eta(seconds):
    if seconds is None:
        return "-"
    s = int(seconds)
    if s >= 100 * 3600:
        return "> 99h"
    if s >= 3600:
        return f"{s // 3600}h {s % 3600 // 60:02d}m"
    if s >= 60:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s}s"


def _cells(char):
    if unicodedata.category(char) in ("Mn", "Me", "Cf"):
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def display_width(text):
    """Terminal cells the text takes: wide characters two, combining ones none (cli can not be imported here)."""
    return sum(_cells(c) for c in text)


def tail_label(path):
    """The path itself when it fits, else `…` and its last characters: names that differ at the end (episode
    numbers, extensions) must differ on screen too, and the host cuts what is too long from the right."""
    if display_width(path) <= LABEL_CELLS:
        return path
    kept, used = [], 1  # the ellipsis
    for char in reversed(path):
        if used + _cells(char) > LABEL_CELLS:
            break
        kept.append(char)
        used += _cells(char)
    while kept and _cells(kept[-1]) == 0:
        kept.pop()  # a combining mark left at the cut has lost its base
    return "…" + "".join(reversed(kept))


def _address(addr):
    host, port = addr
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


class TorrentView:
    """Sends the messages for one torrent run through `notify(method, **params)`."""

    def __init__(self, notify, clock=time.monotonic):
        self._notify = notify
        self._clock = clock
        self._total_gate = Throttle(INTERVAL, clock)
        self._stats_gate = Throttle(INTERVAL, clock)
        self._shown = {}  # (kind, id) -> the last payload sent, to skip an update that changes nothing
        self._files = {}  # index -> (path, size) of the file bars introduced, in order
        self._done = set()  # indexes whose bar was sent done
        self._last = None  # the newest (percent, message) of the total bar, sent or not

    def _send(self, kind, id, **params):
        key = (kind, id)
        payload = tuple(sorted((k, repr(v)) for k, v in params.items()))
        if self._shown.get(key) == payload:
            return
        self._shown[key] = payload
        self._notify(kind, id=id, **params)

    def progress(self, percent, message):
        """The total bar: what the session reports (checking, fetching metadata, bytes of bytes)."""
        self._last = (percent, message)
        if self._total_gate.ready():
            self._send_total()

    def _send_total(self):
        percent, message = self._last
        self._send("bar", "total", label="total", percent=percent, message=message)

    def stats(self, make, final=False):
        """Files, panel and peers from `make()`, which is only called when an update is due.

        The cap of 5 updates a second (R22) holds for every update but the final one: `final=True` always passes
        the gate, so the last numbers, the closing bars and the peers table are never held back or lost."""
        if not self._stats_gate.ready(final=final):
            return
        if self._last is not None:
            self._send_total()  # a value the total bar's own gate dropped must not stay stale; _send skips a repeat
        st = make()
        self._file_bars(st["files"])
        self._send("panel", "stats", title="", items=[
            ["down", human_rate(st["down_rate"])],
            ["up", human_rate(st["up_rate"])],
            ["peers", f"{st['peers_connected']} of {st['peers_known']}"],
            ["eta", human_eta(st["eta_s"])],
            ["ratio", "-" if st["ratio"] is None else f"{st['ratio']:.2f}"],  # None: nothing downloaded this run
        ])
        rows = [[_address(addr), client, human_rate(down), human_rate(up)]
                for addr, client, down, up in st["peers"][:MAX_PEERS]]
        if rows or ("table", "peers") in self._shown:
            self._send("table", "peers", title="", columns=["peer", "client", "down", "up"], rows=rows)

    def _file_bars(self, files):
        """`files` is [(index, path, verified bytes, size)]: the largest unfinished files and those that just finished."""
        if not files:
            return
        for i, path, done, size in files:
            if i in self._files and i not in self._done and done >= size:
                self._done.add(i)
                self._send("bar", f"f{i}", label=tail_label(path), percent=100.0, message=self._text(size, size),
                           done=True)
        unfinished = sorted((f for f in files if f[2] < f[3]), key=lambda f: (-f[3], f[0]))
        for i, path, done, size in unfinished[:MAX_FILE_BARS_SHOWN]:
            if i not in self._files:
                if len(self._files) >= MAX_FILE_BARS_EVER:
                    continue  # no new bar, but one shown earlier may still be due below
                self._files[i] = (path, size)
            self._send("bar", f"f{i}", label=tail_label(path), percent=done * 100.0 / size, message=self._text(done, size))

    @staticmethod
    def _text(done, size):
        return f"{human_size(done)} of {human_size(size)}"

    def finish(self):
        """The download succeeded: close the bars. The panel and the table stay as the last update left them: the
        host writes every live area to scrollback when the invoke ends, so the final numbers and the peers stay."""
        for i, (path, size) in self._files.items():
            if i not in self._done:
                self._done.add(i)
                self._send("bar", f"f{i}", label=tail_label(path), percent=100.0, message=self._text(size, size),
                           done=True)
        if self._last is not None:
            percent, message = self._last
            self._send("bar", "total", label="total", percent=None if percent is None else 100, message=message,
                       done=True)

    def drop(self):
        """The run did not finish: nothing of it stays on screen.

        A piped host prints a panel or table when it is cleared, so they are emptied first: the error line is all
        a failed run should leave."""
        if ("panel", "stats") in self._shown:
            self._send("panel", "stats", title="", items=[])
        if ("table", "peers") in self._shown:
            self._send("table", "peers", title="", columns=[], rows=[])
        for kind, id in list(self._shown):
            self._clear(kind, id)

    def _clear(self, kind, id):
        if self._shown.pop((kind, id), None) is not None:
            self._notify("clear", id=id, kind=kind)
