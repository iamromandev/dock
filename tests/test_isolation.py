"""Container isolation: manifest entry, config, argv, and the host wiring, against the real container runtime."""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import nullcontext, redirect_stderr
from io import StringIO
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import dockenv  # noqa: E402
from cli import API_VERSION, isolation  # noqa: E402
from cli.manifest import ContainerRun, Manifest, ManifestError  # noqa: E402
from cli.process import PluginProcess  # noqa: E402
from cli.store import Store  # noqa: E402

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
    def test_container_entry_is_parsed(self):
        cases = (
            ('container = { cmd = "python3" }', ContainerRun(cmd="python3", args=[], dockerfile="Dockerfile")),
            ('container = { cmd = "/plugin/x", args = ["a", "b"], dockerfile = "build/Df" }',
             ContainerRun("/plugin/x", ["a", "b"], "build/Df")),
        )
        for run, expected in cases:
            with self.subTest(run=run):
                self.assertEqual(Manifest.load(make_plugin(run)).container, expected)

    def test_an_invalid_container_entry_is_rejected_naming_the_problem(self):
        cases = (
            ('container = { args = ["a"] }', "container.*cmd"),
            ('container = { cmd = "x", args = "a" }', "container.*args"),
            ('container = "python3"', "container"),
        )
        for run, pattern in cases:
            with self.subTest(run=run), self.assertRaisesRegex(ManifestError, pattern):
                Manifest.load(make_plugin(run))

    def test_container_dockerfile_must_stay_inside_the_plugin_folder(self):
        for dockerfile in ("../x", "/etc/passwd", "link"):
            with self.subTest(dockerfile=dockerfile):
                root = make_plugin(f'container = {{ cmd = "x", dockerfile = "{dockerfile}" }}')
                if dockerfile == "link":  # a symlink escaping the folder
                    outside = temp_dir("dock-iso-out-")
                    (root / "link").symlink_to(outside / "Dockerfile")
                with self.assertRaisesRegex(ManifestError, "container.*dockerfile.*inside"):
                    Manifest.load(root)

    def test_container_plugin_id_and_version_must_fit_an_image_tag(self):
        for field, value in (("id", ".hidden"), ("id", "a/b.c"), ("id", "../x"), ("id", "has space.x"), ("id", "a:b.c"),
                             ("id", "-x.y"), ("version", ".1"), ("version", "1.0+build"), ("version", "1 0"),
                             ("version", "a/b"), ("version", "-1")):
            with self.subTest(field=field, value=value):
                root = make_plugin('container = { cmd = "python3" }')
                text = (root / "plugin.toml").read_text()
                old = 'id = "dev.dock.sample"' if field == "id" else 'version = "1.2.3"'
                (root / "plugin.toml").write_text(text.replace(old, f'{field} = "{value}"'))
                # An id is refused by the general check (it names store folders); a version, for the image tag.
                pattern = r"id '.*' may only hold" if field == "id" else r"container.*version|version.*container"
                with self.assertRaisesRegex(ManifestError, pattern):
                    Manifest.load(root)
        # dots, dashes and underscores are fine
        root = make_plugin('container = { cmd = "python3" }')
        text = (root / "plugin.toml").read_text().replace("dev.dock.sample", "Dev_1.dock-x").replace("1.2.3", "1.2.3-rc_1")
        (root / "plugin.toml").write_text(text)
        self.assertEqual(Manifest.load(root).id, "Dev_1.dock-x")


class ManifestEnv(unittest.TestCase):
    """R6: [capabilities].env, the host variables a plugin asks to receive inside its container."""

    def load(self, names: str) -> Manifest:
        return Manifest.load(make_plugin('container = { cmd = "python3" }', f"\n[capabilities]\nenv = {names}\n"))

    def test_reserved_names_cover_everything_dock_sets_in_a_container(self):
        from cli.manifest import RESERVED_ENV
        set_by_dock = set(isolation.ENV_ALLOW) | {"HOME", "PATH", "DOCK_PLUGIN_ID", "DOCK_API_VERSION"}
        self.assertLessEqual(set_by_dock, RESERVED_ENV)
        m = Manifest.load(make_plugin('container = { cmd = "python3" }'))
        self.assertLessEqual(set(isolation.container_env(m)), RESERVED_ENV)

    def test_accepted_env(self):
        with self.subTest("no env means an empty list"):
            self.assertEqual(Manifest.load(make_plugin('container = { cmd = "python3" }')).env, [])
        with self.subTest("declared names are kept in order"):
            self.assertEqual(self.load('["MY_VAR", "_X1", "A"]').env, ["MY_VAR", "_X1", "A"])
        with self.subTest("duplicates are dropped silently keeping the first"):
            self.assertEqual(self.load('["B", "A", "B", "A"]').env, ["B", "A"])
        with self.subTest("env does not disturb the other capabilities"):
            root = make_plugin('container = { cmd = "python3" }',
                               '\n[capabilities]\npermissions = ["network"]\nenv = ["MY_VAR"]\n')
            m = Manifest.load(root)
            self.assertEqual((m.permissions, m.env), (["network"], ["MY_VAR"]))

    def test_names_that_are_not_variable_names_are_rejected_naming_the_entry(self):
        for bad in ("lower", "Mixed", "1ABC", "A-B", "A B", "A=B", ""):
            with self.subTest(name=bad):
                with self.assertRaisesRegex(
                    ManifestError, rf"env entry '{bad}' must look like an environment variable name \(A-Z, 0-9, _\)"
                ):
                    self.load(f'["{bad}"]')
        with self.assertRaisesRegex(ManifestError, r"plugin\.toml: env entry 'lower'"):  # the error names the file
            self.load('["lower"]')

    def test_names_dock_sets_itself_are_rejected_naming_the_entry(self):
        for bad in ("HOME", "PATH", "LANG", "TZ", "DOCK_PLUGIN_ID", "DOCK_API_VERSION"):
            with self.subTest(name=bad):
                with self.assertRaisesRegex(ManifestError, f"env entry '{bad}' is set by dock already"):
                    self.load(f'["{bad}"]')

    def test_env_must_be_a_list_of_names(self):
        for bad in ('"MY_VAR"', "[1]", "{ A = 1 }", '["A", 2]'):
            with self.subTest(env=bad):
                with self.assertRaisesRegex(ManifestError, r"\[capabilities\]\.env must be a list of names"):
                    self.load(bad)

    def test_a_bad_env_fails_the_install(self):
        store = Store(temp_dir("dock-iso-install-") / "home")
        root = make_plugin('container = { cmd = "python3" }', '\n[capabilities]\nenv = ["PATH"]\n')
        with self.assertRaisesRegex(ManifestError, "env entry 'PATH'"):
            store.install(root)
        self.assertEqual(store.index(), {})


