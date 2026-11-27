"""Container isolation against the real container runtime.

The recorded-runtime tests in test_isolation.py prove dock's wiring; this proves that the flags it
passes really confine a plugin: no network, no cwd, no host environment. Without a runtime it fails
with one clear message (it never skips).
"""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import dockenv
from dock_host.lifecycle import Session
from dock_host.store import Store
from test_lifecycle_seams import RecordingUI

FIXTURES = ROOT / "tests" / "fixtures"
HOST_SECRET = "host-secret-value"


class RealDocker(unittest.TestCase):
    """dock_host and the real container runtime, in a throwaway DOCK_HOME."""

    @classmethod
    def setUpClass(cls):
        dockenv.require_runtime()
        cls.tmp = Path(tempfile.mkdtemp(prefix="dock-docker-")).resolve()
        cls.addClassCleanup(shutil.rmtree, cls.tmp, ignore_errors=True)
        cls.store = Store(cls.tmp / "home", notice=lambda _message: None)
        wrapper, _ = dockenv.recording_runtime(cls.tmp)
        env = mock.patch.dict(os.environ, {"DOCK_TEST_HOST_SECRET": HOST_SECRET})
        env.start()
        cls.addClassCleanup(env.stop)
        cls.manifest = cls.store.install(FIXTURES / "jail")
        dockenv.write_config(cls.store.home, dockenv.require_runtime())
        dockenv.prebuild(cls.store.home, [cls.manifest.id])
        dockenv.write_config(cls.store.home, wrapper)

    def jail(self, work: Path, *args: str) -> str:
        """One command of the jail plugin, run with `work` as the current folder; what the plugin printed."""
        events: list = []
        session = Session(self.store, lambda plugin_id: RecordingUI(plugin_id, events))
        self.addCleanup(session.close)
        cwd = os.getcwd()
        os.chdir(work)
        try:
            code = session.invoke(self.manifest, args[0], list(args[1:]))
        finally:
            os.chdir(cwd)
        self.assertEqual(code, 0, events)
        return "".join(text for _, kind, text in events if kind == "output").strip()

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
