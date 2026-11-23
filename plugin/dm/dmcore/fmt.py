"""Formatting helpers: sizes, URLs safe to show, progress throttle."""

import time
from urllib.parse import urlsplit

_UNITS = ["B", "kB", "MB", "GB", "TB"]


def human_size(n):
    """Decimal sizes: 512 B, 1.5 kB, 10.0 MB, 1.2 GB."""
    if n < 1000:
        return f"{n} B"
    value, i = float(n), 0
    while value >= 999.95 and i < len(_UNITS) - 1:
        value /= 1000
        i += 1
    return f"{value:.1f} {_UNITS[i]}"


def display_url(url):
    """scheme://host[:port]/path: no credentials, query or fragment."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        port = f":{parts.port}" if parts.port else ""
    except ValueError:
        return url.split("?", 1)[0].split("#", 1)[0].rpartition("@")[2]
    return f"{parts.scheme}://{host}{port}{parts.path}"


class Throttle:
    """ready() is true at most once per interval; final=True always passes."""

    def __init__(self, interval=0.1, clock=time.monotonic):
        self.interval = interval
        self.clock = clock
        self._last = None

    def ready(self, final=False):
        now = self.clock()
        if final or self._last is None or now - self._last >= self.interval - 1e-9:
            self._last = now
            return True
        return False
