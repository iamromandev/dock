"""src.term.Terminal without a pty: the parts that do not need a terminal."""

import os
import signal
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.term import SHOW_CURSOR, Terminal


@unittest.skipUnless(hasattr(signal, "SIGTSTP"), "needs a POSIX terminal")
class TerminalWithoutATty(unittest.TestCase):
    def setUp(self):
        self.r, self.w = os.pipe()
        self.addCleanup(os.close, self.r)
        self.addCleanup(os.close, self.w)
        self.null = os.open(os.devnull, os.O_RDONLY)
        self.addCleanup(os.close, self.null)
        self.term = Terminal(fd_in=self.null, fd_out=self.w)  # stdin is no terminal: modes do nothing

    def written(self):
        os.set_blocking(self.r, False)
        try:
            return os.read(self.r, 65536).decode()
        except BlockingIOError:
            return ""

    def test_size_falls_back_to_80_by_24_when_the_output_is_no_terminal(self):
        self.assertEqual(self.term.size(), (80, 24))

    def test_write_sends_everything_even_a_large_piece(self):
        big = "x" * 300_000
        reader = []

        def read_all():
            got = 0
            while got < len(big):
                got += len(os.read(self.r, 65536))
            reader.append(got)

        t = threading.Thread(target=read_all)
        t.start()
        self.term.write(big)
        t.join(10)
        self.assertEqual(reader, [len(big)])

    def test_write_to_a_closed_terminal_is_not_an_error(self):
        Terminal(fd_in=self.null, fd_out=self.null).write("hello")  # a read-only fd: every write fails

    def test_modes_do_nothing_and_do_not_fail_without_a_terminal(self):
        self.term.install()
        self.addCleanup(self.term.close)
        with self.term.raw(), self.term.quiet(), self.term.cooked():
            pass
        self.term.flush_input()

    def test_restore_shows_the_cursor_and_erases_the_region_first_and_twice_is_fine(self):
        calls = []
        self.term.erase_region = lambda: (calls.append("erase"), self.term.write("<erased>"))
        self.term.restore()
        self.term.restore()
        self.assertEqual(self.written(), "<erased>" + SHOW_CURSOR + "<erased>" + SHOW_CURSOR)
        self.assertEqual(calls, ["erase", "erase"])

    def test_a_failing_eraser_does_not_stop_the_restore(self):
        def boom():
            raise RuntimeError("bug")

        self.term.erase_region = boom
        self.term.restore()
        self.assertEqual(self.written(), SHOW_CURSOR)

    def test_restore_does_not_wait_for_ever_for_a_lock_another_thread_holds(self):
        """A signal handler runs restore() on the main thread while the renderer may hold the lock."""
        held, release = threading.Event(), threading.Event()

        def holder():
            with self.term.lock:
                held.set()
                release.wait(10)

        h = threading.Thread(target=holder, daemon=True)
        h.start()
        held.wait(5)
        self.term.erase_region = lambda: self.term.write("<erased>")
        t = threading.Thread(target=self.term.restore, daemon=True)
        t.start()
        t.join(3)
        stuck = t.is_alive()
        release.set()
        h.join(5)
        t.join(5)
        self.assertFalse(stuck, "restore() blocked on the lock")
        self.assertEqual(self.written(), "<erased>" + SHOW_CURSOR)

    def test_close_gives_back_the_excepthook_and_the_handlers(self):
        before_hook = sys.excepthook
        before = signal.getsignal(signal.SIGTERM)
        self.term.install()
        self.assertNotEqual(sys.excepthook, before_hook)
        self.term.close()
        self.assertEqual(sys.excepthook, before_hook)
        self.assertEqual(signal.getsignal(signal.SIGTERM), before)

    def test_resize_flag_is_taken_once(self):
        self.term.resized = True
        self.assertTrue(self.term.take_resized())
        self.assertFalse(self.term.take_resized())


if __name__ == "__main__":
    unittest.main()
