"""Run a command in a pseudo-terminal of a given size and look at what it wrote, for tests.

`Pty(argv, cols, rows, env)` starts a small launcher as the session leader of a new pty. The launcher runs the
command as a foreground job of its own process group, like a shell does, so Ctrl-C, Ctrl-Z (the group is not
orphaned, so it really stops), SIGWINCH and `fg` behave as in a real terminal. A reader thread collects the
output into `data` and a `Screen`. Tests wait with `wait_for(...)` and generous timeouts, never fixed sleeps.

`AVAILABLE` is False where `pty` or `termios` cannot be imported; test classes skip on it.
"""

from __future__ import annotations

import os
import select
import signal
import struct
import subprocess
import sys
import threading
import time

try:
    import fcntl
    import termios

    AVAILABLE = True
except ImportError:  # no pty: Windows
    AVAILABLE = False

sys.path.insert(0, os.path.dirname(__file__))
from screenkit import Screen  # noqa: E402

TIMEOUT = 30.0

# Session leader with the pty as controlling terminal; the job runs in its own process group, in the foreground.
# It reports on the events pipe: "pid N", "stopped", "continued", "exit CODE" (CODE negative: killed by a signal).
_LAUNCHER = r"""
import os, sys, fcntl, termios, signal, subprocess
ev, argv = int(sys.argv[1]), sys.argv[2:]
os.setsid()
fcntl.ioctl(0, termios.TIOCSCTTY, 0)
signal.signal(signal.SIGTTOU, signal.SIG_IGN)
def say(s):
    os.write(ev, (s + "\n").encode())
def job():
    os.setpgid(0, 0)
    os.tcsetpgrp(0, os.getpgrp())
    for s in (signal.SIGTTOU, signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(s, signal.SIG_DFL)
child = subprocess.Popen(argv, preexec_fn=job, pass_fds=())
say("pid %d" % child.pid)
while True:
    pid, st = os.waitpid(child.pid, os.WUNTRACED | os.WCONTINUED)
    if os.WIFSTOPPED(st):
        say("stopped")
    elif os.WIFCONTINUED(st):
        say("continued")
    elif os.WIFSIGNALED(st):
        say("exit %d" % -os.WTERMSIG(st))
        break
    else:
        say("exit %d" % os.WEXITSTATUS(st))
        break
"""


