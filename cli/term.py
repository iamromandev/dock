"""The real terminal: input modes, one write lock, size, and getting everything back on every way out.

One Terminal per dock process, and only in live mode. It is the only place that calls termios, installs signal
handlers, or writes live output to the terminal.

Locking: `lock` is re-entrant and guards every write, and the state the live renderer shares with its thread.
A signal handler runs on the main thread, possibly while that thread holds the lock, so a handler only takes it
with a timeout (`restore`) or touches a flag (SIGWINCH): it can never deadlock.
"""

from __future__ import annotations

import atexit
import contextlib
import os
import signal
import sys
import threading
import time
from collections.abc import Callable

try:
    import termios
except ImportError:  # Windows: there is no live mode
    termios = None  # type: ignore[assignment]

SHOW_CURSOR = "\x1b[?25h"
LOCK_WAIT = 0.2  # seconds a signal handler waits for the lock before it writes anyway


class Terminal:
    def __init__(self, fd_in: int = 0, fd_out: int = 2):
        self.fd_in, self.fd_out = fd_in, fd_out
        self._ctl = fd_in                           # the terminal whose modes are changed (see install)
        self.lock = threading.RLock()
        self.resized = False                        # set by SIGWINCH; the renderer takes it (take_resized)
        self.resized_at = 0.0                       # time.monotonic() of that signal
        self.erase_region: Callable[[], None] | None = None  # erases what is drawn, called by restore()
        self.on_resume: Callable[[], None] | None = None     # redraw, called after Ctrl-Z and `fg`
        self._saved = None                          # termios settings at install(); None: no terminal on stdin
        self._mode = "cooked"
        self._altered = False                       # the terminal's modes are not what they were at install()
        self._installed = False
        self._forcing = False                       # restore() gave up waiting for the lock: writes skip it
        self._old_handlers: dict[int, object] = {}
        self._old_excepthook = None

    # size and output -----------------------------------------------------------
    def size(self) -> tuple[int, int]:
        """(columns, rows) of the output terminal; (80, 24) when it cannot say."""
        try:
            cols, rows = os.get_terminal_size(self.fd_out)
        except OSError:
            return 80, 24
        return (cols or 80), (rows or 24)

    def write(self, s: str) -> None:
        """Write to the terminal in one piece, under the lock. A terminal that is gone is not an error."""
        data = s.encode("utf-8", "replace")
        if self._forcing:
            self._send(data)
            return
        with self.lock:
            self._send(data)

    def _send(self, data: bytes) -> None:
        try:
            while data:
                data = data[os.write(self.fd_out, data):]
        except OSError:
            pass

    def take_resized(self) -> bool:
        was, self.resized = self.resized, False
        return was

    # input modes -------------------------------------------------------------------
    def _foreground(self) -> bool:
        """Is this process in the terminal's foreground group? From a background job (`dock dm url &`), changing
        the terminal's modes stops the job (SIGTTOU), and would change them under the job that is reading them."""
        try:
            return os.tcgetpgrp(self._ctl) == os.getpgrp()
        except OSError:
            return True  # not a terminal, or no way to tell: the modes do nothing, or fail quietly

    def _apply(self, mode: str) -> None:
        self._mode = mode
        if self._saved is None or not self._foreground():
            return
        attrs = [*self._saved[:6], list(self._saved[6])]
        if mode != "cooked":
            attrs[3] &= ~(termios.ICANON | termios.ECHO)  # lflag
            attrs[6][termios.VMIN], attrs[6][termios.VTIME] = 1, 0
        if mode == "raw":
            attrs[3] &= ~(termios.ISIG | termios.IEXTEN)
            attrs[0] &= ~termios.IXON  # Ctrl-S and Ctrl-Q are keys here, not flow control
        try:
            termios.tcsetattr(self._ctl, termios.TCSANOW, attrs)
            self._altered = mode != "cooked"
        except (termios.error, OSError):
            pass

    @contextlib.contextmanager
    def _enter(self, mode: str):
        before = self._mode
        self._apply(mode)
        try:
            yield
        finally:
            self._apply(before)

    def raw(self):
        """No line editing, echo or signals from keys: dock reads every key (the editor, live forms)."""
        return self._enter("raw")

    def quiet(self):
        """No line editing and no echo, but Ctrl-C and Ctrl-Z still signal: while a command runs."""
        return self._enter("quiet")

    def cooked(self):
        """The settings the terminal had at install (line editing and echo): for prompts that use input()."""
        return self._enter("cooked")

    def flush_input(self) -> None:
        """Throw away keys typed and not read yet (not from a background job: they are not its keys)."""
        if not self._foreground():
            return
        try:
            termios.tcflush(self._ctl, termios.TCIFLUSH)
        except (termios.error, OSError):
            pass

    # leaving ---------------------------------------------------------------------
    def install(self) -> None:
        """Save the settings and make sure every way out gives them back."""
        if self._installed:
            return
        self._installed = True
        # With stdin redirected but the output on a terminal, the keys typed meanwhile would still be echoed into the
        # drawing: the modes go to that terminal (it is the one stderr is). No key is read from it then: forms are plain.
        for fd in dict.fromkeys((self.fd_in, self.fd_out)):
            try:
                self._saved, self._ctl = termios.tcgetattr(fd), fd
                break
            except (termios.error, OSError):
                self._saved = None  # not a terminal: modes do nothing
        atexit.register(self.restore)
        self._old_excepthook = sys.excepthook
        sys.excepthook = self._excepthook  # erase the region before a traceback is printed under it
        if threading.current_thread() is threading.main_thread():
            self._install_handlers()

    def _install_handlers(self) -> None:
        for sig, handler in ((signal.SIGTERM, self._die), (signal.SIGHUP, self._die), (signal.SIGQUIT, self._die),
                             (signal.SIGTSTP, self._suspended), (signal.SIGCONT, self._continued),
                             (signal.SIGWINCH, self._winch)):
            old = signal.signal(sig, handler)
            self._old_handlers.setdefault(sig, old)

    def _excepthook(self, *exc_info) -> None:
        self.restore()
        (self._old_excepthook or sys.__excepthook__)(*exc_info)

    def restore(self) -> None:
        """Erase the live region, give the terminal its old settings and a visible cursor. Safe to repeat."""
        got = self.lock.acquire(timeout=LOCK_WAIT)  # a handler must not block for ever: write anyway
        self._forcing = not got
        try:
            erase = self.erase_region
            if erase is not None:
                try:
                    erase()
                except Exception:  # noqa: BLE001 - nothing may stop the settings from coming back
                    pass
            if self._saved is not None and self._altered:  # a job that never changed them leaves them alone
                # Put back what we changed even if the job was sent to the background since: SIGTTOU blocked,
                # tcsetattr is allowed there, and a terminal left in raw mode is worse than a stopped job.
                before = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTTOU})
                try:
                    termios.tcsetattr(self._ctl, termios.TCSANOW, self._saved)
                    self._altered = False
                except (termios.error, OSError):
                    pass
                finally:
                    signal.pthread_sigmask(signal.SIG_SETMASK, before)
            self.write(SHOW_CURSOR)
        finally:
            self._forcing = False
            if got:
                self.lock.release()

    def close(self) -> None:
        """restore(), and drop the handlers: the process goes on without a Terminal."""
        self.restore()
        if not self._installed:
            return
        self._installed = False
        with contextlib.suppress(ValueError):
            atexit.unregister(self.restore)
        if sys.excepthook == self._excepthook:
            sys.excepthook = self._old_excepthook or sys.__excepthook__
        if threading.current_thread() is threading.main_thread():
            for sig, old in self._old_handlers.items():
                signal.signal(sig, old if old is not None else signal.SIG_DFL)
        self._old_handlers.clear()

    def suspend(self) -> None:
        """Stop the process like Ctrl-Z does (for a Ctrl-Z that arrives as a key, in raw mode)."""
        os.kill(os.getpid(), signal.SIGTSTP)

    # signal handlers ---------------------------------------------------------------
    def _die(self, signum, frame) -> None:
        self.restore()
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    def _suspended(self, signum, frame) -> None:
        self.restore()
        signal.signal(signal.SIGTSTP, signal.SIG_DFL)
        os.kill(os.getpid(), signal.SIGTSTP)  # stops here; SIGCONT carries on in _continued

    def _continued(self, signum, frame) -> None:
        signal.signal(signal.SIGTSTP, self._suspended)
        self._apply(self._mode)
        self.resized = True  # the window may have changed while stopped
        if self.on_resume is not None:
            self.on_resume()

    def _winch(self, signum, frame) -> None:
        self.resized_at = time.monotonic()
        self.resized = True
