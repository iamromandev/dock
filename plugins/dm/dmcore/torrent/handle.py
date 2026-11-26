"""Lets the main thread abort a running torrent download. The worker decides what to keep."""

import threading


class TorrentHandle:
    """Same contract as fetch.Handle (lock, aborted, placed, emit, abort), plus the loop to stop."""

    def __init__(self):
        self.lock = threading.Lock()
        self.aborted = False
        self.placed = False
        self.loop = None
        self.main_task = None

    def emit(self, fn, *args):
        """Call fn unless aborted; checked under the lock abort() takes."""
        with self.lock:
            if not self.aborted:
                fn(*args)

    def attach(self, loop, task):
        """Called once the session's loop and main task exist; stops it at once if already aborted."""
        with self.lock:
            self.loop, self.main_task = loop, task
            aborted = self.aborted
        if aborted:
            self._stop()

    def _stop(self):
        loop, task = self.loop, self.main_task
        if loop is not None and task is not None:
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:  # the loop is already closed
                pass

    def abort(self):
        """Stop the download; nothing is deleted here. False if it was already moved into place."""
        with self.lock:
            if self.placed:
                return False
            self.aborted = True
        self._stop()
        return True

