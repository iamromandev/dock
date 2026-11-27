"""Container isolation: manifest entry, config, argv, and the host wiring, against the real container runtime."""

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import dockenv
from dock_host import isolation
from dock_host.manifest import Manifest, ManifestError
from dock_host.store import Store

BASE = """\
id = "dev.dock.sample"
version = "1.2.3"
api_version = "2"
description = "sample"

[run]
{run}

[[commands]]
name = "go"
"""


def temp_dir(prefix: str) -> Path:
    """A temporary directory that is removed when this module's tests are done."""
    path = Path(tempfile.mkdtemp(prefix=prefix))
    unittest.addModuleCleanup(shutil.rmtree, path, True)
    return path


def make_plugin(run: str, extra: str = "") -> Path:
    root = temp_dir("dock-iso-plugin-")
    (root / "plugin.toml").write_text(BASE.format(run=run) + extra)
    return root


class ManifestContainer(unittest.TestCase):
    def test_container_plugin_id_and_version_must_fit_an_image_tag(self):
        for field, value in (
            ("id", ".hidden"),
            ("id", "a/b.c"),
            ("id", "../x"),
            ("id", "has space.x"),
            ("id", "a:b.c"),
            ("id", "-x.y"),
            ("version", ".1"),
            ("version", "1.0+build"),
            ("version", "1 0"),
            ("version", "a/b"),
            ("version", "-1"),
        ):
            with self.subTest(field=field, value=value):
                root = make_plugin('container = { cmd = "python3" }')
                text = (root / "plugin.toml").read_text()
                old = 'id = "dev.dock.sample"' if field == "id" else 'version = "1.2.3"'
                (root / "plugin.toml").write_text(text.replace(old, f'{field} = "{value}"'))
                # An id is refused by the general check (it names store folders); a version, for the image tag.
                pattern = (
                    r"id '.*' may only hold" if field == "id" else r"container.*version|version.*container"
                )
                with self.assertRaisesRegex(ManifestError, pattern):
                    Manifest.load(root)
        # dots, dashes and underscores are fine
        root = make_plugin('container = { cmd = "python3" }')
        text = (
            (root / "plugin.toml")
            .read_text()
            .replace("dev.dock.sample", "Dev_1.dock-x")
            .replace("1.2.3", "1.2.3-rc_1")
        )
        (root / "plugin.toml").write_text(text)
        self.assertEqual(Manifest.load(root).id, "Dev_1.dock-x")


class ContainerConfig(unittest.TestCase):
    """`[container] runtime` in config.toml, and what happens to a config that is not what dock can honour."""

    def store(self, config: str | bytes | None) -> Store:
        home = temp_dir("dock-iso-home-")
        if isinstance(config, str):
            (home / "config.toml").write_text(config)
        elif config is not None:
            (home / "config.toml").write_bytes(config)
        return Store(home)

    # AC7: a typo must never quietly mean "the default runtime"
    def test_unknown_keys_are_refused_naming_them(self):
        with self.subTest("an unknown key is named with the known one"):
            with self.assertRaises(ManifestError) as cm:
                isolation.config(self.store('[container]\nruntimes = "x"\n'))
            self.assertIn("unknown key 'runtimes'", str(cm.exception))
            self.assertIn("(known: runtime)", str(cm.exception))
        with self.subTest("every unknown key is named"):
            with self.assertRaises(ManifestError) as cm:
                isolation.config(self.store("[container]\nenabled = true\nplugins = []\nruntime = 'x'\n"))
            self.assertIn("'enabled'", str(cm.exception))
            self.assertIn("'plugins'", str(cm.exception))
            self.assertNotIn("'runtime'", str(cm.exception))

    # AC11: a table of a removed feature stops every command that starts a plugin

    # a config that does not parse is fatal: nothing says which part would have been about the runtime


def install(store: Store, run: str, extra: str = "", dockerfile: str | bool = True) -> Manifest:
    """Install a throwaway plugin. `dockerfile`: True for an empty `FROM scratch` image, or the Dockerfile's text."""
    root = make_plugin(run, extra)
    if dockerfile:
        (root / "Dockerfile").write_text("FROM scratch\n" if dockerfile is True else dockerfile)
    return store.install(root)


CONTAINER = 'container = { cmd = "python3", args = ["/plugin/main.py"] }'
SAMPLE_IMAGE = "dock/dev.dock.sample:1.2.3"