class ContainerConfig(unittest.TestCase):
    """`[container] runtime` in config.toml, and what happens to a config that is not what dock can honour."""

    def store(self, config: str | bytes | None) -> Store:
        home = temp_dir("dock-iso-home-")
        if isinstance(config, str):
            (home / "config.toml").write_text(config)
        elif config is not None:
            (home / "config.toml").write_bytes(config)
        return Store(home)

    def test_valid_configs(self):
        with self.subTest("no container table means automatic runtime"):
            self.assertEqual(isolation.config(self.store(None)), isolation.Config())
            self.assertEqual(isolation.config(self.store("[shell]\nidle_timeout = 5\n")), isolation.Config())
            self.assertIsNone(isolation.Config().runtime)
        with self.subTest("an empty container table means automatic runtime"):
            self.assertEqual(isolation.config(self.store("[container]\n")), isolation.Config())
        with self.subTest("runtime is read from the container table"):
            cfg = isolation.config(self.store('[container]\nruntime = "/opt/podman"\n'))
            self.assertEqual(cfg, isolation.Config("/opt/podman"))
        with self.subTest("other tables next to a real container table are fine"):
            cfg = isolation.config(self.store('[shell]\nidle_timeout = 5\n[container]\nruntime = "podman"\n'))
            self.assertEqual(cfg.runtime, "podman")
        with self.subTest("unrelated tables are not mistaken for container"):
            text = '[shell]\nidle_timeout = 5\n[other]\nx = 9\n[something_else]\na = 1\n'
            self.assertEqual(isolation.config(self.store(text)), isolation.Config())

    def test_type_errors_are_reported(self):
        for body in ("runtime = 3", 'runtime = ""', "runtime = true", 'runtime = ["docker"]'):
            with self.subTest(body=body):
                with self.assertRaisesRegex(ManifestError, r"\[container\] runtime must be a command name or path"):
                    isolation.config(self.store(f"[container]\n{body}\n"))
        with self.subTest("container must be a table"):
            with self.assertRaisesRegex(ManifestError, r"\[container\] must be a table"):
                isolation.config(self.store("container = true\n"))

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

    def test_typos_are_refused_suggesting_the_table(self):
        with self.subTest("the runtime key at the top level"):
            with self.assertRaisesRegex(ManifestError, r"\[container\] table"):
                isolation.config(self.store('runtime = "docker"\n\n[shell]\nidle_timeout = 5\n'))
        for name in ("contaner", "Container", "CONTAINER", "containers", "containr", "container_"):
            with self.subTest(name=name):
                with self.assertRaisesRegex(ManifestError, rf"\[{name}\].*\[container\]"):
                    isolation.config(self.store(f'[{name}]\nruntime = "docker"\n'))

    # AC11: a table of a removed feature stops every command that starts a plugin
    def test_a_removed_table_is_refused_naming_it(self):
        for table, text in (("isolation", "[isolation]\nenabled = true\n"), ("wasm", '[wasm]\nruntime = "x"\n'),
                            ("http", '[http]\nhosts = ["x"]\n'), ("providers", '[providers]\nllm = "x"\n'),
                            ("serve", "[serve]\nidle_timeout = 9\n")):
            with self.subTest(table=table):
                for check in (isolation.config, lambda store: store.config()):
                    with self.assertRaisesRegex(ManifestError, rf"config\.toml: \[{table}\] "):
                        check(self.store(text))
        with self.subTest("the isolation message says to use container runtime"):
            with self.assertRaises(ManifestError) as cm:
                isolation.config(self.store("[isolation]\nenabled = true\n"))
            self.assertIn('[container] runtime = "docker"', str(cm.exception))
        with self.subTest("even as an inline table"):
            with self.assertRaisesRegex(ManifestError, r"\[isolation\] was replaced"):
                isolation.config(self.store("isolation = { enabled = true }\n[shell]\nidle_timeout = 5\n"))

    # a config that does not parse is fatal: nothing says which part would have been about the runtime
    def test_a_config_that_does_not_parse_is_refused_wherever_the_damage_is(self):
        for text in ("[container\nruntime = 'x'\n", "[shell\nidle_timeout = 5\n", '[catalog]\npaths = []\n[shell\n',
                     "[container]\nruntime = \n", "[[container\n", 'runtime = "x\n'):
            with self.subTest(text=text):
                for check in (isolation.config, lambda store: store.config()):
                    with self.assertRaisesRegex(ManifestError, r"config\.toml: "):
                        check(self.store(text))

    def test_undecodable_and_unreadable_files_are_refused(self):
        with self.assertRaises(ManifestError):
            isolation.config(self.store(b"# caf\xe9\n[shell]\nidle_timeout = 5\n"))
        store = self.store(None)
        (store.home / "config.toml").mkdir()  # exists, cannot be read as a file
        with self.assertRaises(ManifestError):
            isolation.config(store)
        with self.assertRaises(ManifestError):
            store.config()


class ResolveRuntime(unittest.TestCase):
    """AC7, AC8: which runtime dock calls, found without asking the runtime anything."""

    def which(self, found: dict[str, str]):
        return mock.patch.object(isolation.shutil, "which", side_effect=lambda name: found.get(name))

    def test_resolution_order(self):
        both = {"podman": "/opt/bin/podman", "docker": "/usr/bin/docker"}
        cases = (
            ("a configured path is used", None, sys.executable, sys.executable),
            ("a configured name is looked up on the path", both, "podman", "/opt/bin/podman"),
            ("without a setting docker comes first", both, None, "/usr/bin/docker"),
            ("without docker podman is used", {"podman": "/opt/bin/podman"}, None, "/opt/bin/podman"),
        )
        for label, found, configured, expected in cases:
            with self.subTest(label), (self.which(found) if found is not None else nullcontext()):
                self.assertEqual(isolation.resolve_runtime(isolation.Config(configured)), expected)

    def test_no_runtime_gives_the_message_that_names_the_fix(self):
        with self.which({}):
            with self.assertRaises(ManifestError) as cm:
                isolation.resolve_runtime(isolation.Config())
        self.assertEqual(str(cm.exception), "no container runtime found (install docker or podman, "
                                            "or set [container] runtime in ~/.dock/config.toml)")
        store = Store(temp_dir("dock-iso-home-"))  # and the message names the config file of this store
        with self.which({}):
            with self.assertRaises(ManifestError) as cm:
                isolation.resolve_runtime(isolation.Config(), store)
        self.assertIn(f"set [container] runtime in {store.home / 'config.toml'})", str(cm.exception))

    def test_a_configured_runtime_that_is_missing_is_refused_naming_it(self):
        with self.which({"docker": "/usr/bin/docker"}):  # a configured runtime never falls back to another one
            with self.assertRaises(ManifestError) as cm:
                isolation.resolve_runtime(isolation.Config("/nonexistent/runtime-xyz"))
        self.assertEqual(str(cm.exception), "container runtime '/nonexistent/runtime-xyz' not found")

    def test_a_configured_relative_path_is_refused_not_resolved_against_some_folder(self):
        for rel in ("./docker", "bin/docker", "../docker"):
            with self.subTest(rel), self.assertRaises(ManifestError) as cm:
                isolation.resolve_runtime(isolation.Config(rel))
            self.assertIn(rel, str(cm.exception))
            self.assertIn("absolute path or a command name", str(cm.exception))

    def test_a_runtime_found_through_a_relative_path_entry_is_made_absolute(self):
        folder = temp_dir("dock-iso-path-")
        runtime = folder / "docker"
        runtime.write_text("#!/bin/sh\n")
        runtime.chmod(0o755)
        old = os.getcwd()
        self.addCleanup(os.chdir, old)
        os.chdir(folder)
        for entry in (".", "/nonexistent:", ":/nonexistent", "./"):
            with self.subTest(repr(entry)), mock.patch.dict(os.environ, {"PATH": entry}):
                found = isolation.resolve_runtime(isolation.Config())
                self.assertTrue(os.path.isabs(found), found)
                self.assertEqual(os.path.realpath(found), os.path.realpath(runtime))
                found = isolation.resolve_runtime(isolation.Config("docker"))
                self.assertTrue(os.path.isabs(found), found)
        with self.subTest("resolved against dock's cwd, not a later cwd"):
            with mock.patch.dict(os.environ, {"PATH": "."}):
                found = isolation.resolve_runtime(isolation.Config())
            os.chdir(old)
            self.assertEqual(os.path.realpath(found), os.path.realpath(runtime))


