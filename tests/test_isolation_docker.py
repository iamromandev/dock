"""Container isolation against a real Docker daemon (skipped where there is none).

The fake-runtime tests in test_isolation.py prove dock's wiring; these prove that the flags it
passes really confine a plugin (no network, no cwd, no host environment) and that the bundled
plugins work in their images. They run on the Ubuntu CI runner; macOS runners have no Docker.
"""

import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cli.store import Store  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
BUILD_TIMEOUT = 1200  # the first build pulls base images
RUN_TIMEOUT = 300
# The positive control for the no-network test needs the internet; opt in with DOCK_TEST_NETWORK=1.
NETWORK_CONTROL = os.environ.get("DOCK_TEST_NETWORK") == "1"


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=60, check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def docker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=RUN_TIMEOUT, check=False)


@unittest.skipUnless(docker_available(), "docker is not available")
class RealDocker(unittest.TestCase):
    """dock with `[isolation] enabled = true` and the real `docker`, in a throwaway DOCK_HOME."""

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
        cls.tmp = Path(tempfile.mkdtemp(prefix="dock-docker-")).resolve()
        cls.addClassCleanup(shutil.rmtree, cls.tmp, ignore_errors=True)
        cls.store = Store(cls.tmp / "home")
        # A thin wrapper around the real docker that records each call, to see what dock asked it to run.
        cls.calls = cls.tmp / "docker-calls"
        wrapper = cls.tmp / "docker-recorder"
        wrapper.write_text(
            f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {shlex.quote(str(cls.calls))}\n"
            f"exec {shlex.quote(shutil.which('docker'))} \"$@\"\n"
        )
        wrapper.chmod(0o755)
        (cls.store.home / "config.toml").write_text(f'[isolation]\nenabled = true\nruntime = "{wrapper}"\n')
        cls.pids = []  # every dock process of this run: their containers are the only ones we judge

        cls.env = {
            **os.environ, "DOCK_HOME": str(cls.store.home), "PYTHONPATH": str(ROOT),
            "DOCK_TEST_HOST_SECRET": cls.HOST_SECRET, "DOCK_LLM_MODEL": "test-model",
        }
        # A developer's real model server must not leak in: the isolated llm would try it and fail.
        for name in ("DOCK_LLM_BASE_URL", "DOCK_LLM_API_KEY"):
            cls.env.pop(name, None)
        plugins = [ROOT / "plugin" / "hello", FIXTURES / "jail", cls.jail_copy("jailcwd", ["fs:write:cwd"]),
                   ROOT / "plugin" / "download", ROOT / "plugin" / "llm", ROOT / "plugin" / "chat"]
        if NETWORK_CONTROL:
            plugins.append(cls.jail_copy("jailnet", ["network"]))
        for path in plugins:
            m = cls.store.install(path)
            cls.addClassCleanup(docker, "image", "rm", "-f", f"dock/{m.id}:{m.version}")
            # Also covers `dock plugin build` for real: the images exist before any test runs.
            built = cls.run_dock("plugin", "build", m.id, cwd=ROOT, timeout=BUILD_TIMEOUT)
            if built.returncode != 0:
                raise AssertionError(f"plugin build {m.id} failed: {built.stderr}\n{built.stdout}")

    @classmethod
    def run_dock(cls, *args, cwd, stdin="", timeout=RUN_TIMEOUT, env=None):
        p = subprocess.Popen(
            [sys.executable, "-m", "cli", *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, env=env or cls.env, cwd=cwd,
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

    # AC9 / R10
    def test_isolated_hello_greets(self):
        r = self.dock("hello", "Roman")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "hello, Roman!")

    def test_the_plugin_really_runs_inside_the_container(self):
        # A native run would see the user's own HOME; the container sets HOME=/tmp.
        self.assertEqual(self.jail("env", "HOME"), "set:/tmp")
        self.assertEqual(self.jail("env", "DOCK_PLUGIN_ID"), "set:dev.dock.jail")

    # NFR1: network
    def test_plugin_without_network_cannot_connect_out(self):
        self.assertEqual(self.jail("net"), "blocked")

    @unittest.skipUnless(NETWORK_CONTROL, "set DOCK_TEST_NETWORK=1 to run the positive control (needs the internet)")
    def test_the_same_plugin_with_network_permission_can_connect_out(self):
        # Without this, "blocked" above could just mean the host has no route to 1.1.1.1.
        self.assertEqual(self.jail("net", plugin="jailnet"), "open")

    # NFR1: cwd
    def test_plugin_without_fs_write_cwd_cannot_read_a_cwd_file(self):
        (self.work / "secret.txt").write_text("top secret")
        self.assertEqual(self.jail("readcwd", "secret.txt"), "unreadable")

    def test_plugin_with_fs_write_cwd_can_read_it(self):
        (self.work / "secret.txt").write_text("top secret")
        self.assertEqual(self.jail("readcwd", "secret.txt", plugin="jailcwd"), "top secret")

    # NFR1: environment
    def test_host_variable_outside_the_allowlist_is_not_visible(self):
        self.assertEqual(self.jail("env", "DOCK_TEST_HOST_SECRET"), "unset")

    # AC10 / R7: a declared variable gets in by name; its undeclared neighbour does not
    def test_declared_host_variable_is_visible(self):
        self.assertEqual(self.jail("env", "DOCK_LLM_MODEL"), "set:test-model")

    def test_declared_host_variable_that_is_unset_is_not_visible(self):
        env = {k: v for k, v in self.env.items() if k != "DOCK_LLM_MODEL"}
        self.assertEqual(self.jail("env", "DOCK_LLM_MODEL", env=env), "unset")

    def test_declared_host_variable_set_to_empty_is_passed_empty(self):
        self.assertEqual(self.jail("env", "DOCK_LLM_MODEL", env={**self.env, "DOCK_LLM_MODEL": ""}), "set:")

    def test_allowlisted_host_variable_is_visible(self):
        self.assertEqual(self.jail("env", "TZ", env={**self.env, "TZ": "Europe/Oslo"}), "set:Europe/Oslo")

    # F2: the host PATH points at host programs; the image's own PATH is the right one inside
    def test_the_host_path_is_not_the_containers_path(self):
        host_path = f"/dock-test-host-only/bin{os.pathsep}{os.environ['PATH']}"
        inside = self.jail("env", "PATH", env={**self.env, "PATH": host_path})
        self.assertTrue(inside.startswith("set:"), inside)
        self.assertNotIn("dock-test-host-only", inside)
        self.assertNotEqual(inside, f"set:{host_path}")
        self.assertIn("/usr/local/bin", inside.removeprefix("set:").split(":"))  # python:3.12-slim's PATH

    def test_hello_works_when_the_host_path_lacks_usr_local_bin(self):
        bindir = self.tmp / "only-docker"
        bindir.mkdir(exist_ok=True)
        if not (bindir / "docker").exists():
            (bindir / "docker").symlink_to(shutil.which("docker"))
        host_path = f"{bindir}{os.pathsep}/usr/bin{os.pathsep}/bin"
        self.assertNotIn("/usr/local/bin", host_path.split(os.pathsep))
        r = self.dock("hello", "Roman", env={**self.env, "PATH": host_path})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "hello, Roman!")  # python3 was found on the image's PATH

    # NFR4
    def test_no_container_outlives_the_run(self):
        self.jail("net")
        self.dock("hello", "x")
        # Only this run's dock processes: other dock containers on the machine are not ours to judge.
        mine = [
            docker("ps", "-a", "--filter", f"label=dev.dock.owner={socket.gethostname()}:{pid}",
                   "--format", "{{.Names}}")
            for pid in self.pids
        ]
        for left in mine:
            self.assertEqual(left.returncode, 0, left.stderr)
        self.assertEqual([n for left in mine for n in left.stdout.split()], [])

    # AC9 / R10, R11
    def test_isolated_chat_gets_a_reply_from_the_isolated_toy_model_and_leaves_no_container(self):
        self.calls.write_text("")
        r = self.dock("chat", stdin="hi\n/exit\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("toy: toy-1", r.stdout)
        # the banner comes from llm.info; the reply proves llm.generate worked across both containers
        self.assertIn("Hello! I'm a toy model served by the dock llm plugin.", r.stdout)
        self.assertNotIn("error", r.stderr.lower())
        runs = [l for l in self.calls.read_text().splitlines() if l.startswith("run ")]
        for image in ("dock/dev.dock.chat:", "dock/dev.dock.llm:"):
            self.assertEqual(len([l for l in runs if f" {image}" in l]), 1, f"{image} not run once: {runs}")
        left = docker("ps", "-a", "--filter", f"label=dev.dock.owner={socket.gethostname()}:{self.pids[-1]}",
                      "--format", "{{.Names}}")
        self.assertEqual(left.stdout.split(), [])

    def test_isolated_chat_with_an_argument_is_refused_by_dock(self):
        r = self.dock("chat", "extra")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(r.stderr.strip(), "dock: chat: unexpected argument 'extra'")

    # AC13 / R14
    def test_isolated_download_of_a_file_url_writes_identical_bytes_in_cwd(self):
        payload = os.urandom(300_000)  # binary, several read chunks
        source = self.work / "src.bin"  # inside the cwd: the only user path the container can see
        source.write_bytes(payload)
        r = self.dock("download", source.as_uri(), "-o", "copy.bin")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.work / "copy.bin").read_bytes(), payload)

    def test_isolated_download_reads_its_typed_inputs_for_the_long_flag_and_the_default_name(self):
        payload = os.urandom(1000)
        (self.work / "sub").mkdir()
        source = self.work / "sub" / "src.bin"
        source.write_bytes(payload)
        r = self.dock("download", source.as_uri(), "--output=copy2.bin")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.work / "copy2.bin").read_bytes(), payload)
        r = self.dock("download", source.as_uri())  # no output: named after the last path segment, in the cwd
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.work / "src.bin").read_bytes(), payload)

    def test_isolated_download_without_a_url_is_refused_by_dock_before_any_container_starts(self):
        r = self.dock("download")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(r.stderr.strip(), "dock: download: missing required input 'url'")


if __name__ == "__main__":
    unittest.main()
