"""dock's own threads never take the terminal's signals, so the main thread, where Python runs the handlers, gets them.

A process-wide signal (Ctrl-Z, fg, a resize, Ctrl-C) goes to any thread that does not block it. Python runs the handler
only on the main thread, and only once that thread runs again: when the signal lands on a helper thread while the main
thread waits in a read, Ctrl-Z does nothing and `fg` never draws the region again.
"""

import os
import signal
import threading
import unittest

from src import term

POSIX = hasattr(signal, "pthread_sigmask")


def mask_in(start) -> set:
    """The signal mask of a thread that `start(target)` starts."""
    seen = {}

    def target():
        seen["mask"] = signal.pthread_sigmask(signal.SIG_BLOCK, [])

    t = start(target)
    t.join(5)
    return set(seen["mask"])


@unittest.skipUnless(POSIX, "needs pthread_sigmask")
class Thread(unittest.TestCase):
    def test_a_helper_thread_blocks_the_terminal_signals(self):
        mask = mask_in(lambda f: term.thread(f, name="probe"))
        for sig in term.TERMINAL_SIGNALS:
            self.assertIn(sig, mask, signal.Signals(sig).name)

    def test_the_main_thread_mask_is_left_as_it_was(self):
        before = signal.pthread_sigmask(signal.SIG_BLOCK, [])
        term.thread(lambda: None, name="probe").join(5)
        self.assertEqual(signal.pthread_sigmask(signal.SIG_BLOCK, []), before)

    def test_it_is_started_and_named_and_a_daemon(self):
        ran = threading.Event()
        t = term.thread(ran.set, name="probe")
        t.join(5)
        self.assertTrue(ran.is_set())
        self.assertEqual(t.name, "probe")
        self.assertTrue(t.daemon)

    def test_a_process_signal_reaches_the_main_thread_while_it_waits_in_a_read(self):
        r, w = os.pipe()
        got = []
        old = signal.signal(signal.SIGWINCH, lambda *a: got.append(threading.current_thread() is threading.main_thread()))
        try:
            # the helper sends the signal to the process and waits; only the main thread may take it
            t = term.thread(lambda: (os.kill(os.getpid(), signal.SIGWINCH), os.write(w, b"x")), name="probe")
            os.read(r, 1)  # Python runs the handler here, between the interrupted read and its retry
            t.join(5)
            self.assertEqual(got, [True])
        finally:
            signal.signal(signal.SIGWINCH, old)
            os.close(r)
            os.close(w)


class Users(unittest.TestCase):
    """The threads dock starts go through term.thread."""

    def test_the_render_thread_and_the_plugin_threads_use_it(self):
        import inspect

        from src import live, process

        for mod in (live, process):
            src = inspect.getsource(mod)
            self.assertNotIn("threading.Thread(", src, f"{mod.__name__} starts a thread that may take signals")
            self.assertIn("term.thread(", src.replace("from src.term import thread", "term.thread("), mod.__name__)


if __name__ == "__main__":
    unittest.main()