class RunArgv(unittest.TestCase):
    RUNTIME = "docker"

    def manifest(self, permissions=(), container='{ cmd = "python3", args = ["/plugin/main.py"] }'):
        extra = f"\n[capabilities]\npermissions = {json.dumps(list(permissions))}\n"
        return Manifest.load(make_plugin(f"container = {container}", extra))

    def argv(self, m, env=None, cwd="/work/dir", uid_gid=(501, 20), data_dir="/home/u/.dock/data/dev.dock.sample",
             passthrough=()):
        return isolation.run_argv(self.RUNTIME, m, name="dock-dev-dock-sample-1-0", data_dir=data_dir,
                                  cwd=cwd, uid_gid=uid_gid, env=env if env is not None else {"HOME": "/tmp"},
                                  passthrough=passthrough)

    def test_exact_argv_without_permissions(self):
        m = self.manifest()
        env = {"LANG": "C", "HOME": "/tmp", "DOCK_PLUGIN_ID": m.id}
        self.assertEqual(self.argv(m, env), [
            "docker", "run", "-i", "--rm", "--pull", "never", "--name", "dock-dev-dock-sample-1-0",
            "--label", f"dev.dock.owner={socket.gethostname()}:{os.getpid()}",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--read-only", "--tmpfs", "/tmp",
            "--user", "501:20",
            "--network", "none",
            "--mount", "type=bind,src=/home/u/.dock/data/dev.dock.sample,dst=/home/u/.dock/data/dev.dock.sample",
            "--workdir", "/plugin",
            "-e", "LANG=C", "-e", "HOME=/tmp", "-e", f"DOCK_PLUGIN_ID={m.id}",
            "dock/dev.dock.sample:1.2.3", "python3", "/plugin/main.py",
        ])

    def test_exact_argv_with_passed_through_names_after_the_value_pairs_and_without_values(self):
        m = self.manifest()
        argv = self.argv(m, {"HOME": "/tmp"}, passthrough=["MY_VAR", "OTHER_VAR"])
        i = argv.index("--workdir")
        self.assertEqual(argv[i:], [
            "--workdir", "/plugin", "-e", "HOME=/tmp", "-e", "MY_VAR", "-e", "OTHER_VAR",
            "dock/dev.dock.sample:1.2.3", "python3", "/plugin/main.py",
        ])

    def test_passthrough_env_lists_declared_names_that_are_set_on_the_host(self):
        m = Manifest.load(make_plugin('container = { cmd = "python3" }',
                                      '\n[capabilities]\nenv = ["MY_VAR", "UNSET_VAR", "EMPTY_VAR"]\n'))
        with mock.patch.dict(os.environ, {"MY_VAR": "v", "EMPTY_VAR": "", "OTHER": "x"}):
            os.environ.pop("UNSET_VAR", None)
            self.assertEqual(isolation.passthrough_env(m), ["MY_VAR", "EMPTY_VAR"])  # empty counts as set
        with mock.patch.dict(os.environ, {"OTHER": "x"}):  # nothing declared: nothing passed
            self.assertEqual(isolation.passthrough_env(self.manifest()), [])

    def test_network_flag_only_without_network_permission(self):
        self.assertIn("--network", self.argv(self.manifest()))
        self.assertNotIn("--network", self.argv(self.manifest(["network"])))

    def test_cwd_is_mounted_only_with_fs_write_cwd(self):
        cwd_mount = "type=bind,src=/work/dir,dst=/work/dir"
        self.assertNotIn(cwd_mount, self.argv(self.manifest(["network"])))
        argv = self.argv(self.manifest(["fs:write:cwd"]))
        i = argv.index(cwd_mount)
        self.assertEqual(argv[i - 1], "--mount")
        data_mount = "type=bind,src=/home/u/.dock/data/dev.dock.sample,dst=/home/u/.dock/data/dev.dock.sample"
        self.assertLess(argv.index(data_mount), i)
        self.assertNotIn("-v", argv)
        # paths with spaces stay single arguments
        argv = self.argv(self.manifest(["fs:write:cwd"]), cwd="/my dir/x", data_dir="/data dir/p")
        self.assertIn("type=bind,src=/my dir/x,dst=/my dir/x", argv)
        self.assertIn("type=bind,src=/data dir/p,dst=/data dir/p", argv)

    def test_no_user_flag_without_posix_ids(self):
        self.assertNotIn("--user", self.argv(self.manifest(), uid_gid=None))

    def test_a_colon_comma_or_quote_in_a_mounted_path_is_refused_naming_the_path(self):
        for bad in ("/w/a:b", "/w/a,b", '/w/a"b'):
            with self.subTest(bad):
                with self.assertRaises(ManifestError) as cm:
                    self.argv(self.manifest(["fs:write:cwd"]), cwd=bad)
                self.assertIn(bad, str(cm.exception))
                self.assertIn("fs:write:cwd", str(cm.exception))
                with self.assertRaises(ManifestError) as cm:
                    self.argv(self.manifest(), data_dir=bad)
                self.assertIn(bad, str(cm.exception))
        # a colon in the cwd is fine when it is not mounted
        self.assertIn("type=bind,src=/home/u/.dock/data/dev.dock.sample,dst=/home/u/.dock/data/dev.dock.sample",
                      self.argv(self.manifest(["network"]), cwd="/w/a:b"))

    def test_container_env_is_only_the_allowlist_plus_dock_variables(self):
        m = self.manifest()
        host = {"PATH": "/usr/bin", "LANG": "C", "TZ": "UTC", "SECRET_TOKEN": "s3cret", "HOME": "/Users/x"}
        old = dict(os.environ)
        os.environ.clear()
        os.environ.update(host)
        try:
            env = isolation.container_env(m)
        finally:
            os.environ.clear()
            os.environ.update(old)
        self.assertEqual(env, {"LANG": "C", "TZ": "UTC", "HOME": "/tmp",
                               "DOCK_PLUGIN_ID": m.id, "DOCK_API_VERSION": API_VERSION})  # the host PATH is not passed on
        # allowlisted variables unset on the host are omitted
        os.environ.clear()
        try:
            env = isolation.container_env(self.manifest())
        finally:
            os.environ.update(old)
        self.assertEqual(sorted(env), ["DOCK_API_VERSION", "DOCK_PLUGIN_ID", "HOME"])

    def test_container_names_are_unique_per_process_and_valid(self):
        m = self.manifest()
        a, b = isolation.container_name(m), isolation.container_name(m)
        self.assertNotEqual(a, b)
        self.assertRegex(a, rf"^dock-dev-dock-sample-{os.getpid()}-\d+$")


class PluginProcessNeedsALaunch(unittest.TestCase):
    def test_a_plugin_process_cannot_be_built_without_saying_how_to_launch_it(self):
        m = Manifest.load(make_plugin(CONTAINER))
        with self.assertRaises(TypeError):
            PluginProcess(m, os.devnull, {})  # no silent fallback to some other kind of start


class LaunchIsAlwaysAContainer(unittest.TestCase):
    def test_a_launch_must_name_the_runtime_and_the_container(self):
        with self.assertRaises(TypeError):
            isolation.Launch([sys.executable, "-c", "pass"], {})  # no way to describe a native start
        with self.assertRaises(TypeError):
            isolation.Launch([sys.executable, "-c", "pass"], {}, runtime="docker")


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


