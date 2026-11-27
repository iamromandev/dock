"""Container isolation against the real container runtime.

The recorded-runtime tests in test_isolation.py prove dock's wiring; this proves that the flags it
passes really confine a plugin: no network, no cwd, no host environment. Without a runtime it fails
with one clear message (it never skips).
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import dockenv
from dock_host.store import Store

FIXTURES = ROOT / "tests" / "fixtures"
BUILD_TIMEOUT = 1200  # the first build pulls base images
RUN_TIMEOUT = 300
HOST_SECRET = "host-secret-value"


class RealDocker(unittest.TestCase):
    """dock and the real container runtime, in a throwaway DOCK_HOME."""

    @classmethod
    def setUpClass(cls):
        dockenv.require_runtime()
        cls.tmp = Path(tempfile.mkdtemp(prefix="dock-docker-")).resolve()
        cls.addClassCleanup(shutil.rmtree, cls.tmp, ignore_errors=True)
        store = Store(cls.tmp / "home")
        wrapper, _ = dockenv.recording_runtime(cls.tmp)
        cls.env = {
            **os.environ,
            "DOCK_HOME": str(store.home),
            "PYTHONPATH": str(ROOT),
            "DOCK_TEST_HOST_SECRET": HOST_SECRET,
        }
        m = store.install(FIXTURES / "jail")
        built = cls.run_dock("plugin", "build", m.id, cwd=ROOT, timeout=BUILD_TIMEOUT)
        if built.returncode != 0:
            raise AssertionError(f"plugin build {m.id} failed: {built.stderr}\n{built.stdout}")
        dockenv.write_config(store.home, wrapper)

    @classmethod
    def run_dock(cls, *args, cwd, timeout=RUN_TIMEOUT):
        p = subprocess.Popen(
            [sys.executable, "-m", "src", *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=cls.env,
            cwd=cwd,
            **dockenv.SIGINT_DEFAULT,
        )
        try:
            out, err = p.communicate("", timeout=timeout)
        except subprocess.TimeoutExpired:
            p.kill()
            p.communicate()
            raise
        return subprocess.CompletedProcess(p.args, p.returncode, out, err)

    def jail(self, work: Path, *args: str) -> str:
        r = self.run_dock(f"dev.dock.jail:{args[0]}", *args[1:], cwd=work)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    # NFR1: a plugin without permissions has no network, no cwd and no host environment
    def test_a_plugin_without_permissions_is_confined(self):
        work = Path(tempfile.mkdtemp(prefix="dock-work-", dir=self.tmp))
        (work / "secret.txt").write_text("top secret")
        with self.subTest("no network"):
            self.assertEqual(self.jail(work, "net"), "blocked")
        with self.subTest("no cwd"):
            self.assertEqual(self.jail(work, "readcwd", "secret.txt"), "unreadable")
        with self.subTest("no host environment"):
            self.assertEqual(self.jail(work, "env", "DOCK_TEST_HOST_SECRET"), "unset")
            self.assertEqual(self.jail(work, "env", "HOME"), "set:/tmp")  # really inside the container


if __name__ == "__main__":
    unittest.main()
