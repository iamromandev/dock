"""Helper threads that leave terminal signals to the main thread."""

from __future__ import annotations

import signal
import threading
from collections.abc import Callable

# Process-wide signals the main thread must take: Python runs a handler only there, and only once that thread runs
# again. Delivered to a helper thread while the main thread waits (a read, a queue), Ctrl-Z or `fg` would do nothing.
TERMINAL_SIGNALS = tuple(getattr(signal, n) for n in ("SIGINT", "SIGTERM", "SIGHUP", "SIGQUIT", "SIGTSTP", "SIGCONT",
                                                       "SIGWINCH") if hasattr(signal, n))


def thread(target: Callable[[], object], name: str | None = None) -> threading.Thread:
    """Start a daemon thread that blocks TERMINAL_SIGNALS, so the kernel hands them to the main thread instead."""
    t = threading.Thread(target=target, name=name, daemon=True)
    if not hasattr(signal, "pthread_sigmask"):  # Windows: no signal masks, and none of these signals
        t.start()
        return t
    old = signal.pthread_sigmask(signal.SIG_BLOCK, TERMINAL_SIGNALS)  # the new thread inherits the mask
    try:
        t.start()
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, old)
    return t
