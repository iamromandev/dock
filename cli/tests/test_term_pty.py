"""src.term.Terminal in a real pty: the terminal is restored on every way out (R18, NFR2, AC13)."""

import os
import select
import signal
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

import ptykit

DRIVER = str(ROOT / "tests" / "fixtures" / "termdriver.py")


def _env():
    return {**os.environ, "PYTHONPATH": str(ROOT), "TERM": "xterm-256color"}


@unittest.skipUnless(ptykit.AVAILABLE, "needs pty and termios")
class TerminalRestore(unittest.TestCase):
    def start(self, mode):
        p = ptykit.Pty([sys.executable, DRIVER, mode], env=_env(), cwd=ROOT)
        self.addCleanup(p.close)
        p.wait_text("READY")
        return p

    def assert_restored(self, p):
        self.assertEqual(p.termios(), p.initial_termios, "termios is not what it was before")
        self.assertTrue(p.screen.cursor_visible, "the cursor is still hidden")

    def test_raw_mode_changes_the_terminal_while_it_runs(self):
        p = self.start("wait")
        lflag = p.termios()[3]
        self.assertFalse(lflag & ptykit.termios.ICANON)
        self.assertFalse(lflag & ptykit.termios.ECHO)
        self.assertFalse(lflag & ptykit.termios.ISIG)

    def test_quiet_mode_keeps_signals_on(self):
        p = self.start("quiet")
        lflag = p.termios()[3]
        self.assertFalse(lflag & ptykit.termios.ICANON)
        self.assertFalse(lflag & ptykit.termios.ECHO)
        self.assertTrue(lflag & ptykit.termios.ISIG)

    def test_with_stdin_redirected_the_modes_are_applied_to_the_terminal_stderr_is(self):
        p = ptykit.Pty(["sh", "-c", f'exec "{sys.executable}" "{DRIVER}" quiet </dev/null'], env=_env(), cwd=ROOT)
        self.addCleanup(p.close)
        p.wait_text("READY")
        lflag = p.termios()[3]
        self.assertFalse(lflag & ptykit.termios.ECHO, "keys typed meanwhile would be echoed into the region")
        self.assertFalse(lflag & ptykit.termios.ICANON)
        self.assertTrue(lflag & ptykit.termios.ISIG, "Ctrl-C must still cancel")
        p.signal(signal.SIGTERM)
        self.assertEqual(p.wait_exit(), -signal.SIGTERM)
        self.assert_restored(p)

    def test_normal_exit_restores(self):
        p = self.start("normal")
        self.assertEqual(p.wait_exit(), 0)
        self.assert_restored(p)

    def test_exception_restores_and_the_traceback_is_shown(self):
        p = self.start("raise")
        self.assertEqual(p.wait_exit(), 1)
        p.wait_text("RuntimeError: boom")
        self.assert_restored(p)

    def test_sigterm_restores_and_dies_of_it(self):
        p = self.start("wait")
        p.signal(signal.SIGTERM)
        self.assertEqual(p.wait_exit(), -signal.SIGTERM)
        self.assert_restored(p)
        self.assertIn(b"<erased>", p.data)  # the live region was erased first

    def test_sighup_restores_and_dies_of_it(self):
        p = self.start("wait")
        p.signal(signal.SIGHUP)
        self.assertEqual(p.wait_exit(), -signal.SIGHUP)
        self.assert_restored(p)

    def test_sigquit_restores_and_dies_of_it(self):
        p = self.start("wait")
        p.signal(signal.SIGQUIT)
        self.assertEqual(p.wait_exit(), -signal.SIGQUIT)
        self.assert_restored(p)
        self.assertIn(b"<erased>", p.data)

    def test_ctrl_backslash_in_quiet_mode_restores_the_terminal(self):
        p = self.start("quiet")
        p.send("\x1c")  # ISIG is on in quiet mode: the pty turns it into SIGQUIT
        self.assertEqual(p.wait_exit(), -signal.SIGQUIT)
        self.assert_restored(p)

    def test_ctrl_z_restores_while_stopped_and_fg_reenters_the_mode(self):
        p = self.start("quiet")
        p.send("\x1a")  # ISIG is on in quiet mode: the pty turns it into SIGTSTP
        p.wait_stopped()
        self.assert_restored(p)
        p.cont()
        p.wait_text("<resumed>")
        lflag = p.termios()[3]
        self.assertFalse(lflag & ptykit.termios.ICANON, "quiet mode was not entered again")
        self.assertTrue(lflag & ptykit.termios.ISIG)
        p.signal(signal.SIGTERM)  # the handlers were installed again by the resume
        self.assertEqual(p.wait_exit(), -signal.SIGTERM)
        self.assert_restored(p)

    def test_ctrl_z_in_raw_mode_is_a_key_not_a_signal(self):
        p = self.start("wait")
        p.send("\x1a")
        p.quiet_for(0.3)
        self.assertEqual(p.events, [f"pid {p.pid}"], "the job stopped or ended")
        p.signal(signal.SIGTERM)
        p.wait_exit()


# A session leader on a new pty that starts the job in a background process group (as `dock dm url &` does) and
# says whether it ran to its end or was stopped by SIGTTOU.
_BACKGROUND = r"""
import os, sys, fcntl, termios, signal, subprocess, time
os.setsid()
fcntl.ioctl(0, termios.TIOCSCTTY, 0)
signal.signal(signal.SIGTTOU, signal.SIG_IGN)  # the launcher, like a job-control shell, is not stopped by it
def job():
    os.setpgid(0, 0)  # its own group, never made the foreground one
    signal.signal(signal.SIGTTOU, signal.SIG_DFL)
child = subprocess.Popen(sys.argv[2:], preexec_fn=job)
end = time.time() + 20
verdict = "TIMEOUT"
while time.time() < end:
    pid, st = os.waitpid(child.pid, os.WUNTRACED | os.WNOHANG)
    if pid:
        verdict = "STOPPED" if os.WIFSTOPPED(st) else "EXITED %d" % os.WEXITSTATUS(st) if os.WIFEXITED(st) else "KILLED"
        break
    time.sleep(0.05)
if verdict in ("STOPPED", "TIMEOUT"):
    os.kill(child.pid, signal.SIGKILL)
    os.waitpid(child.pid, 0)
open(sys.argv[1], "w").write(verdict)
"""


@unittest.skipUnless(ptykit.AVAILABLE, "needs pty and termios")
class BackgroundJob(unittest.TestCase):
    def test_a_job_in_a_background_process_group_is_not_stopped_by_the_terminal_modes(self):
        import subprocess
        import tempfile
        import threading

        master, slave = os.openpty()
        stop = threading.Event()

        def drain():  # a session leader that exits waits for its output to be read
            while not stop.is_set():
                if select.select([master], [], [], 0.05)[0]:
                    try:
                        os.read(master, 65536)
                    except OSError:
                        return

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        self.addCleanup(reader.join)
        self.addCleanup(stop.set)
        before = ptykit.termios.tcgetattr(master)
        with tempfile.TemporaryDirectory() as tmp:
            verdict = os.path.join(tmp, "verdict")
            subprocess.run([sys.executable, "-c", _BACKGROUND, verdict, sys.executable, DRIVER, "normal"],
                           stdin=slave, stdout=slave, stderr=slave, env=_env(), cwd=ROOT, timeout=60, check=True)
            self.assertEqual(Path(verdict).read_text(), "EXITED 0")
        self.assertEqual(ptykit.termios.tcgetattr(master)[3] & ptykit.termios.ECHO, before[3] & ptykit.termios.ECHO)


if __name__ == "__main__":
    unittest.main()
