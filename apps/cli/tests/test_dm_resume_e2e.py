"""dm HTTP resume end to end: dock, the real container runtime, the dm image and the test server container."""

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT.parent.parent / "plugins" / "dm" / "tests" / "fixtures" / "dmserver"))

import dockenv
import server as dmserver

sys.path.insert(0, str(ROOT.parent.parent / "plugins" / "dm"))
from dmcore.fmt import human_size

DM = ROOT.parent.parent / "plugins" / "dm"
SERVER_DIR = ROOT.parent.parent / "plugins" / "dm" / "tests" / "fixtures" / "dmserver"
SIZE = 4_000_000
TAIL = 1 << 20  # dm re-fetches this much of a partial


def sha(data):
    return hashlib.sha256(data).hexdigest()


class DmResumeE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rt = dockenv.require_runtime()
        dockenv.prebuild_plugins(DM)
        cls.home = dockenv.make_home("dock-dmr-", DM)
        cls.addClassCleanup(shutil.rmtree, cls.home, ignore_errors=True)
        cls.base = Path(tempfile.mkdtemp(prefix="dock-dmr-work-")).resolve()
        cls.addClassCleanup(shutil.rmtree, cls.base, ignore_errors=True)
        r = subprocess.run([cls.rt, "run", "-d", "--rm", "-v", f"{SERVER_DIR}:/srv:ro", "--label", "dock-test=dmserver",
                            "python:3.15.2-slim", "python3", "-u", "/srv/server.py", "--max-seconds", "900"],
                           capture_output=True, text=True, timeout=600, check=False)
        if r.returncode != 0:
            raise AssertionError(f"cannot start the test server container: {r.stderr}")
        cid = cls.cid = r.stdout.strip().splitlines()[-1]
        cls.addClassCleanup(dockenv.kill_container, cid, cls.rt)
        end = time.monotonic() + 30
        while True:
            logs = subprocess.run([cls.rt, "logs", cid], capture_output=True, text=True, timeout=60, check=False)
            if "ready" in logs.stdout + logs.stderr:
                break
            if time.monotonic() > end:
                raise AssertionError(f"the test server did not print 'ready' within 30 s: {logs.stdout}{logs.stderr}")
            time.sleep(0.3)
        ip = ""
        for fmt in ("{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", "{{.NetworkSettings.IPAddress}}"):
            ip = subprocess.run([cls.rt, "inspect", "-f", fmt, cid], capture_output=True, text=True,
                                timeout=60, check=False).stdout.strip()
            if ip:
                break
        if not ip:
            raise AssertionError("cannot find the IP of the test server container")
        cls.url = f"http://{ip}:8000"

    @classmethod
    def env(cls):
        return {**os.environ, "DOCK_HOME": str(cls.home), "PYTHONPATH": str(ROOT)}

    @classmethod
    def popen(cls, *args, cwd):
        return subprocess.Popen([sys.executable, "-m", "src", *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, env=cls.env(), cwd=cwd, **dockenv.SIGINT_DEFAULT)

    @classmethod
    def dock(cls, *args, cwd, timeout=180):
        return subprocess.run([sys.executable, "-m", "src", *args], capture_output=True, text=True, env=cls.env(),
                              cwd=cwd, timeout=timeout, check=False, **dockenv.SIGINT_DEFAULT)

    def setUp(self):
        self.cwd = Path(tempfile.mkdtemp(prefix="w-", dir=self.base))

    def start(self, url):
        p = self.popen("dm", url, cwd=self.cwd)
        self.addCleanup(lambda: (p.poll() is None and p.kill(), p.communicate()))
        return p

    def server_log(self):
        code = "import urllib.request as u;print(u.urlopen('http://127.0.0.1:8000/_log').read().decode())"
        r = subprocess.run([self.rt, "exec", self.cid, "python3", "-c", code], capture_output=True, text=True,
                           timeout=60, check=False)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def ranges(self, name):
        return [e["range"] for e in self.server_log() if e["path"] == f"/range/{name}"]

    def parts(self, suffix=".dm-part"):
        return sorted(self.cwd.glob(f".*{suffix}"))

    def wait_partial(self, minimum, p):
        """Wait until the partial holds at least `minimum` bytes."""
        end = time.monotonic() + 60
        while True:
            self.assertIsNone(p.poll(), "download ended too early")
            parts = self.parts()
            if parts and parts[0].stat().st_size >= minimum:
                return
            self.assertLess(time.monotonic(), end, "the partial did not grow")
            time.sleep(0.05)

    # AC1, NFR3
    def test_ctrl_c_keeps_the_partial_and_the_next_run_resumes(self):
        name = "ctrlc.bin"
        p = self.start(f"{self.url}/range/{name}?size={SIZE}&slow=10")
        self.wait_partial(SIZE // 2, p)
        sent = time.monotonic()
        p.send_signal(signal.SIGINT)
        try:
            p.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            self.fail("dock did not exit within 15 s of Ctrl-C")
        took = time.monotonic() - sent
        self.assertEqual(p.returncode, 130)
        self.assertLess(took, 2, f"dock took {took:.1f} s to exit after Ctrl-C")
        self.assertTrue(dockenv.wait_clients_gone(self.home), "a plugin container is still running")
        part, record = self.parts(), self.parts(".dm-resume")
        self.assertEqual((len(part), len(record)), (1, 1))
        size = part[0].stat().st_size
        self.assertGreaterEqual(size, SIZE // 2)
        self.assertLess(size, SIZE)
        r = self.dock("dm", f"{self.url}/range/{name}?size={SIZE}&slow=10", cwd=self.cwd)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"resuming from {human_size(size - TAIL)} of 4.0 MB", r.stdout)
        self.assertIn(f"saved {name} (4.0 MB)", r.stdout)
        self.assertEqual(self.ranges(name), [None, f"bytes={size - TAIL}-"])
        self.assertEqual(sorted(f.name for f in self.cwd.iterdir()), [name])
        self.assertEqual(sha((self.cwd / name).read_bytes()), sha(dmserver.content(SIZE)))

    # AC2
    def test_dropped_connection_keeps_the_partial_and_the_next_run_resumes(self):
        name = "drop.bin"
        url = f"{self.url}/range/{name}?size={SIZE}&drop_at=2500000"
        r = self.dock("dm", url, cwd=self.cwd)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("connection closed early", r.stderr)
        self.assertEqual([f.stat().st_size for f in self.parts()], [2_500_000])
        self.assertEqual(len(self.parts(".dm-resume")), 1)
        r = self.dock("dm", url, cwd=self.cwd)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"resuming from {human_size(2_500_000 - TAIL)} of 4.0 MB", r.stdout)
        self.assertEqual(self.ranges(name), [None, f"bytes={2_500_000 - TAIL}-"])
        self.assertEqual(sorted(f.name for f in self.cwd.iterdir()), [name])
        self.assertEqual(sha((self.cwd / name).read_bytes()), sha(dmserver.content(SIZE)))

    def test_no_resume_flag_starts_over(self):
        name = "fresh.bin"
        url = f"{self.url}/range/{name}?size={SIZE}&drop_at=700000"
        self.assertEqual(self.dock("dm", url, cwd=self.cwd).returncode, 1)
        r = self.dock("dm", "--no-resume", url, cwd=self.cwd)
        self.assertEqual(r.returncode, 1, r.stderr)  # the server drops a request that starts at 0 again
        self.assertEqual([f.stat().st_size for f in self.parts()], [700_000])
        self.assertEqual(self.ranges(name), [None, None])

    # AC11: needs flock to work on the bind-mounted folder, which it does on the runtimes this suite runs on
    def test_second_run_in_the_same_folder_fails_at_once_and_the_first_completes(self):
        name = "twice.bin"
        url = f"{self.url}/range/{name}?size={SIZE}&slow=20"
        first = self.start(url)
        self.wait_partial(1, first)
        started = time.monotonic()
        r = self.dock("dm", url, cwd=self.cwd)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("already being downloaded in this folder", r.stderr)
        self.assertLess(time.monotonic() - started, 15)
        self.assertEqual(first.poll(), None, "the first run was disturbed")
        out, err = first.communicate(timeout=120)
        self.assertEqual(first.returncode, 0, err)
        self.assertIn(f"saved {name}", out)
        self.assertEqual(sorted(f.name for f in self.cwd.iterdir()), [name])
        self.assertEqual(sha((self.cwd / name).read_bytes()), sha(dmserver.content(SIZE)))
        self.assertEqual(self.ranges(name), [None])


if __name__ == "__main__":
    unittest.main()
