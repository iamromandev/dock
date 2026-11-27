"""Container isolation against the real container runtime.

The recorded-runtime tests in test_isolation.py prove dock's wiring; these prove that the flags it
passes really confine a plugin (no network, no cwd, no host environment) and that the bundled
plugin works in its image. Without a runtime the class fails with one clear message (it never skips).
"""

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import dockenv
from src.store import Store

FIXTURES = ROOT / "tests" / "fixtures"
BUILD_TIMEOUT = 1200  # the first build pulls base images
RUN_TIMEOUT = 300
# The positive control for the no-network test needs the internet; opt in with DOCK_TEST_NETWORK=1.
NETWORK_CONTROL = os.environ.get("DOCK_TEST_NETWORK") == "1"


class RealDocker(unittest.TestCase):
    """dock and the real container runtime, in a throwaway DOCK_HOME."""

    HOST_SECRET = "host-secret-value"

    @classmethod
    def jail_copy(cls, name: str, permissions: list[str]) -> Path:
        """The jail fixture again under another id and with permissions: a control for a restriction."""
        path = cls.tmp / name
        shutil.copytree(FIXTURES / "jail", path, ignore=shutil.ignore_patterns("__pycache__"))
        manifest = (path / "plugin.toml").read_text().replace("dev.dock.jail", f"dev.dock.{name}")
        perms = ", ".join(f'"{p}"' for p in permissions)
        # The jail already has a [capabilities] table (for env): add to it, never open a second one.
        (path / "plugin.toml").write_text(manifest.replace("[capabilities]\n", f"[capabilities]\npermissions = [{perms}]\n"))
        return path

    @classmethod
    def setUpClass(cls):
        dockenv.require_runtime()
        cls.tmp = Path(tempfile.mkdtemp(prefix="dock-docker-")).resolve()
        cls.addClassCleanup(shutil.rmtree, cls.tmp, ignore_errors=True)
        cls.store = Store(cls.tmp / "home")
        # A thin wrapper around the real runtime that records each call, to see what dock asked it to run.
        wrapper, cls.calls = dockenv.recording_runtime(cls.tmp)
        cls.pids = []  # every dock process of this run: their containers are the only ones we judge

        cls.env = {
            **os.environ, "DOCK_HOME": str(cls.store.home), "PYTHONPATH": str(ROOT),
            "DOCK_TEST_HOST_SECRET": cls.HOST_SECRET, "DOCK_TEST_DECLARED": "test-model",
        }
        plugins = [FIXTURES / "jail", cls.jail_copy("jailcwd", ["fs:write:cwd"]),
                   cls.jail_copy("jailin", ["fs:read:in"]),
                   cls.jail_copy("jailboth", ["fs:write:cwd", "fs:read:in"])]
        if NETWORK_CONTROL:
            plugins.append(cls.jail_copy("jailnet", ["network"]))
        for path in plugins:
            m = cls.store.install(path)
            if path.parent == cls.tmp:  # a copy under its own id: ours to delete; jail is shared
                cls.addClassCleanup(dockenv.remove_image, f"dock/{m.id}:{m.version}")
            # Also covers `dock plugin build` for real: the images exist before any test runs.
            built = cls.run_dock("plugin", "build", m.id, cwd=ROOT, timeout=BUILD_TIMEOUT)
            if built.returncode != 0:
                raise AssertionError(f"plugin build {m.id} failed: {built.stderr}\n{built.stdout}")
        dockenv.write_config(cls.store.home, wrapper)  # a first install asks the runtime nothing, so setting the runtime here is only the last step of the setup

    @classmethod
    def run_dock(cls, *args, cwd, stdin="", timeout=RUN_TIMEOUT, env=None):
        p = subprocess.Popen(
            [sys.executable, "-m", "src", *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, env=env or cls.env, cwd=cwd, **dockenv.SIGINT_DEFAULT,
        )
        cls.pids.append(p.pid)
        try:
            out, err = p.communicate(stdin, timeout=timeout)
        except subprocess.TimeoutExpired:
            p.kill()
            p.communicate()
            raise
        return subprocess.CompletedProcess(p.args, p.returncode, out, err)

    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="dock-work-", dir=self.tmp))  # the user's cwd

    def dock(self, *args, env=None, stdin=""):
        return self.run_dock(*args, cwd=self.work, env=env, stdin=stdin)

    def jail(self, plugin_command: str, *args: str, plugin="jail", env=None) -> str:
        """Run a command of the jail fixture; its one line of output."""
        r = self.dock(f"dev.dock.{plugin}:{plugin_command}", *args, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    # AC9 / R10, F2: the host PATH points at host programs; the image's own PATH is the right one inside
    def test_the_plugin_runs_inside_the_container_with_its_own_path(self):
        with self.subTest("the plugin really runs inside the container"):
            # A native run would see the user's own HOME; the container sets HOME=/tmp.
            self.assertEqual(self.jail("env", "HOME"), "set:/tmp")
            self.assertEqual(self.jail("env", "DOCK_PLUGIN_ID"), "set:dev.dock.jail")
        with self.subTest("the host path is not the container's path"):
            host_path = f"/dock-test-host-only/bin{os.pathsep}{os.environ['PATH']}"
            inside = self.jail("env", "PATH", env={**self.env, "PATH": host_path})
            self.assertTrue(inside.startswith("set:"), inside)
            self.assertNotIn("dock-test-host-only", inside)
            self.assertNotEqual(inside, f"set:{host_path}")
            self.assertIn("/usr/local/bin", inside.removeprefix("set:").split(":"))  # python:3.15.2-slim's PATH
        with self.subTest("the plugin works when the host path lacks /usr/local/bin"):
            bindir = self.tmp / "only-docker"
            bindir.mkdir(exist_ok=True)
            if not (bindir / "docker").exists():
                (bindir / "docker").symlink_to(dockenv.require_runtime())
            host_path = f"{bindir}{os.pathsep}/usr/bin{os.pathsep}/bin"
            self.assertNotIn("/usr/local/bin", host_path.split(os.pathsep))
            # python3 was found on the image's PATH
            self.assertEqual(self.jail("env", "DOCK_PLUGIN_ID", env={**self.env, "PATH": host_path}), "set:dev.dock.jail")

    # NFR1: network
    def test_plugin_without_network_cannot_connect_out(self):
        self.assertEqual(self.jail("net"), "blocked")

    @unittest.skipUnless(NETWORK_CONTROL, "set DOCK_TEST_NETWORK=1 to run the positive control (needs the internet)")
    def test_the_same_plugin_with_network_permission_can_connect_out(self):
        # Without this, "blocked" above could just mean the host has no route to 1.1.1.1.
        self.assertEqual(self.jail("net", plugin="jailnet"), "open")

    # NFR1: cwd
    def test_a_cwd_file_is_readable_only_with_fs_write_cwd(self):
        (self.work / "secret.txt").write_text("top secret")
        with self.subTest("without fs:write:cwd"):
            self.assertEqual(self.jail("readcwd", "secret.txt"), "unreadable")
        with self.subTest("with fs:write:cwd"):
            self.assertEqual(self.jail("readcwd", "secret.txt", plugin="jailcwd"), "top secret")

    # NFR1: the in folder is readable, never writable
    def test_the_in_folder_is_read_only_for_a_plugin_with_fs_read_in(self):
        inbox = Path(tempfile.mkdtemp(prefix="dock-in-", dir=self.tmp))
        (inbox / "data.txt").write_text("input data")
        with self.subTest("without fs:read:in the folder is not there"):
            r = self.dock("--in", str(inbox), "dev.dock.jail:readin", "data.txt")
            self.assertEqual((r.returncode, r.stdout.strip()), (0, "unreadable"), r.stderr)
            self.assertIn("--in ignored", r.stderr)
        with self.subTest("with fs:read:in it can be read"):
            r = self.dock("--in", str(inbox), "dev.dock.jailin:readin", "data.txt")
            self.assertEqual((r.returncode, r.stdout.strip()), (0, "input data"), r.stderr)
        with self.subTest("but not overwritten, extended or emptied"):
            r = self.dock("--in", str(inbox), "dev.dock.jailin:writein", "data.txt")
            self.assertEqual((r.returncode, r.stdout.strip()), (0, "denied"), r.stderr)
            self.assertEqual((inbox / "data.txt").read_text(), "input data")
            self.assertEqual(sorted(p.name for p in inbox.iterdir()), ["data.txt"])

    def test_nested_in_and_out_folders_keep_their_own_mode(self):
        outer = Path(tempfile.mkdtemp(prefix="dock-nest-", dir=self.tmp))
        inner = outer / "inner"
        inner.mkdir()
        (outer / "data.txt").write_text("outer data")
        (inner / "data.txt").write_text("inner data")
        with self.subTest("in inside out: in is read-only, the rest of out is writable"):
            r = self.dock("--out", str(outer), "--in", str(inner), "dev.dock.jailboth:writein", "data.txt")
            self.assertEqual((r.returncode, r.stdout.strip()), (0, "denied"), r.stderr)
            self.assertEqual(sorted(p.name for p in inner.iterdir()), ["data.txt"])
            r = self.dock("--out", str(outer), "--in", str(inner), "dev.dock.jailboth:writecwd", "w.txt")
            self.assertEqual((r.returncode, r.stdout.strip()), (0, "written"), r.stderr)
            self.assertTrue((outer / "w.txt").exists())
        with self.subTest("out inside in: out is writable, the rest of in is read-only"):
            r = self.dock("--out", str(inner), "--in", str(outer), "dev.dock.jailboth:writecwd", "w2.txt")
            self.assertEqual((r.returncode, r.stdout.strip()), (0, "written"), r.stderr)
            self.assertTrue((inner / "w2.txt").exists())
            r = self.dock("--out", str(inner), "--in", str(outer), "dev.dock.jailboth:writein", "data.txt")
            self.assertEqual((r.returncode, r.stdout.strip()), (0, "denied"), r.stderr)
            self.assertEqual((outer / "data.txt").read_text(), "outer data")
            self.assertFalse((outer / "new.txt").exists())

    # NFR1: environment
    def test_host_variable_outside_the_allowlist_is_not_visible(self):
        self.assertEqual(self.jail("env", "DOCK_TEST_HOST_SECRET"), "unset")

    # AC10 / R7: a declared variable gets in by name; its undeclared neighbour does not
    def test_declared_and_allowlisted_host_variables(self):
        with self.subTest("a declared host variable is visible"):
            self.assertEqual(self.jail("env", "DOCK_TEST_DECLARED"), "set:test-model")
        with self.subTest("a declared host variable that is unset is not visible"):
            env = {k: v for k, v in self.env.items() if k != "DOCK_TEST_DECLARED"}
            self.assertEqual(self.jail("env", "DOCK_TEST_DECLARED", env=env), "unset")
        with self.subTest("a declared host variable set to empty is passed empty"):
            self.assertEqual(self.jail("env", "DOCK_TEST_DECLARED", env={**self.env, "DOCK_TEST_DECLARED": ""}), "set:")
        with self.subTest("an allowlisted host variable is visible"):
            self.assertEqual(self.jail("env", "TZ", env={**self.env, "TZ": "Europe/Oslo"}), "set:Europe/Oslo")

    # NFR4
    def test_no_container_outlives_the_run(self):
        self.jail("net")
        self.jail("env", "TZ")
        # Only this run's dock processes: other dock containers on the machine are not ours to judge.
        mine = [
            dockenv.run_runtime("ps", "-a", "--filter", f"label=dev.dock.owner={socket.gethostname()}:{pid}",
                                "--format", "{{.Names}}")
            for pid in self.pids
        ]
        for left in mine:
            self.assertEqual(left.returncode, 0, left.stderr)
        self.assertEqual([n for left in mine for n in left.stdout.split()], [])


if __name__ == "__main__":
    unittest.main()
