"""The questions a run has put to its client, waiting for answers."""

from __future__ import annotations

import itertools
import queue
import threading


class Asks:
    """ask id -> the queue its asker blocks on. One per run; `ids` is shared by the connection so ids never repeat."""

    def __init__(self, ids: itertools.count | None = None):
        self._ids = ids if ids is not None else itertools.count(1)
        self._lock = threading.Lock()
        self._waiting: dict[int, queue.Queue] = {}
        self._released = False

    def ask(self, send, make_frame) -> dict:
        """Register a question, send `make_frame(ask_id)`, block for the answer; {"eof": True} once released."""
        q: queue.Queue = queue.Queue(1)
        with self._lock:
            if self._released:
                return {"eof": True}
            ask_id = next(self._ids)
            self._waiting[ask_id] = q
        try:
            frame = make_frame(ask_id)
            with self._lock:
                if self._released:  # cancelled before the frame went out: the client never sees this ask
                    return {"eof": True}
            send(frame)
            return q.get()
        finally:
            with self._lock:
                self._waiting.pop(ask_id, None)

    def answer(self, ask_id, result: dict) -> bool:
        """Deliver an answer; False when no such ask is waiting (unknown, already answered or released)."""
        with self._lock:
            q = self._waiting.pop(ask_id, None) if type(ask_id) is int else None
        if q is None:
            return False
        q.put(result)
        return True

    def release_all(self) -> None:
        """Wake every waiting ask with {"eof": True}; later asks of this run answer eof at once."""
        with self._lock:
            self._released = True
            waiting, self._waiting = list(self._waiting.values()), {}
        for q in waiting:
            q.put({"eof": True})