class Prepare(RecordingRuntimeCase):
    def test_launch_runs_the_container_and_builds_the_image_first(self):
        self.configure()
        m = install(self.store, CONTAINER)
        with redirect_stderr(StringIO()) as err:
            launch = isolation.prepare(self.store, m, cwd="/some/cwd")
        self.assertEqual(launch.argv[:2], [str(self.wrapper), "run"])
        self.assertEqual(launch.runtime, str(self.wrapper))
        self.assertEqual(launch.argv[launch.argv.index("--name") + 1], launch.container)
        self.assertEqual(launch.argv[-3:], [SAMPLE_IMAGE, "python3", "/plugin/main.py"])
        self.assertEqual(launch.env["SECRET_TOKEN"], "s3cret")  # the runtime client needs the host env; -e is what counts
        self.assertNotIn("SECRET_TOKEN", " ".join(launch.argv))
        self.assertIn(f"dock: building image {SAMPLE_IMAGE}", err.getvalue())

        builds = self.calls("build")
        self.assertEqual(len(builds), 1)
        self.assertEqual(builds[0][:3], ["build", "-t", SAMPLE_IMAGE])
        self.assertEqual(builds[0][3:], ["-f", str(m.root / "Dockerfile"), str(m.root)])
        self.assertTrue(dockenv.image_exists(SAMPLE_IMAGE))

    def test_declared_env_is_passed_by_name_only_and_the_client_keeps_the_host_environment(self):
        self.configure()
        m = install(self.store, CONTAINER, '\n[capabilities]\nenv = ["MY_VAR", "UNSET_VAR"]\n')
        with mock.patch.dict(os.environ, {"MY_VAR": "hello-123", "OTHER": "nope"}):
            os.environ.pop("UNSET_VAR", None)
            with redirect_stderr(StringIO()):
                launch = isolation.prepare(self.store, m)
        passed = [launch.argv[i + 1] for i, a in enumerate(launch.argv) if a == "-e"]
        self.assertIn("MY_VAR", passed)  # name only
        self.assertNotIn("UNSET_VAR", passed)
        self.assertEqual(launch.env["MY_VAR"], "hello-123")  # the client resolves it from its own environment
        joined = " ".join(launch.argv)
        for leaked in ("hello-123", "OTHER", "nope", "UNSET_VAR"):
            self.assertNotIn(leaked, joined)

    def test_build_caching(self):
        self.configure()
        m = install(self.store, CONTAINER)
        with self.subTest("a second prepare does not build again"):
            with redirect_stderr(StringIO()):
                isolation.prepare(self.store, m)
                isolation.prepare(self.store, m)
            self.assertEqual(len(self.calls("build")), 1)
        with self.subTest("ensure_image with force rebuilds an existing image"):
            cfg = isolation.config(self.store)
            with redirect_stderr(StringIO()):
                isolation.ensure_image(cfg, self.store, m, force=True)
            self.assertEqual(len(self.calls("build")), 2)

    def test_failing_build_refuses_with_the_log_path(self):
        self.configure()
        m = install(self.store, CONTAINER, dockerfile="FROM scratch\nRUN false\n")
        with redirect_stderr(StringIO()):
            with self.assertRaises(ManifestError) as cm:
                isolation.prepare(self.store, m)
        self.assertEqual(str(cm.exception), f"dev.dock.sample: image build failed (see {self.store.log_path(m.id)})")
        self.assertEqual(self.calls("run"), [])
        self.assertFalse(dockenv.image_exists(SAMPLE_IMAGE))
        log = self.store.log_path(m.id).read_text()
        self.assertIn(f"build {SAMPLE_IMAGE}", log)  # dock's header for the build
        self.assertGreater(len(log.splitlines()), 2)  # and the runtime's own build output after it

    def test_plugin_with_container_entry_but_no_dockerfile_is_refused(self):
        self.configure()
        m = install(self.store, CONTAINER, dockerfile=False)
        with self.assertRaisesRegex(ManifestError, "the plugin has no Dockerfile"):
            isolation.prepare(self.store, m)

    def test_missing_runtime_is_refused(self):
        self.configure(runtime="/nonexistent/runtime-xyz")
        m = install(self.store, CONTAINER)
        with self.assertRaises(ManifestError) as cm:
            isolation.prepare(self.store, m)
        self.assertEqual(str(cm.exception), "container runtime '/nonexistent/runtime-xyz' not found")

    def test_a_data_directory_with_a_colon_is_refused_before_anything_is_started(self):
        store = Store(self.tmp / "ho:me")
        dockenv.write_config(store.home, self.wrapper)
        m = install(store, CONTAINER)
        with self.assertRaises(ManifestError) as cm:
            isolation.prepare(store, m)
        self.assertIn(str(store.data_root / m.id), str(cm.exception))
        self.assertEqual(self.calls(), [])


class PopenIsOnlyEverTheRuntime(RecordingRuntimeCase):
    """AC19 / NFR1: the one `Popen` in cli/process.py is only ever given a `<runtime> run` command line."""

    def test_every_plugin_argv_given_to_popen_is_the_runtime_then_run(self):
        from cli import process
        from cli.lifecycle import Session

        dockenv.prebuild_plugins(ROOT / "plugin" / "hello")
        self.store.install(ROOT / "plugin" / "hello")
        self.configure()
        seen = []
        real_popen = subprocess.Popen

        def spy(argv, *args, **kw):
            seen.append(list(argv))
            return real_popen(argv, *args, **kw)

        class Proxy:  # cli.process's view of `subprocess`, with only Popen watched
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
                return {"output": self.output, "log": self.log, "bar": self.bar, "table": self.table,
                        "panel": self.panel, "clear": self.clear, "prompt_user": self.prompt_user,
                        "form": self.form}

        with mock.patch.object(process, "subprocess", Proxy()), \
                mock.patch("cli.lifecycle.make_ui", side_effect=lambda *a, **k: UI()):
            session = Session(self.store)
            try:
                self.assertEqual(session.invoke(self.store.get("dev.dock.hello"), "hello", ["Ada"]), 0)
                session.invoke(self.store.get("dev.dock.hello"), "count", ["1"])  # a second call: same process
            finally:
                session.close()
        self.assertEqual(events[0], "hello, Ada!\n")
        self.assertEqual(len(seen), 1)  # one plugin, one start
        self.assertEqual(seen[0][:2], [str(self.wrapper), "run"])


class PluginFolderNeverRunsNatively(RecordingRuntimeCase):
    """A plugin that ships an executable named like the runtime must not get it run on the host."""

    MARKER = "#!/bin/sh\necho ran > \"{marker}\"\n"

    def ship(self, root: Path, name: str, marker: Path) -> None:
        (root / name).write_text(self.MARKER.format(marker=marker))
        (root / name).chmod(0o755)

    def run_plugin(self, launch, m) -> None:
        proc = PluginProcess(m, self.store.log_path(m.id), {}, launch)
        try:
            proc.start()
            proc.proc.wait(timeout=60)
        except OSError:
            pass  # nothing was found to run: fine for this test
        finally:
            if proc.proc:
                proc.kill()
            else:
                proc._log.close()

    def test_a_dot_in_path_never_makes_the_plugins_own_docker_the_runtime(self):
        self.configure(runtime=self.wrapper.name)  # a bare name, found on PATH
        m = install(self.store, CONTAINER)
        marker = self.tmp / "marker"
        self.ship(m.root, self.wrapper.name, marker)
        old = os.getcwd()
        self.addCleanup(os.chdir, old)
        os.chdir(self.tmp)  # dock runs here: it holds the recording wrapper, the plugin folder holds the marker script
        with mock.patch.dict(os.environ, {"PATH": f".{os.pathsep}{os.environ['PATH']}"}):
            with redirect_stderr(StringIO()):
                launch = isolation.prepare(self.store, m)
            self.assertTrue(os.path.isabs(launch.argv[0]), launch.argv[0])
            self.run_plugin(launch, m)
        self.assertFalse(marker.exists(), "the plugin's own docker ran on the host")

    def test_a_relative_program_is_never_looked_up_in_the_plugin_folder(self):
        m = install(self.store, CONTAINER)
        marker = self.tmp / "marker"
        self.ship(m.root, "tool", marker)
        old = os.getcwd()
        self.addCleanup(os.chdir, old)
        os.chdir(self.tmp)
        self.run_plugin(isolation.Launch(["./tool"], dict(os.environ), runtime="./tool", container="x"), m)
        self.assertFalse(marker.exists(), "Popen resolved a relative program against the plugin folder")


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

    def test_a_project_directory_is_mounted(self):
        project = (self.user_home / "project").resolve()
        launch = self.prepare(project)
        self.assertIn(f"type=bind,src={project},dst={project}", launch.argv)
        # without fs:write:cwd any cwd is fine because nothing is mounted
        m = Manifest.load(make_plugin(CONTAINER, '\n[capabilities]\npermissions = ["network"]'))
        (m.root / "Dockerfile").write_text("FROM scratch\n")
        launch = self.prepare("/", m)
        self.assertNotIn("/:/", launch.argv)

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

    def test_a_symlink_to_a_refused_directory_is_refused(self):
        link = self.tmp / "innocent"
        link.symlink_to(self.user_home)
        self.assertRefused(link, "home directory")

    def test_a_cwd_with_a_colon_or_comma_is_refused_before_anything_is_built_or_started(self):
        for name in ("a:b", "a,b"):
            odd = self.user_home / name
            odd.mkdir()
            with self.subTest(name), self.assertRaises(ManifestError) as cm:
                self.prepare(odd)
            self.assertIn(str(odd), str(cm.exception))
            self.assertEqual(self.calls(), [])  # no image probe, no build, no run

    def test_a_case_variant_of_a_refused_directory_is_refused(self):
        for label, real, reason in (("the home directory", self.user_home, "home directory"),
                                    ("the dock home", self.store.home, "dock home")):
            with self.subTest(label):
                variant = Path(str(real).swapcase())
                if not variant.exists() or not os.path.samefile(variant, real):
                    self.skipTest("the temporary filesystem is case-sensitive")
                self.assertRefused(variant, reason)