def set_winsize(fd: int, cols: int, rows: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


class Pty:
    def __init__(self, argv: list[str], cols: int = 80, rows: int = 24, env: dict | None = None, cwd=None):
        self.cols, self.rows = cols, rows
        self.master, self._slave = os.openpty()  # the parent keeps the slave open: the pty's settings outlive the job
        set_winsize(self.master, cols, rows)
        self.initial_termios = self.termios()
        self.data = b""
        self.screen = Screen(cols, rows)
        self.events: list[str] = []
        self._cv = threading.Condition()
        self._stop = False
        self._ev_r, ev_w = os.pipe()
        self.proc = subprocess.Popen(
            [sys.executable, "-c", _LAUNCHER, str(ev_w), *argv], stdin=self._slave, stdout=self._slave,
            stderr=self._slave, pass_fds=(ev_w,), env=env if env is not None else os.environ.copy(), cwd=cwd)
        os.close(ev_w)
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        pending = b""
        while not self._stop:
            try:
                ready = select.select([self.master, self._ev_r], [], [], 0.1)[0]
                for fd in ready:
                    chunk = os.read(fd, 65536)
                    with self._cv:
                        if fd == self.master:
                            self.data += chunk
                            self.screen.feed(chunk)
                        else:
                            pending += chunk
                            *lines, pending = pending.split(b"\n")
                            self.events += [ln.decode() for ln in lines]
                        self._cv.notify_all()
            except (OSError, ValueError):
                return

    # looking ---------------------------------------------------------------------
    def wait_for(self, condition, timeout: float = TIMEOUT, what: str = "the condition") -> None:
        """Wait until `condition()` is true (it is called under the lock); fail with the screen when it is not."""
        end = time.monotonic() + timeout
        with self._cv:
            while not condition():
                left = end - time.monotonic()
                if left <= 0:
                    raise AssertionError(f"timed out waiting for {what}; the screen is:\n{self.screen.text()}"
                                         f"\nraw: {self.data[-400:]!r}\nevents: {self.events}")
                self._cv.wait(min(left, 0.2))

    def wait_text(self, text: str, timeout: float = TIMEOUT) -> None:
        self.wait_for(lambda: text in self.screen.text(), timeout, f"{text!r}")

    def wait_data(self, needle: bytes, start: int = 0, timeout: float = TIMEOUT) -> int:
        """Wait for `needle` in the raw output after offset `start`; the offset just past it."""
        self.wait_for(lambda: needle in self.data[start:], timeout, f"{needle!r} in the output")
        return start + self.data[start:].index(needle) + len(needle)

    def quiet_for(self, seconds: float = 0.3, timeout: float = TIMEOUT) -> None:
        """Wait until nothing was written for `seconds`."""
        end = time.monotonic() + timeout
        while True:
            with self._cv:
                before = len(self.data)
            time.sleep(seconds)
            with self._cv:
                if len(self.data) == before:
                    return
            if time.monotonic() > end:
                raise AssertionError("the output never went quiet")

    def termios(self) -> list:
        """The pty's current settings (what `stty -g` reports), read on the master side.

        PENDIN is dropped from the local flags: the kernel sets it itself when a terminal leaves line mode, it is
        a state and not a setting.
        """
        attrs = termios.tcgetattr(self.master)
        attrs[3] &= ~getattr(termios, "PENDIN", 0)
        return attrs

    def text(self) -> str:
        with self._cv:
            return self.screen.text()

    # the job ---------------------------------------------------------------------
    @property
    def pid(self) -> int:
        self.wait_for(lambda: any(e.startswith("pid ") for e in self.events), what="the job to start")
        return int(next(e for e in self.events if e.startswith("pid ")).split()[1])

    def send(self, data: bytes | str) -> None:
        os.write(self.master, data.encode() if isinstance(data, str) else data)

    def resize(self, cols: int, rows: int) -> None:
        """Change the size; the kernel sends SIGWINCH to the foreground process group. The screen model keeps
        its content and does not reflow."""
        self.cols, self.rows = cols, rows
        set_winsize(self.master, cols, rows)
        with self._cv:
            s = self.screen
            s.cols, s.rows = cols, rows
            s.grid = [(r + [" "] * cols) if len(r) < cols else r for r in s.grid]  # nothing is cut or reflowed
            while len(s.grid) < rows:
                s.grid.append([" "] * cols)
            while len(s.grid) > rows:
                s.scrollback.append("".join(s.grid.pop(0)).rstrip())
                s.row = max(0, s.row - 1)
            s.row, s.col = min(s.row, rows - 1), min(s.col, cols - 1)

    def signal(self, sig: int) -> None:
        os.kill(self.pid, sig)

    def stopped_count(self) -> int:
        return self.events.count("stopped")

    def wait_stopped(self, after: int = 0, timeout: float = TIMEOUT) -> None:
        """Wait until the job has stopped more than `after` times (Ctrl-Z / SIGTSTP)."""
        self.wait_for(lambda: self.events.count("stopped") > after, timeout, "the job to stop")

    def cont(self) -> None:
        self.signal(signal.SIGCONT)

    def wait_exit(self, timeout: float = TIMEOUT) -> int:
        """The job's exit code (negative: killed by that signal)."""
        self.wait_for(lambda: any(e.startswith("exit ") for e in self.events), timeout, "the job to exit")
        return int(next(e for e in self.events if e.startswith("exit ")).split()[1])

    def close(self) -> None:
        try:
            pid = self.pid if any(e.startswith("pid ") for e in self.events) else None
            if pid and not any(e.startswith("exit ") for e in self.events):
                os.kill(pid, signal.SIGKILL)
        except (OSError, AssertionError):
            pass
        if self.proc.poll() is None:
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self._stop = True
        self._reader.join(timeout=2)
        for fd in (self.master, self._slave, self._ev_r):
            try:
                os.close(fd)
            except OSError:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
