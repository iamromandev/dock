"""What the live view needs to know about a download: smoothed rates and peer client names."""

import heapq
import math
import unicodedata

from . import layout

WINDOW = 5.0  # seconds: a rate is an exponential moving average over about this long
CLIENT_CHARS = 20

# Azureus-style peer ids: "-" + two letters + four version characters + "-"
CLIENTS = {
    "qB": "qBittorrent",
    "TR": "Transmission",
    "UT": "uTorrent",
    "LT": "libtorrent",
    "lt": "libtorrent",
    "DE": "Deluge",
    "AZ": "Azureus",
    "DM": "dm",
}


class Rate:
    """Bytes per second as an exponential moving average. add() counts bytes; value() brings the average up to date."""

    def __init__(self, clock, window=WINDOW):
        self.clock = clock
        self.window = window
        self._value = 0.0
        self._pending = 0
        self._last = clock()

    def add(self, n):
        self._pending += n

    def value(self):
        now = self.clock()
        dt = now - self._last
        if dt > 0:
            share = 1 - math.exp(-dt / self.window)
            self._value += share * (self._pending / dt - self._value)
            self._pending = 0
            self._last = now
        return self._value


class FileProgress:
    """Bytes verified per file, kept up to date as pieces complete, and what the live view needs of them.

    The work per piece and per report does not grow with the number of files: `report()` gives the 8 largest
    unfinished files (a heap with lazy deletion) plus the files it reported before that have finished since.
    """

    SHOWN = 8

    def __init__(self, specs, piece_length, total):
        self._specs = specs
        self._piece_length = piece_length
        self._total = total
        self._offsets = [f.offset for f in specs]
        self._done = [0] * len(specs)
        self._heap = [(-f.length, i) for i, f in enumerate(specs) if f.length > 0]
        heapq.heapify(self._heap)
        self._seen = set()  # files that were reported unfinished: the view may have a bar for them
        self._finished = []  # of those, the ones that finished since the last report

    def piece_done(self, index):
        for i, n in layout.piece_files(self._specs, self._piece_length, self._total, index, self._offsets):
            self._done[i] += n
            if self._done[i] >= self._specs[i].length and i in self._seen:
                self._seen.discard(i)
                self._finished.append(i)

    def _row(self, i):
        f = self._specs[i]
        return (i, "/".join(f.parts), min(self._done[i], f.length), f.length)

    def report(self):
        """[(index, path, verified bytes, size)]: the largest unfinished files, then those that just finished."""
        rows = [self._row(i) for i in self._finished]
        self._finished = []
        keep = []
        while self._heap and len(keep) < self.SHOWN:
            entry = heapq.heappop(self._heap)
            i = entry[1]
            if self._done[i] >= self._specs[i].length:
                continue  # finished: gone from the heap for good
            keep.append(entry)
            self._seen.add(i)
            rows.append(self._row(i))
        for entry in keep:
            heapq.heappush(self._heap, entry)
        return rows


def _printable(text):
    """Controls, Unicode format characters (bidi overrides and the like) and line separators become '?'."""
    return "".join("?" if ord(c) < 32 or 127 <= ord(c) < 160 or unicodedata.category(c) in ("Cf", "Zl", "Zp") else c
                   for c in text)


def _version(chars):
    """'4650' -> '4.6.5': the first three characters, each a base-36 digit; None when they are not."""
    parts = []
    for c in chars[:3]:
        try:
            parts.append(str(int(c, 36)))
        except ValueError:
            return None
    return ".".join(parts)


def client_name(peer_id, ext_v):
    """The name to show for a peer: its extension handshake `v` (cleaned, cut), else its peer id prefix, else '?'."""
    if ext_v:
        name = _printable(ext_v.strip())[:CLIENT_CHARS]
        if name:
            return name
    if isinstance(peer_id, bytes) and len(peer_id) >= 8 and peer_id[0:1] == b"-" and peer_id[7:8] == b"-":
        try:
            code, digits = peer_id[1:3].decode("ascii"), peer_id[3:7].decode("ascii")
        except UnicodeDecodeError:
            return "?"
        name, version = CLIENTS.get(code), _version(digits)
        if name and version:
            return f"{name} {version}"
    return "?"
