"""The WASM runner against a real `wasmtime` and the real pinned Python interpreter (skipped without wasmtime).

The fake-wasmtime tests in test_wasm.py prove dock's wiring; these prove that the flags it passes really
confine a plugin (no network, no cwd, no host files, no host environment, read-only plugin and interpreter)
and that a plain Python plugin works on the interpreter. They run on the Ubuntu and macOS CI runners.

  DOCK_TEST_WASMTIME   path of the wasmtime binary to use (default: `wasmtime` on PATH)
  DOCK_TEST_WASM_CACHE a folder that holds the pinned interpreter zip, so a run does not download 15.5 MB
                       again. A zip missing there is fetched once and stored; the pinned SHA-256 is checked
                       either way, before the zip is used.
"""

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cli import wasm  # noqa: E402
from cli.store import Store  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
RUN_TIMEOUT = 120
FETCH_TIMEOUT = 600  # the first run may download the interpreter


def wasmtime_path() -> str | None:
    override = os.environ.get("DOCK_TEST_WASMTIME")
    if override:
        return override if os.access(override, os.X_OK) else None
    return shutil.which("wasmtime")


WASMTIME = wasmtime_path()
PIN = wasm.PYTHONS["3.15"]


def cached_zip(cache: Path) -> Path:
    """The pinned interpreter archive in `cache`, fetched there if missing; refuses one with the wrong hash."""
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / PIN.url.rsplit("/", 1)[1]
    if not path.exists():
        part = path.with_name(path.name + f".part-{os.getpid()}")
        try:
            with urllib.request.urlopen(PIN.url, timeout=60) as r, open(part, "wb") as f:
                shutil.copyfileobj(r, f)
            os.replace(part, path)
        finally:
            part.unlink(missing_ok=True)
    got = hashlib.sha256(path.read_bytes()).hexdigest()
    if got != PIN.sha256:
        raise AssertionError(f"{path} has SHA-256 {got}, not the pinned {PIN.sha256}")
    return path


def remove(path: Path) -> None:
    for root, dirs, files in os.walk(path):  # sealed folders cannot be deleted before they are made writable
        os.chmod(root, 0o755)
        for name in dirs:
            if not os.path.islink(os.path.join(root, name)):
                os.chmod(os.path.join(root, name), 0o755)
    shutil.rmtree(path, ignore_errors=True)