FIXTURES = ROOT / "tests" / "fixtures"


class DockIsolated(RecordingRuntimeCase):
    """Real `python3 -m cli` runs against the real runtime, recorded."""

    PLUGINS = (ROOT / "plugin" / "hello", FIXTURES / "probe")

    @classmethod
    def setUpClass(cls):
        dockenv.prebuild_plugins(*cls.PLUGINS)

    def setUp(self):
        super().setUp()
        for path in self.PLUGINS:
            self.store.install(path)
        self.work = self.tmp / "work dir"  # a space: paths must stay single arguments
        self.work.mkdir()
        self.env = {**os.environ, "DOCK_HOME": str(self.store.home), "PYTHONPATH": str(ROOT)}

    def plugin_copy(self, src: Path, plugin_id: str, edit=None) -> Path:
        """A copy of a fixture under its own id (so its image is the test's own), optionally edited first."""
        copies = self.tmp / "copies"
        copies.mkdir(exist_ok=True)
        copy = dockenv.plugin_copy(src, copies, plugin_id)
        self.own_image(f"dock/{plugin_id}:0.1.0")
        if edit:
            edit(copy)
        return copy

    def dock(self, *args, stdin="", env=None):
        return subprocess.run(
            [sys.executable, "-m", "cli", *args], input=stdin, capture_output=True, text=True,
            env=env or self.env, cwd=self.work, timeout=180, check=False, **dockenv.SIGINT_DEFAULT,
        )

    def spawn(self, *args):
        p = subprocess.Popen([sys.executable, "-m", "cli", *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, env=self.env, cwd=self.work, **dockenv.SIGINT_DEFAULT)
        self.addCleanup(p.kill)
        return p

    def run_calls(self):
        return self.calls("run")

    def container_names(self):
        return [c[c.index("--name") + 1] for c in self.run_calls()]

    def removed(self):
        return [c[-1] for c in self.calls("rm")]


class EnvPassthrough(DockIsolated):
    """R7/R8: declared variables reach the container by name only; nothing else of the host does."""

    HOST = {"MY_VAR": "hello-123", "OTHER": "nope", "EMPTY_VAR": ""}

    def setUp(self):
        super().setUp()
        self.configure()

        def declare(path):
            manifest = (path / "plugin.toml").read_text()
            manifest = manifest.replace('env = ["DOCK_TEST_DECLARED"]', 'env = ["MY_VAR", "UNSET_VAR", "EMPTY_VAR"]')
            self.assertIn("MY_VAR", manifest)
            (path / "plugin.toml").write_text(manifest)

        self.store.install(self.plugin_copy(FIXTURES / "jail", "dev.dock.envprobe", declare))
        self.host_env = {k: v for k, v in self.env.items() if k != "UNSET_VAR"} | self.HOST

    def probe(self, name):
        r = self.dock("dev.dock.envprobe:env", name, env=self.host_env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def test_the_recorded_argv_has_the_name_but_never_the_value(self):
        self.probe("MY_VAR")
        argv = self.run_calls()[0]
        passed = [argv[i + 1] for i, a in enumerate(argv) if a == "-e"]
        self.assertIn("MY_VAR", passed)
        self.assertIn("EMPTY_VAR", passed)  # set to the empty string on the host: still passed
        self.assertNotIn("UNSET_VAR", passed)
        text = json.dumps(argv)
        for leaked in ("hello-123", "OTHER", "nope", "UNSET_VAR"):
            self.assertNotIn(leaked, text)
        # a secret declared variable never appears in any runtime call
        env = {**self.host_env, "MY_VAR": "secret-xyz"}
        r = self.dock("dev.dock.envprobe:env", "MY_VAR", env=env)
        self.assertEqual(r.stdout.strip(), "set:secret-xyz", r.stderr)  # the plugin inside sees it
        self.assertNotIn("secret-xyz", json.dumps(self.calls()))

    def test_the_plugin_inside_sees_the_declared_variable_and_not_the_others(self):
        self.assertEqual(self.probe("MY_VAR"), "set:hello-123")
        self.assertEqual(self.probe("EMPTY_VAR"), "set:")
        self.assertEqual(self.probe("UNSET_VAR"), "unset")
        self.assertEqual(self.probe("OTHER"), "unset")


class IsolatedRun(DockIsolated):
    def setUp(self):
        super().setUp()
        self.configure()

    def test_the_probe_runs_through_the_runtime(self):
        r = self.dock("code", "3")
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertNotIn("building image", r.stderr)  # prebuilt: the shared fixture image is reused, not rebuilt
        self.assertEqual(len(self.calls("build")), 0)
        self.assertEqual(len(self.run_calls()), 1)

    def test_recorded_run_has_every_restriction_and_only_allowlisted_env(self):
        env = {**self.env, "SECRET_TOKEN": "s3cret", "LANG": "C.UTF-8", "TZ": "UTC"}
        self.assertEqual(self.dock("code", "0", env=env).returncode, 0)
        argv = self.run_calls()[0]
        self.assertEqual(argv[:6], ["run", "-i", "--rm", "--pull", "never", "--name"])  # AC1
        for flag in ("--read-only", "--cap-drop", "--security-opt", "--user", "--label"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--cap-drop") + 1], "ALL")
        self.assertEqual(argv[argv.index("--network") + 1], "none")
        self.assertEqual(argv[argv.index("--security-opt") + 1], "no-new-privileges")
        self.assertEqual(argv[argv.index("--tmpfs") + 1], "/tmp")
        self.assertEqual(argv[argv.index("--user") + 1], f"{os.getuid()}:{os.getgid()}")
        data_dir = str(self.store.data_root / "dev.dock.probe")
        self.assertIn(f"type=bind,src={data_dir},dst={data_dir}", argv)
        self.assertNotIn(f"type=bind,src={self.work},dst={self.work}", argv)  # probe declares no fs:write:cwd
        names = {argv[i + 1].split("=", 1)[0] for i, a in enumerate(argv) if a == "-e"}
        self.assertEqual(names, {"LANG", "TZ", "HOME", "DOCK_PLUGIN_ID", "DOCK_API_VERSION"})  # not PATH: the image's own
        self.assertNotIn("s3cret", " ".join(argv))

    def test_prompt_through_the_host_works_in_a_container(self):
        self.assertIn("hello, Ada!", self.dock("hello", stdin="Ada\n").stdout)

    def test_declared_inputs_reach_the_isolated_plugin(self):
        r = self.dock("fetch", "http://x", "-o", "out.bin")
        self.assertEqual(r.returncode, 0, r.stderr)
        got = json.loads(r.stdout)["inputs"]
        self.assertEqual(got, {"url": "http://x", "output": "out.bin", "retries": 3, "quiet": False})

    def test_shell_reuses_one_warm_container_for_two_operations(self):
        r = self.dock("shell", stdin="hello a\nhello b\n")
        self.assertIn("hello, a!", r.stdout)
        self.assertIn("hello, b!", r.stdout)
        self.assertEqual(len(self.run_calls()), 1)


def wait_until(predicate, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


class ContainerCleanup(DockIsolated):
    """R8/NFR4: whatever stops a plugin, its container is removed with `rm -f <name>`."""

    def setUp(self):
        super().setUp()
        self.configure()

    def assertAllRemoved(self):
        names = self.container_names()
        self.assertTrue(names, "no container was started")
        self.assertEqual(sorted(self.removed()), sorted(names))
        for name in names:  # and nothing is left in the runtime
            self.assertFalse(dockenv.container_exists(name), f"{name} is still there")

    def test_exit_paths_remove_the_container(self):
        with self.subTest("one-shot exit"):
            self.reset_calls()
            self.assertEqual(self.dock("code", "0").returncode, 0)
            self.assertAllRemoved()
        with self.subTest("every container of a session is removed at exit"):
            self.reset_calls()
            r = self.dock("shell", stdin="hello a\ncode 0\n")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(len(self.container_names()), 2)
            self.assertAllRemoved()
        with self.subTest("a crashed plugin's container"):
            self.reset_calls()
            self.assertEqual(self.dock("crash").returncode, 1)
            self.assertAllRemoved()

    def test_failed_initialize_removes_the_container(self):
        def die(path):
            (path / "main.py").write_text("import sys\nsys.exit(3)\n")

        self.store.install(self.plugin_copy(FIXTURES / "probe", "dev.dock.initfail", die))
        r = self.dock("dev.dock.initfail:code", "0")
        self.assertEqual(r.returncode, 1)
        self.assertIn("exited during 'initialize'", r.stderr)
        self.assertAllRemoved()

    def test_idle_reap_removes_the_container_while_the_shell_keeps_running(self):
        dockenv.add_config(self.store.home, "[shell]\nidle_timeout = 1\n")
        p = self.spawn("shell")
        p.stdin.write("code 0\n")
        p.stdin.flush()
        self.assertTrue(wait_until(lambda: len(self.run_calls()) == 1))
        time.sleep(3)  # idle (since the call ended) for longer than the timeout
        p.stdin.write("\n")  # a line at the prompt reaps what has been idle too long
        p.stdin.flush()
        self.assertTrue(wait_until(lambda: self.removed() != []), "idle plugin's container was not removed")
        self.assertIsNone(p.poll())  # the shell is still up
        p.communicate("", timeout=30)
        self.assertAllRemoved()

    def test_force_kill_after_cancel_grace_removes_the_container(self):
        self.env["DOCK_CANCEL_GRACE"] = "1"
        p = self.spawn("shell")
        p.stdin.write("stubborn 30\n")
        p.stdin.flush()
        self.assertTrue(wait_until(lambda: len(self.run_calls()) == 1))
        id_file = self.store.data_root / "dev.dock.probe" / "cid"
        self.assertTrue(wait_until(id_file.exists))
        time.sleep(0.3)
        p.send_signal(2)  # Ctrl-C: cancel is ignored, the grace period ends, the plugin is killed
        self.assertTrue(wait_until(lambda: self.removed() != []), "stuck plugin's container was not removed")
        self.assertIsNone(p.poll())
        p.communicate("", timeout=30)
        self.assertAllRemoved()


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
            r = dockenv.run_runtime("create", "--name", name, "--label", f"dev.dock.owner={label}",
                                    "python:3.12-slim", "true")
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

    def test_sweep_leaves_containers_it_cannot_judge_alone(self):
        self.configure()
        cases = (
            # its pid means nothing here
            ("another host", dict(elsewhere=owner(dead_pid(), host="some-other-host"))),
            # not host:pid: whoever wrote it, we cannot judge it
            ("the old pid-only label", dict(old=str(dead_pid()))),
            # os.kill would raise or signal a process group
            ("unparsable labels", dict(odd="not-a-pid", odd2=f"{HOST}:abc", odd3=f"{HOST}:", odd4=":12",
                                       huge=f"{HOST}:{'9' * 30}", zero=f"{HOST}:0")),
        )
        for label, owners in cases:
            with self.subTest(label):
                names = self.seed(**owners)
                self.assertEqual(self.swept(isolation.sweep(self.store)), [])
                for name in names.values():
                    self.assertTrue(dockenv.container_exists(name), name)

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

    def test_sweep_without_a_container_table_uses_docker_when_it_is_on_the_path(self):
        # The user may have dropped [container] after a crash: dock's own leftovers should still go.
        names = self.seed(stale=owner(dead_pid()), live=owner(os.getpid()))
        bindir = self.tmp / "bin"
        bindir.mkdir()
        (bindir / "docker").symlink_to(self.wrapper)
        with mock.patch.dict(os.environ, {"PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"}):
            self.assertEqual(self.swept(isolation.sweep(self.store)), [names["stale"]])
        self.assertEqual(self.removed_mine(), [names["stale"]])

    def test_sweep_is_capped_in_time_when_the_runtime_hangs(self):
        hang = self.tmp / "hang"
        hang.write_text("#!/bin/sh\nsleep 5\n")
        hang.chmod(0o755)
        self.configure(runtime=hang)
        started = time.monotonic()
        with mock.patch.object(isolation, "SWEEP_TIMEOUT", 0.5):
            self.assertEqual(isolation.sweep(self.store), [])
        self.assertLess(time.monotonic() - started, 3)

    def test_start_removes_a_stale_container_and_keeps_a_live_one(self):
        self.configure()
        with self.subTest("one-shot start"):
            names = self.seed(stale=owner(dead_pid()), live=owner(os.getpid()))
            self.assertEqual(self.dock("code", "0").returncode, 0)
            self.assertFalse(dockenv.container_exists(names["stale"]))  # AC6
            self.assertTrue(dockenv.container_exists(names["live"]))
        with self.subTest("shell start"):
            names = self.seed(stale=owner(dead_pid()))
            self.assertEqual(self.dock("shell", stdin="").returncode, 0)
            self.assertFalse(dockenv.container_exists(names["stale"]))


class BrokenConfig(DockIsolated):
    """AC11: a config.toml that dock cannot honour stops every command that starts a plugin."""

    def write(self, text: str | bytes) -> None:
        path = self.store.home / "config.toml"
        path.write_bytes(text if isinstance(text, bytes) else text.encode())

    def container_section(self, header: str = "[container]", body: str = "") -> str:
        return f'{header}\nruntime = "{self.wrapper}"\n{body}\n'

    def assertRefusedWithoutStarting(self, r):
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertNotIn("hello,", r.stdout)
        self.assertIn("config.toml", r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertFalse(self.store.log_path("dev.dock.hello").exists(), "a plugin process was started")
        self.assertEqual(self.run_calls(), [])

    def test_bad_config_refuses_without_starting(self):
        removed = (("isolation", "enabled = true"), ("wasm", 'runtime = "x"'),
                   ("http", 'hosts = ["x"]'), ("providers", 'llm = "x"'))
        cases = [(f"removed table [{t}]", lambda t=t, b=body: self.write(self.container_section() + f"[{t}]\n{b}\n"),
                  (f"[{t}] ",)) for t, body in removed]
        cases += [
            ("the isolation message says to use container runtime",
             lambda: self.write("[isolation]\nenabled = true\n"), ("[container] runtime",)),
            ("a broken container header", lambda: self.write(f'[container\nruntime = "{self.wrapper}"\n'), ()),
            ("a broken section elsewhere", lambda: self.write("[shell\n\n" + self.container_section()), ()),
            ("a misspelt container table", lambda: self.write(self.container_section("[contaner]")), ("[container]",)),
            ("an unknown key", lambda: self.write(self.container_section(body="runtimes = true")), ("runtimes", "known: runtime")),
            ("a non-utf8 config", lambda: self.write(b"# caf\xe9\n" + self.container_section().encode()), ()),
            ("an unreadable config", lambda: (self.store.home / "config.toml").mkdir(), ()),
        ]
        for label, setup, expected in cases:
            with self.subTest(label):
                path = self.store.home / "config.toml"
                if path.is_dir():  # the unreadable case makes it a directory
                    path.rmdir()
                path.unlink(missing_ok=True)
                self.reset_calls()
                self.store.log_path("dev.dock.hello").unlink(missing_ok=True)
                setup()
                r = self.dock("hello", "Roman")
                self.assertRefusedWithoutStarting(r)
                for text in expected:
                    self.assertIn(text, r.stderr)

    def test_a_broken_container_header_refuses_in_the_shell_too_without_starting(self):
        self.write(f'[container\nruntime = "{self.wrapper}"\n')
        r = self.dock("shell", stdin="hello a\nstatus\n")
        self.assertNotIn("hello, a!", r.stdout)
        self.assertIn("config.toml", r.stderr)
        self.assertFalse(self.store.log_path("dev.dock.hello").exists())

    def test_a_valid_container_table_with_other_tables_runs_in_a_container(self):
        self.write(self.container_section() + "[shell]\nidle_timeout = 5\n")
        r = self.dock("hello", "Roman")
        self.assertEqual(r.stdout.strip(), "hello, Roman!", r.stderr)
        self.assertEqual(len(self.run_calls()), 1)


class PluginBuild(DockIsolated):
    """R11: `dock plugin build <id>` (re)builds the image, isolation on or off."""

    def test_builds_the_image_and_reports_success_without_any_isolation_config(self):
        # No config.toml: the runtime defaults to `docker`, so point PATH at a dir with the recorder as `docker`.
        bindir = self.tmp / "bin"
        bindir.mkdir()
        (bindir / "docker").symlink_to(self.wrapper)
        env = {**self.env, "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"}
        r = self.dock("plugin", "build", "dev.dock.hello", env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("built dock/dev.dock.hello:0.2.0", r.stdout)
        builds = self.calls("build")
        self.assertEqual(len(builds), 1)
        self.assertEqual(builds[0][builds[0].index("-t") + 1], "dock/dev.dock.hello:0.2.0")
        self.assertEqual(builds[0][-1], str(self.store.get("dev.dock.hello").root))
        self.assertEqual(self.run_calls(), [])  # building starts nothing

    def test_rebuilds_even_when_the_image_exists(self):
        self.configure()
        self.assertEqual(self.dock("hello", "a").returncode, 0)  # the image exists: used, not built
        self.assertEqual(len(self.calls("build")), 0)
        r = self.dock("plugin", "build", "dev.dock.hello")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.calls("build")), 1)
        self.assertEqual(self.calls("image"), [["image", "inspect", "dock/dev.dock.hello:0.2.0"]])  # not asked again

    def test_bad_build_arguments_are_reported(self):
        with self.subTest("an unknown plugin is reported"):
            r = self.dock("plugin", "build", "dev.dock.nope")
            self.assertEqual(r.returncode, 1)
            self.assertIn("dev.dock.nope is not installed", r.stderr)
        with self.subTest("build needs exactly one id"):
            r = self.dock("plugin", "build")
            self.assertEqual(r.returncode, 2)
            self.assertIn("plugin build <id>", r.stderr)
        with self.subTest("a failing build prints the build-failed message and exits 1"):
            self.configure()

            def break_build(path):
                (path / "Dockerfile").write_text("FROM python:3.12-slim\nRUN false\n")

            self.store.install(self.plugin_copy(ROOT / "plugin" / "hello", "dev.dock.hellofail", break_build))
            r = self.dock("plugin", "build", "dev.dock.hellofail")
            self.assertEqual(r.returncode, 1)
            log = self.store.log_path("dev.dock.hellofail")
            self.assertIn(f"dock: dev.dock.hellofail: image build failed (see {log})", r.stderr)
            self.assertNotIn("built dock/", r.stdout)
        with self.subTest("a plugin without a Dockerfile is refused"):
            self.reset_calls()
            install(self.store, CONTAINER, dockerfile=False)
            self.configure()
            r = self.dock("plugin", "build", "dev.dock.sample")
            self.assertEqual(r.returncode, 1)
            self.assertIn("dock: dev.dock.sample: the plugin has no Dockerfile (Dockerfile): add one and reinstall it",
                          r.stderr)
            self.assertEqual(self.calls("build"), [])
        with self.subTest("a missing runtime is refused"):
            self.configure(runtime="no-such-runtime-xyz")
            r = self.dock("plugin", "build", "dev.dock.hello")
            self.assertEqual(r.returncode, 1)
            self.assertIn("dock: container runtime 'no-such-runtime-xyz' not found", r.stderr)

    def test_works_from_the_shell_and_the_help_lists_it(self):
        self.configure()
        r = self.dock("shell", stdin="plugin build dev.dock.hello\nhelp\n")
        self.assertIn("built dock/dev.dock.hello:0.2.0", r.stdout)
        self.assertEqual(len(self.calls("build")), 1)
        self.assertEqual(r.stdout.count("plugin build <id>"), 1)  # in the shell's help
        self.assertIn("plugin build <id>", self.dock("help").stdout)


class PluginInfoRuntime(DockIsolated):
    """AC15: `plugin info` has a `runtime:` line, and none of the lines of removed features."""

    def info(self, plugin_id, env=None):
        r = self.dock("plugin", "info", plugin_id, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def runtime_line(self, plugin_id, env=None):
        return next(l for l in self.info(plugin_id, env).splitlines() if l.startswith("runtime:"))

    def test_runtime_line(self):
        with self.subTest("without network and cwd access"):
            self.configure()
            self.assertEqual(self.runtime_line("dev.dock.hello"), "runtime:     docker (no network, no cwd access)")
            self.assertEqual(self.calls(), [])  # info asks the runtime nothing
        with self.subTest("the runtime is found without any config"):
            (self.store.home / "config.toml").unlink()
            self.assertEqual(self.runtime_line("dev.dock.hello"), "runtime:     docker (no network, no cwd access)")
        with self.subTest("network without cwd"):
            install(self.store, CONTAINER, '\n[capabilities]\npermissions = ["network"]')
            self.configure()
            self.assertEqual(self.runtime_line("dev.dock.sample"), "runtime:     docker (network, no cwd access)")
        with self.subTest("cwd and network"):  # a reinstall under the same id
            install(self.store, CONTAINER, '\n[capabilities]\npermissions = ["network", "fs:write:cwd"]')
            self.assertEqual(self.runtime_line("dev.dock.sample"), "runtime:     docker (network, cwd)")

    def test_info_layout_has_nothing_of_a_removed_feature(self):
        keys = [l.split(":")[0] for l in self.info("dev.dock.hello").splitlines()]  # the runtime after env, before location
        self.assertEqual(keys, ["id", "version", "protocol", "description", "commands", "permissions", "env", "keywords",
                                "runtime", "location"])
        text = self.info("dev.dock.hello")
        for removed in ("runner:", "isolation:", "builds:", "provision", "provides", "requires", "http:"):
            self.assertNotIn(removed, text)

    def test_info_survives_a_missing_runtime_and_a_broken_config(self):
        with self.subTest("no runtime found still shows info and says what to install"):
            empty = self.tmp / "empty"
            empty.mkdir()
            env = {**self.env, "PATH": str(empty)}  # AC8: info needs no runtime
            self.assertEqual(self.runtime_line("dev.dock.hello", env),
                             "runtime:     none found (install docker or podman, or set [container] runtime)")
        with self.subTest("a broken config does not break info"):
            (self.store.home / "config.toml").write_text("[container\n")
            self.assertIn("runtime:     unknown", self.runtime_line("dev.dock.hello"))


class PluginInfoEnv(DockIsolated):
    """R9: the `env:` line of `plugin info`, and the install summary."""

    def line(self, plugin_id, key="env:"):
        out = self.dock("plugin", "info", plugin_id).stdout
        return next(l for l in out.splitlines() if l.startswith(key))

    def test_the_env_line(self):
        with self.subTest("a dash without declared env"):
            self.assertEqual(self.line("dev.dock.hello"), "env:         -")
        with self.subTest("declared names are listed after the permissions"):
            install(self.store, CONTAINER, '\n[capabilities]\npermissions = ["network"]\nenv = ["A_ONE", "B_TWO"]\n')
            self.assertEqual(self.line("dev.dock.sample"), "env:         A_ONE, B_TWO")
            lines = self.dock("plugin", "info", "dev.dock.sample").stdout.splitlines()
            keys = [l.split(":")[0] for l in lines]
            self.assertEqual(keys.index("env"), keys.index("permissions") + 1)

    def test_install_summary_lists_env_only_when_declared(self):
        root = make_plugin(CONTAINER, '\n[capabilities]\nenv = ["A_ONE", "B_TWO"]\n')
        (root / "Dockerfile").write_text("FROM scratch\n")
        out = self.dock("plugin", "install", "--yes", str(root)).stdout
        self.assertIn("  env:         A_ONE, B_TWO", out.splitlines())
        # hello under another id: a reinstall of hello itself would delete the image the whole run shares
        out = self.dock("plugin", "install", str(self.plugin_copy(ROOT / "plugin" / "hello", "dev.dock.hellosummary"))).stdout
        self.assertNotIn("env:", out)


class ShellStatus(DockIsolated):
    """AC15: shell `status` lists each running plugin with its idle time and runtime, and nothing of removed features."""

    @staticmethod
    def status_line(stdout, plugin_id):
        lines = (l.replace("dock> ", "") for l in stdout.splitlines())  # prompts precede the first output
        return next(l for l in lines if l.startswith(plugin_id) and "running" in l)

    def test_status_shows_id_idle_time_and_the_runtime(self):
        self.configure()
        r = self.dock("shell", stdin="hello a\nstatus\n")
        line = self.status_line(r.stdout, "dev.dock.hello")
        self.assertRegex(line, r"^dev\.dock\.hello +running +idle 0m\d\ds +docker$")


class ReinstallDropsTheImage(RecordingRuntimeCase):
    """F6: an image is named by id and version, so a reinstall of the same version must not reuse a stale one.

    The plugin is a copy under its own id (dev.dock.reinstallprobe): a reinstall deletes its image, and the
    images of hello and the fixtures are shared by the whole run and must never be touched.
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

    def test_reinstalling_removes_the_old_image_through_the_configured_runtime(self):
        self.configure()
        m = self.sample()
        self.build_image(m)
        self.assertTrue(dockenv.image_exists(self.TAG))
        self.sample()
        self.assertIn(["image", "rm", "-f", self.TAG], self.image_rms())
        self.assertFalse(dockenv.image_exists(self.TAG))
        # the next run rebuilds the image
        with redirect_stderr(StringIO()):
            isolation.prepare(self.store, self.store.get(self.ID))
        self.assertEqual(len(self.calls("build")), 2)

    def test_a_first_install_asks_the_runtime_nothing_and_so_never_drops_a_shared_image(self):
        # A fresh DOCK_HOME installing hello must not delete the hello image other homes (and tests) rely on.
        self.configure()
        self.own_image(self.TAG)
        with redirect_stderr(StringIO()):
            isolation.ensure_image(isolation.config(self.store), self.store, self.sample())  # built by someone else
        other_home = Store(self.tmp / "other-home")
        root = make_plugin(CONTAINER)  # the same id and version, in another home: a first install there
        (root / "plugin.toml").write_text((root / "plugin.toml").read_text().replace("dev.dock.sample", self.ID))
        (root / "Dockerfile").write_text("FROM scratch\n")
        dockenv.write_config(other_home.home, self.wrapper)
        before = len(self.calls())
        other_home.install(root)
        self.assertEqual(self.calls()[before:], [])
        self.assertTrue(dockenv.image_exists(self.TAG))

    def test_with_only_the_runtime_on_the_path_a_reinstall_asks_it(self):
        self.sample()
        bindir = self.tmp / "bin"
        bindir.mkdir()
        (bindir / "docker").symlink_to(self.wrapper)
        with mock.patch.dict(os.environ, {"PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"}):
            self.sample()
        self.assertEqual(self.image_rms(), [["image", "rm", "-f", self.TAG]])

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

    def test_a_hanging_runtime_does_not_hold_the_install_up_forever(self):
        self.sample()
        hang = self.tmp / "hang"
        hang.write_text("#!/bin/sh\nsleep 5\n")
        hang.chmod(0o755)
        self.configure(runtime=hang)
        with mock.patch.object(isolation, "REMOVE_TIMEOUT", 0.5), redirect_stderr(StringIO()):
            started = time.monotonic()
            self.sample()
        self.assertLess(time.monotonic() - started, 4)

    def test_remove_then_install_of_the_same_version_leaves_no_old_image(self):
        self.configure()
        m = self.sample()
        self.build_image(m)
        self.assertTrue(dockenv.image_exists(self.TAG))
        self.store.remove(self.ID)
        self.assertFalse(dockenv.image_exists(self.TAG))
        self.assertIn(["image", "rm", "-f", self.TAG], self.image_rms())
        self.sample()
        with redirect_stderr(StringIO()):
            isolation.prepare(self.store, self.store.get(self.ID))
        self.assertEqual(len(self.calls("build")), 2)  # the new sources got a new image

    def test_remove_does_not_fail_when_the_runtime_cannot_be_asked(self):
        self.sample()
        self.configure(runtime="/nonexistent/runtime-xyz")
        with redirect_stderr(StringIO()):
            self.store.remove(self.ID)
        self.assertNotIn(self.ID, self.store.index())

    def test_stale_image_warning(self):
        with self.subTest("a broken config warns that the old image may be stale"):
            self.fresh_store()
            self.sample()
            (self.store.home / "config.toml").write_text("[container\n")
            with redirect_stderr(StringIO()) as err:
                self.sample()
            lines = err.getvalue().splitlines()
            self.assertEqual(len(lines), 1, lines)
            self.assertIn(f"dock: old image {self.TAG} may be stale", lines[0])
            self.assertIn(f"dock plugin build {self.ID}", lines[0])
        with self.subTest("remove warns when there is no runtime to drop the image"):
            self.fresh_store()
            self.sample()
            self.configure(runtime="/nonexistent/runtime-xyz")
            with redirect_stderr(StringIO()) as err:
                self.store.remove(self.ID)
            self.assertEqual(len(err.getvalue().splitlines()), 1, err.getvalue())
            self.assertIn(f"dock: old image {self.TAG} may be stale", err.getvalue())
        with self.subTest("a working runtime prints no warning"):
            self.fresh_store()
            self.configure()
            self.sample()
            with redirect_stderr(StringIO()) as err:
                self.sample()
                self.store.remove(self.ID)
            self.assertEqual(err.getvalue(), "")

    def test_removing_a_plugin_whose_folder_is_damaged_still_removes_it(self):
        self.configure()
        self.sample()
        (self.store.plugin_dir / self.ID / "1.2.3" / "plugin.toml").write_text("not toml [")
        self.store.remove(self.ID)
        self.assertNotIn(self.ID, self.store.index())


class StoreHomeIsAbsolute(unittest.TestCase):
    def test_a_relative_dock_home_becomes_absolute(self):
        base = temp_dir("dock-iso-rel-")
        old = os.getcwd()
        self.addCleanup(os.chdir, old)
        os.chdir(base)
        for how in ("argument", "environment"):
            with self.subTest(how):
                if how == "argument":
                    store = Store(Path("rel-home"))
                else:
                    with mock.patch.dict(os.environ, {"DOCK_HOME": "rel-home"}):
                        store = Store()
                self.assertTrue(store.home.is_absolute(), store.home)
                self.assertTrue(store.data_dir("dev.dock.sample").is_absolute())
                self.assertEqual(os.path.realpath(store.home), os.path.realpath(base / "rel-home"))


class InstallKeepsBuildFiles(unittest.TestCase):
    """R13: a container plugin keeps its build sources; others keep today's exclusions."""

    def setUp(self):
        self.store = Store(temp_dir("dock-iso-install-") / "home")

    def plugin(self, run: str) -> Path:
        root = make_plugin(run)
        (root / "Dockerfile").write_text("FROM scratch\n")
        for name in ("main.go", "go.mod", "lib.go"):
            (root / name).write_text("x")
        for d in (".git", "__pycache__", "node_modules"):
            (root / d).mkdir()
            (root / d / "f").write_text("x")
        return root

    def test_container_plugin_keeps_go_sources_and_build_files(self):
        m = self.store.install(self.plugin(CONTAINER))
        names = sorted(p.name for p in m.root.iterdir())
        self.assertEqual(names, ["Dockerfile", "go.mod", "lib.go", "main.go", "plugin.toml"])
        with self.subTest("a rust build output folder is never installed"):
            root = self.plugin(CONTAINER)
            (root / "target").mkdir()
            (root / "target" / "big").write_text("x")
            m = self.store.install(root)
            self.assertNotIn("target", {p.name for p in m.root.iterdir()})


if __name__ == "__main__":
    unittest.main()