class RecordingRuntimeCase(unittest.TestCase):
    """A throwaway DOCK_HOME whose runtime is a recording wrapper around the real one, so a test reads exactly what
    dock asked it to do. Images of the throwaway plugins (never the shared fixture ones) are removed afterwards."""

    @classmethod
    def setUpClass(cls):
        dockenv.require_runtime()

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dock-iso-case-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.wrapper, self.log = dockenv.recording_runtime(self.tmp)
        self.store = Store(self.tmp / "home")
        patcher = mock.patch.dict(os.environ, {"SECRET_TOKEN": "s3cret"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.own_image(SAMPLE_IMAGE)

    def own_image(self, tag: str) -> None:
        """An image only this test makes: gone before it starts and after it ends."""
        dockenv.remove_image(tag)
        self.addCleanup(dockenv.remove_image, tag)

    def configure(self, runtime: str | Path | None = None) -> None:
        dockenv.write_config(self.store.home, runtime or self.wrapper)

    def reset_calls(self) -> None:
        """Forget the recorded runtime calls: the start of the next case of a table-driven test."""
        self.log.unlink(missing_ok=True)

    def calls(self, verb: str | None = None) -> list[list[str]]:
        return [c for c in dockenv.calls_in(self.log) if verb is None or c[0] == verb]


class PopenIsOnlyEverTheRuntime(RecordingRuntimeCase):
    """AC19 / NFR1: the one `Popen` in src/process.py is only ever given a `<runtime> run` command line."""

    def test_every_plugin_argv_given_to_popen_is_the_runtime_then_run(self):
        from dock_host import process
        from dock_host.lifecycle import Session

        dockenv.prebuild_plugins(ROOT / "tests" / "fixtures" / "probe")
        self.store.install(ROOT / "tests" / "fixtures" / "probe")
        self.configure()
        seen = []
        real_popen = subprocess.Popen

        def spy(argv, *args, **kw):
            seen.append(list(argv))
            return real_popen(argv, *args, **kw)

        class Proxy:  # dock_host.process's view of `subprocess`, with only Popen watched
            Popen = staticmethod(spy)

            def __getattr__(self, name):
                return getattr(subprocess, name)

        events = []

        class UI:
            def output(self, p):
                events.append(p.get("text"))

            log = bar = table = panel = clear = lambda self, p: None

            def begin(self):
                pass

            end = begin

            def form(self, p):
                return {"eof": True}

            def prompt_user(self, p):
                return {"eof": True}

            def warn(self, message):
                pass

            def handlers(self):
                return {
                    "output": self.output,
                    "log": self.log,
                    "bar": self.bar,
                    "table": self.table,
                    "panel": self.panel,
                    "clear": self.clear,
                    "prompt_user": self.prompt_user,
                    "form": self.form,
                }

        with mock.patch.object(process, "subprocess", Proxy()):
            session = Session(self.store, lambda _id: UI())
            try:
                self.assertEqual(session.invoke(self.store.get("dev.dock.probe"), "echoargs", ["Ada"]), 0)
                session.invoke(self.store.get("dev.dock.probe"), "code", ["0"])  # a second call: same process
            finally:
                session.close()
        self.assertIn('"args": ["Ada"]', events[0])
        self.assertEqual(len(seen), 1)  # one plugin, one start
        self.assertEqual(seen[0][:2], [str(self.wrapper), "run"])


class CwdMount(RecordingRuntimeCase):
    """F4: with fs:write:cwd the cwd is mounted read-write, so a cwd that is too broad is refused."""

    def setUp(self):
        super().setUp()
        self.user_home = self.tmp / "user"
        (self.user_home / "project").mkdir(parents=True)
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.user_home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.configure()
        self.m = install(self.store, CONTAINER, '\n[capabilities]\npermissions = ["fs:write:cwd"]')

    def prepare(self, cwd, manifest=None):
        with redirect_stderr(StringIO()):
            return isolation.prepare(self.store, manifest or self.m, cwd=str(cwd))

    def assertRefused(self, cwd, reason):
        with self.assertRaises(ManifestError) as cm:
            self.prepare(cwd)
        self.assertIn(reason, str(cm.exception))
        self.assertIn("dev.dock.sample", str(cm.exception))
        self.assertEqual(self.calls("run"), [])

    def reader(self, *perms):
        extra = "\n[capabilities]\npermissions = [" + ", ".join(f'"{x}"' for x in perms) + "]"
        m = Manifest.load(make_plugin(CONTAINER, extra))
        (m.root / "Dockerfile").write_text("FROM scratch\n")
        return m

    def with_in(self, path):
        from types import SimpleNamespace

        self.store.dirs = SimpleNamespace(in_dir=Path(path), in_source="--in")

    def test_the_in_folder_is_mounted_read_only_defaulting_to_the_cwd(self):
        project = (self.user_home / "project").resolve()
        other = self.user_home / "other"
        other.mkdir()
        m = self.reader("fs:read:in")
        self.assertIn(f"type=bind,src={project},dst={project},readonly", self.prepare(project, m).argv)
        self.with_in(other)
        argv = self.prepare(project, m).argv
        self.assertIn(f"type=bind,src={other.resolve()},dst={other.resolve()},readonly", argv)
        self.assertNotIn(f"type=bind,src={project},dst={project}", argv)
        both = self.prepare(project, self.reader("fs:write:cwd", "fs:read:in"))
        self.assertIn(f"type=bind,src={project},dst={project}", both.argv)
        self.assertIn(f"type=bind,src={other.resolve()},dst={other.resolve()},readonly", both.argv)

    def test_too_broad_a_cwd_is_refused(self):
        cases = (
            ("the filesystem root", lambda: "/", "filesystem root"),
            ("the home directory", lambda: self.user_home, "home directory"),
            ("a directory containing the dock home", lambda: self.store.home.parent, "dock home"),
            ("the dock home itself", lambda: self.store.home, "dock home"),
            ("inside the dock home", lambda: self.store.home / "data" / "dev.dock.sample", "dock home"),
        )
        for label, cwd, reason in cases:
            with self.subTest(label):
                self.assertRefused(cwd(), reason)
        with self.subTest("a directory above the home directory"):
            deep = self.tmp / "a" / "b" / "user"
            (deep / "project").mkdir(parents=True)
            with mock.patch.dict(os.environ, {"HOME": str(deep)}):
                self.assertRefused(deep.parent, "above your home directory")
                self.assertRefused(deep.parent.parent, "above your home directory")
                launch = self.prepare(deep / "project")  # a project inside the home is still fine
            self.assertTrue(any(str(deep / "project") in a for a in launch.argv), launch.argv)


FIXTURES = ROOT / "tests" / "fixtures"


class DockIsolated(RecordingRuntimeCase):
    """Fixture plugins built once in the real runtime, with a recording wrapper around it."""

    PLUGINS = (FIXTURES / "uiprobe", FIXTURES / "probe")

    @classmethod
    def setUpClass(cls):
        dockenv.prebuild_plugins(*cls.PLUGINS)

    def setUp(self):
        super().setUp()
        for path in self.PLUGINS:
            self.store.install(path)
    def plugin_copy(self, src: Path, plugin_id: str, edit=None) -> Path:
        """A copy of a fixture under its own id (so its image is the test's own), optionally edited first."""
        copies = self.tmp / "copies"
        copies.mkdir(exist_ok=True)
        copy = dockenv.plugin_copy(src, copies, plugin_id)
        self.own_image(f"dock/{plugin_id}:0.1.0")
        if edit:
            edit(copy)
        return copy

    def run_calls(self):
        return self.calls("run")

    def container_names(self):
        return [c[c.index("--name") + 1] for c in self.run_calls()]

    def removed(self):
        return [c[-1] for c in self.calls("rm")]


def wait_until(predicate, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


HOST = socket.gethostname()


def owner(pid: int, host: str = HOST) -> str:
    return f"{host}:{pid}"


class Sweep(DockIsolated):
    """R9: containers of dock processes that no longer exist are removed at the next start."""

    PREFIX = "dock-test-sweep-"

    def seed(self, **owners) -> dict[str, str]:
        """Stopped containers labelled with the given owners; returns short name -> real name.

        Only these (named with a test prefix, removed afterwards) are judged by the tests: a stale container of
        some real dock on this machine may be swept too, which is what a sweep is for, but is no business of ours.
        """
        names = {}
        tag = f"{os.getpid()}-{time.monotonic_ns()}"
        for short, label in owners.items():
            name = f"{self.PREFIX}{tag}-{short}"
            r = dockenv.run_runtime(
                "create", "--name", name, "--label", f"dev.dock.owner={label}", "python:3.15.2-slim", "true"
            )
            self.assertEqual(r.returncode, 0, r.stderr)
            self.addCleanup(dockenv.run_runtime, "rm", "-f", name)
            names[short] = name
        return names

    def swept(self, result) -> list[str]:
        return [n for n in result if n.startswith(self.PREFIX)]

    def removed_mine(self):
        return [n for n in self.removed() if n.startswith(self.PREFIX)]

    def test_sweep_removes_only_containers_of_dead_owners(self):
        self.configure()
        names = self.seed(stale=owner(dead_pid()), mine=owner(os.getpid()))
        self.assertEqual(self.swept(isolation.sweep(self.store)), [names["stale"]])
        self.assertEqual(self.removed_mine(), [names["stale"]])
        self.assertFalse(dockenv.container_exists(names["stale"]))
        self.assertTrue(dockenv.container_exists(names["mine"]))
        ps = self.calls("ps")[0]
        self.assertEqual(ps[:5], ["ps", "-a", "--filter", "label=dev.dock.owner", "--format"])
        self.assertEqual(ps[5], '{{.Names}} {{.Label "dev.dock.owner"}}')

    def test_sweep_is_a_no_op(self):
        with self.subTest("skipped on windows"):
            self.configure()
            names = self.seed(stale=owner(dead_pid()))
            with mock.patch.object(isolation.sys, "platform", "win32"):
                self.assertEqual(isolation.sweep(self.store), [])
            self.assertEqual(self.calls(), [])
            self.assertTrue(dockenv.container_exists(names["stale"]))
        with self.subTest("without a container table and no docker it calls nothing"):
            (self.store.home / "config.toml").unlink()
            self.reset_calls()
            self.seed(stale=owner(dead_pid()))
            empty = self.tmp / "empty"
            empty.mkdir()
            with mock.patch.dict(os.environ, {"PATH": str(empty)}):  # no `docker` anywhere
                self.assertEqual(isolation.sweep(self.store), [])
            self.assertEqual(self.calls(), [])
        with self.subTest("a missing runtime is silent"):
            self.configure(runtime="no-such-runtime-xyz")
            self.assertEqual(isolation.sweep(self.store), [])


class ReinstallDropsTheImage(RecordingRuntimeCase):
    """F6: an image is named by id and version, so a reinstall of the same version must not reuse a stale one.

    The plugin is a copy under its own id (dev.dock.reinstallprobe): a reinstall deletes its image, and the
    images of the fixtures are shared by the whole run and must never be touched.
    """

    ID = "dev.dock.reinstallprobe"
    TAG = f"dock/{ID}:1.2.3"

    def setUp(self):
        super().setUp()
        self.own_image(self.TAG)

    def sample(self) -> Manifest:
        root = make_plugin(CONTAINER)  # id dev.dock.sample, version 1.2.3
        text = (root / "plugin.toml").read_text().replace("dev.dock.sample", self.ID)
        (root / "plugin.toml").write_text(text)
        (root / "Dockerfile").write_text("FROM scratch\n")
        return self.store.install(root)

    def fresh_store(self) -> None:
        """A new DOCK_HOME, no config, no recorded calls: the start of the next case of a table-driven test."""
        self.store = Store(self.tmp / f"home-{time.monotonic_ns()}")
        self.reset_calls()

    def image_rms(self):
        return [c for c in self.calls("image") if c[1] == "rm"]

    def build_image(self, m):
        with redirect_stderr(StringIO()):
            isolation.ensure_image(isolation.config(self.store), self.store, m)

    def test_the_install_does_not_fail_when_the_old_image_cannot_be_dropped(self):
        with self.subTest("a missing runtime"):
            self.fresh_store()
            self.sample()
            self.configure(runtime="/nonexistent/runtime-xyz")
            with redirect_stderr(StringIO()):
                m = self.sample()
            self.assertIn(m.id, self.store.index())
        with self.subTest("a failing image rm"):
            self.fresh_store()
            self.sample()
            self.configure()  # nothing was built: the runtime answers `No such image` with exit 1
            m = self.sample()
            self.assertEqual(self.image_rms(), [["image", "rm", "-f", self.TAG]])
            self.assertIn(m.id, self.store.index())
        with self.subTest("a broken config"):
            self.fresh_store()
            self.sample()
            (self.store.home / "config.toml").write_text("[container\n")
            with redirect_stderr(StringIO()):
                m = self.sample()
            self.assertIn(m.id, self.store.index())
            self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