@unittest.skipUnless(WASMTIME, "wasmtime is not available (put it on PATH or set DOCK_TEST_WASMTIME)")
class RealWasmtime(unittest.TestCase):
    """dock with the real `wasmtime`, a throwaway DOCK_HOME and a throwaway HOME."""

    # Unique per run, so the process-list check cannot trip over some other command line that mentions them.
    HOST_SECRET = f"host-secret-{uuid.uuid4().hex}"
    DECLARED_VALUE = f"declared-{uuid.uuid4().hex}"

    @classmethod
    def plugin_copy(cls, name: str, permissions: list[str]) -> Path:
        """The wasmpy fixture again under another id and with permissions: a control for a restriction."""
        path = cls.tmp / name
        shutil.copytree(FIXTURES / "wasmpy", path, ignore=shutil.ignore_patterns("__pycache__"))
        manifest = (path / "plugin.toml").read_text().replace("dev.dock.wasmpy", f"dev.dock.{name}")
        perms = ", ".join(f'"{p}"' for p in permissions)
        # The fixture already has a [capabilities] table (for env): add to it, never open a second one.
        (path / "plugin.toml").write_text(manifest.replace("[capabilities]\n", f"[capabilities]\npermissions = [{perms}]\n"))
        return path

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="dock-wasm-real-")).resolve()
        cls.addClassCleanup(remove, cls.tmp)
        cls.store = Store(cls.tmp / "home")
        (cls.store.home / "config.toml").write_text(f'[wasm]\nruntime = "{WASMTIME}"\n')
        cls.host_home = cls.tmp / "host-home"
        cls.host_home.mkdir()
        (cls.host_home / "secret.txt").write_text("top secret")
        cls.env = {
            **os.environ, "DOCK_HOME": str(cls.store.home), "PYTHONPATH": str(ROOT), "HOME": str(cls.host_home),
            "DOCK_TEST_HOST_SECRET": cls.HOST_SECRET, "DOCK_WASMPY_DECLARED": cls.DECLARED_VALUE,
        }
        for name in ("DOCK_TEST_WASM_PYTHON_URL", "DOCK_TEST_WASM_PYTHON_SHA256"):
            cls.env.pop(name, None)
        cache = os.environ.get("DOCK_TEST_WASM_CACHE")
        if cache:
            # Served through the test override, which only works with the pinned hash given beside the URL.
            cls.env["DOCK_TEST_WASM_PYTHON_URL"] = cached_zip(Path(cache)).as_uri()
            cls.env["DOCK_TEST_WASM_PYTHON_SHA256"] = PIN.sha256
        for path in (FIXTURES / "wasmpy", cls.plugin_copy("wasmpycwd", ["fs:write:cwd"])):
            cls.store.install(path)
        cls.work = cls.tmp / "work"
        cls.work.mkdir()
        # The first start fetches and verifies the interpreter; every test after it finds it in place.
        first = cls.run_dock("dev.dock.wasmpy:greet", "setup", timeout=FETCH_TIMEOUT)
        if first.returncode != 0:
            raise AssertionError(f"first start failed: {first.stderr}\n{first.stdout}")

    @classmethod
    def run_dock(cls, *args, stdin="", timeout=RUN_TIMEOUT, env=None, cwd=None):
        return subprocess.run(
            [sys.executable, "-m", "cli", *args], input=stdin, capture_output=True, text=True,
            env=env or cls.env, cwd=cwd or cls.work, timeout=timeout, check=False,
        )

    def command(self, command: str, *args: str, plugin="wasmpy", env=None, cwd=None) -> str:
        """Run a command of a wasmpy fixture; its output, which must be one line."""
        r = self.run_dock(f"dev.dock.{plugin}:{command}", *args, env=env, cwd=cwd)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def python_root(self) -> Path:
        return wasm.python_home(self.store, "3.15")

    def test_a_plain_python_plugin_greets_on_the_pinned_interpreter(self):
        self.assertEqual(self.command("greet", "Roman"), "hello, Roman!")
        self.assertTrue((self.python_root() / ".verified").is_file())

    def test_the_interpreter_is_installed_sealed(self):
        root = self.python_root()
        self.assertFalse(os.access(root, os.W_OK))
        self.assertFalse(os.access(root / "python.wasm", os.W_OK))

    def test_the_warm_plugin_in_the_shell_keeps_its_state_between_commands(self):
        r = self.run_dock("shell", stdin="dev.dock.wasmpy:count\ndev.dock.wasmpy:count\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = [l.replace("dock> ", "").strip() for l in r.stdout.splitlines()]
        self.assertEqual([l for l in lines if l in ("1", "2")], ["1", "2"])

    def test_the_plugin_writes_into_its_data_dir(self):
        self.assertEqual(self.command("writedata", "kept"), "kept")
        self.assertEqual((self.store.data_dir("dev.dock.wasmpy") / "note.txt").read_text(), "kept")

    # NFR1: cwd
    def test_without_fs_write_cwd_the_plugin_cannot_read_a_cwd_file(self):
        (self.work / "secret.txt").write_text("top secret")
        self.assertEqual(self.command("readcwd", "secret.txt"), "unreadable")

    def test_without_fs_write_cwd_the_plugin_cannot_create_a_cwd_file(self):
        self.assertEqual(self.command("writecwd", "denied.txt"), "denied")
        self.assertFalse((self.work / "denied.txt").exists())

    def test_with_fs_write_cwd_the_plugin_reads_a_cwd_file(self):
        (self.work / "readable.txt").write_text("top secret")
        self.assertEqual(self.command("readcwd", "readable.txt", plugin="wasmpycwd"), "top secret")

    def test_with_fs_write_cwd_the_plugin_creates_a_cwd_file(self):
        self.assertEqual(self.command("writecwd", "created.txt", plugin="wasmpycwd"), "written")
        self.assertEqual((self.work / "created.txt").read_text(), "from the plugin")

    # NFR1: the rest of the host
    def test_a_file_in_the_host_home_is_not_readable(self):
        self.assertEqual((self.host_home / "secret.txt").read_text(), "top secret")  # it is there, and readable by us
        self.assertEqual(self.command("readhome", str(self.host_home / "secret.txt")), "unreadable")
        self.assertEqual(self.command("readhome", "/etc/hosts"), "unreadable")

    def test_the_plugin_has_no_network(self):
        self.assertEqual(self.command("net"), "blocked")

    # NFR1: environment
    def test_an_undeclared_host_variable_is_not_visible(self):
        self.assertEqual(self.command("env", "DOCK_TEST_HOST_SECRET"), "unset")

    def test_a_declared_host_variable_is_visible(self):
        self.assertEqual(self.command("env", "DOCK_WASMPY_DECLARED"), f"set:{self.DECLARED_VALUE}")

    def test_a_declared_host_variable_that_is_unset_stays_unset(self):
        env = {k: v for k, v in self.env.items() if k != "DOCK_WASMPY_DECLARED"}
        self.assertEqual(self.command("env", "DOCK_WASMPY_DECLARED", env=env), "unset")

    def test_the_host_variables_dock_does_not_hand_over_stay_out_but_its_own_are_set(self):
        self.assertEqual(self.command("env", "DOCK_PLUGIN_ID"), "set:dev.dock.wasmpy")
        data = self.store.data_dir("dev.dock.wasmpy")
        self.assertEqual(self.command("env", "HOME"), f"set:{data}/.home")

    def test_no_variable_value_is_in_the_process_list_while_the_plugin_runs(self):
        p = subprocess.Popen([sys.executable, "-m", "cli", "shell"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, env=self.env, cwd=self.work)
        self.addCleanup(p.kill)
        p.stdin.write("dev.dock.wasmpy:count\n")
        p.stdin.flush()
        self.assertIn("1", p.stdout.readline())  # the warm plugin is up and stays up while stdin is open
        ps = subprocess.run(["ps", "-axo", "args"], capture_output=True, text=True, check=False).stdout
        p.stdin.close()
        p.wait(timeout=RUN_TIMEOUT)
        p.stdout.close()
        p.stderr.close()
        self.assertIn("wasmtime", ps)  # the check looked at the right thing
        self.assertNotIn(self.HOST_SECRET, ps)
        self.assertNotIn(self.DECLARED_VALUE, ps)

    # R6: read-only plugin folder and interpreter
    def test_the_plugin_cannot_write_into_its_own_folder(self):
        self.assertEqual(self.command("writeplugin"), "denied")
        self.assertFalse((self.store.get("dev.dock.wasmpy").root / "injected.py").exists())

    def test_the_plugin_cannot_write_into_the_interpreter_folder(self):
        self.assertEqual(self.command("writepython"), "denied")
        self.assertFalse((self.python_root() / "lib" / "injected.py").exists())


if __name__ == "__main__":
    unittest.main()
