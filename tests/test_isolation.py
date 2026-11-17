"""Container isolation: manifest entry, config, argv, and the host wiring against a fake runtime."""

import json
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

from cli import API_VERSION, isolation  # noqa: E402
from cli.manifest import ContainerRun, Manifest, ManifestError  # noqa: E402
from cli.process import PluginProcess  # noqa: E402
from cli.store import Store  # noqa: E402

FAKE = ROOT / "tests" / "fixtures" / "fakeruntime" / "docker"

BASE = """\
id = "dev.dock.sample"
version = "1.2.3"
api_version = "1"
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
    def test_container_entry_is_parsed_with_defaults(self):
        m = Manifest.load(make_plugin('container = { cmd = "python3" }'))
        self.assertEqual(m.container, ContainerRun(cmd="python3", args=[], dockerfile="Dockerfile"))

    def test_container_entry_with_args_and_dockerfile(self):
        root = make_plugin('container = { cmd = "/plugin/x", args = ["a", "b"], dockerfile = "build/Df" }')
        m = Manifest.load(root)
        self.assertEqual(m.container, ContainerRun("/plugin/x", ["a", "b"], "build/Df"))

    def test_no_container_entry_means_none(self):
        m = Manifest.load(make_plugin('any = { cmd = "python3" }'))
        self.assertIsNone(m.container)

    def test_container_without_cmd_is_rejected_naming_the_problem(self):
        with self.assertRaisesRegex(ManifestError, "container.*cmd"):
            Manifest.load(make_plugin('container = { args = ["a"] }'))

    def test_container_dockerfile_outside_the_plugin_folder_is_rejected(self):
        with self.assertRaisesRegex(ManifestError, "container.*dockerfile.*inside"):
            Manifest.load(make_plugin('container = { cmd = "x", dockerfile = "../x" }'))

    def test_container_absolute_dockerfile_is_rejected(self):
        with self.assertRaisesRegex(ManifestError, "container.*dockerfile.*inside"):
            Manifest.load(make_plugin('container = { cmd = "x", dockerfile = "/etc/passwd" }'))

    def test_container_dockerfile_symlink_escaping_the_folder_is_rejected(self):
        root = make_plugin('container = { cmd = "x", dockerfile = "link" }')
        outside = temp_dir("dock-iso-out-")
        (root / "link").symlink_to(outside / "Dockerfile")
        with self.assertRaisesRegex(ManifestError, "inside"):
            Manifest.load(root)

    def test_container_args_must_be_a_list_of_strings(self):
        with self.assertRaisesRegex(ManifestError, "container.*args"):
            Manifest.load(make_plugin('container = { cmd = "x", args = "a" }'))

    def test_container_must_be_a_table(self):
        with self.assertRaisesRegex(ManifestError, "container"):
            Manifest.load(make_plugin('container = "python3"'))

    def test_container_plugin_id_and_version_must_fit_an_image_tag(self):
        for field, value in (("id", ".hidden"), ("id", "a/b.c"), ("id", "../x"), ("id", "has space.x"), ("id", "a:b.c"),
                             ("id", "-x.y"), ("version", ".1"), ("version", "1.0+build"), ("version", "1 0"),
                             ("version", "a/b"), ("version", "-1")):
            with self.subTest(field=field, value=value):
                root = make_plugin('container = { cmd = "python3" }')
                text = (root / "plugin.toml").read_text()
                old = 'id = "dev.dock.sample"' if field == "id" else 'version = "1.2.3"'
                (root / "plugin.toml").write_text(text.replace(old, f'{field} = "{value}"'))
                with self.assertRaisesRegex(ManifestError, rf"container.*{field}|{field}.*container"):
                    Manifest.load(root)

    def test_plugin_without_container_keeps_its_free_form_id_and_version(self):
        root = make_plugin('any = { cmd = "python3" }')
        text = (root / "plugin.toml").read_text().replace('version = "1.2.3"', 'version = "1.0+build"')
        (root / "plugin.toml").write_text(text)
        self.assertEqual(Manifest.load(root).version, "1.0+build")

    def test_container_plugin_id_and_version_with_dots_dashes_and_underscores_are_fine(self):
        root = make_plugin('container = { cmd = "python3" }')
        text = (root / "plugin.toml").read_text().replace("dev.dock.sample", "Dev_1.dock-x").replace("1.2.3", "1.2.3-rc_1")
        (root / "plugin.toml").write_text(text)
        self.assertEqual(Manifest.load(root).id, "Dev_1.dock-x")

    def test_entrypoint_ignores_the_container_key(self):
        m = Manifest.load(make_plugin('container = { cmd = "python3" }\nany = { cmd = "python3", args = ["m.py"] }'))
        self.assertEqual(m.entrypoint(), ["python3", "m.py"])

    def test_container_only_manifest_has_no_native_build(self):
        m = Manifest.load(make_plugin('container = { cmd = "python3" }'))
        with self.assertRaisesRegex(ManifestError, "no native build"):
            m.entrypoint()


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

    def test_no_env_means_an_empty_list(self):
        self.assertEqual(Manifest.load(make_plugin('container = { cmd = "python3" }')).env, [])

    def test_declared_names_are_kept_in_order(self):
        self.assertEqual(self.load('["MY_VAR", "_X1", "A"]').env, ["MY_VAR", "_X1", "A"])

    def test_duplicates_are_dropped_silently_keeping_the_first(self):
        self.assertEqual(self.load('["B", "A", "B", "A"]').env, ["B", "A"])

    def test_env_does_not_disturb_the_other_capabilities(self):
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

    def test_names_dock_sets_itself_are_rejected_naming_the_entry(self):
        for bad in ("HOME", "PATH", "LANG", "TZ", "DOCK_PLUGIN_ID", "DOCK_API_VERSION"):
            with self.subTest(name=bad):
                with self.assertRaisesRegex(ManifestError, f"env entry '{bad}' is set by dock already"):
                    self.load(f'["{bad}"]')

    def test_the_error_names_the_manifest_file(self):
        with self.assertRaisesRegex(ManifestError, r"plugin\.toml: env entry 'lower'"):
            self.load('["lower"]')

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

    def test_a_good_env_installs(self):
        store = Store(temp_dir("dock-iso-install-") / "home")
        root = make_plugin('container = { cmd = "python3" }', '\n[capabilities]\nenv = ["MY_VAR"]\n')
        self.assertEqual(store.install(root).env, ["MY_VAR"])


class IsolationConfig(unittest.TestCase):
    def store(self, config: str | bytes | None) -> Store:
        home = temp_dir("dock-iso-home-")
        if isinstance(config, str):
            (home / "config.toml").write_text(config)
        elif config is not None:
            (home / "config.toml").write_bytes(config)
        return Store(home)

    def test_no_isolation_table_means_none(self):
        self.assertIsNone(isolation.config(self.store(None)))
        self.assertIsNone(isolation.config(self.store("[shell]\nidle_timeout = 5\n")))

    def test_defaults_are_docker_and_disabled(self):
        cfg = isolation.config(self.store("[isolation]\n"))
        self.assertEqual(cfg, isolation.Config("docker", False, ()))
        self.assertFalse(cfg.isolates("dev.dock.hello"))

    def test_enabled_isolates_every_plugin(self):
        cfg = isolation.config(self.store('[isolation]\nruntime = "/opt/podman"\nenabled = true\n'))
        self.assertEqual(cfg.runtime, "/opt/podman")
        self.assertTrue(cfg.isolates("anything.at.all"))

    def test_plugins_list_isolates_only_those(self):
        with redirect_stderr(StringIO()):  # not installed here: reported, which is tested below
            cfg = isolation.config(self.store('[isolation]\nplugins = ["dev.dock.download"]\n'))
        self.assertTrue(cfg.isolates("dev.dock.download"))
        self.assertFalse(cfg.isolates("dev.dock.hello"))

    def test_wrong_types_are_reported(self):
        for body in ('enabled = "yes"', "runtime = 3", 'plugins = "dev.dock.hello"', "plugins = [1]"):
            with self.assertRaisesRegex(ManifestError, "isolation"):
                isolation.config(self.store(f"[isolation]\n{body}\n"))

    def test_isolation_must_be_a_table(self):
        with self.assertRaisesRegex(ManifestError, "isolation"):
            isolation.config(self.store("isolation = true\n"))

    # F1: a typo must never quietly mean "no isolation"
    def test_unknown_key_in_the_table_is_refused_naming_it(self):
        with self.assertRaisesRegex(ManifestError, "unknown key 'enabeld'"):
            isolation.config(self.store("[isolation]\nenabeld = true\n"))

    def test_every_unknown_key_is_named(self):
        with self.assertRaises(ManifestError) as cm:
            isolation.config(self.store("[isolation]\nenabled = true\nplugin = []\nruntim = 'x'\n"))
        self.assertIn("'plugin'", str(cm.exception))
        self.assertIn("'runtim'", str(cm.exception))

    def test_isolation_keys_at_the_top_level_are_refused_suggesting_the_table(self):
        for line in ("enabled = true", 'runtime = "docker"', 'plugins = ["dev.dock.hello"]'):
            with self.subTest(line=line):
                with self.assertRaisesRegex(ManifestError, r"\[isolation\]"):
                    isolation.config(self.store(f"{line}\n\n[shell]\nidle_timeout = 5\n"))

    def test_a_table_named_almost_isolation_is_refused_suggesting_the_real_name(self):
        for name in ("isolaton", "Isolation", "ISOLATION", "isolation_", "isolations", "isolatoin"):
            with self.subTest(name=name):
                with self.assertRaisesRegex(ManifestError, rf"\[{name}\].*\[isolation\]"):
                    isolation.config(self.store(f"[{name}]\nenabled = true\n"))

    def test_unrelated_tables_are_not_mistaken_for_isolation(self):
        cfg = self.store('[shell]\nidle_timeout = 5\n[providers]\nllm = "x"\n[something_else]\na = 1\n')
        self.assertIsNone(isolation.config(cfg))

    def test_other_tables_next_to_a_real_isolation_table_are_fine(self):
        cfg = isolation.config(self.store('[shell]\nidle_timeout = 5\n[isolation]\nenabled = true\n'))
        self.assertTrue(cfg.enabled)

    def test_plugins_entry_that_is_not_installed_warns_once_on_stderr(self):
        store = self.store(None)
        install(store, NATIVE_AND_CONTAINER)
        (store.home / "config.toml").write_text('[isolation]\nplugins = ["dev.dock.sample", "dev.dock.nope"]\n')
        with redirect_stderr(StringIO()) as err:
            isolation.config(store)
            isolation.config(store)
        self.assertEqual(err.getvalue().count("dev.dock.nope"), 1, err.getvalue())
        self.assertNotIn("dev.dock.sample", err.getvalue())
        self.assertIn("not an installed plugin", err.getvalue())

    # F3: a config.toml that does not parse as a whole
    def test_broken_isolation_table_is_refused(self):
        with self.assertRaises(ManifestError):
            isolation.config(self.store("[isolation\nenabled = true\n"))

    def test_broken_other_table_alone_means_none(self):
        self.assertIsNone(isolation.config(self.store("[shell\nidle_timeout = 5\n")))
        self.assertIsNone(isolation.config(self.store('[providers]\nllm = "x"\n[shell\n')))

    def test_valid_isolation_table_is_used_despite_a_broken_one_elsewhere(self):
        for text in ("[shell\n\n[isolation]\nenabled = true\n", "[isolation]\nenabled = true\n\n[shell\n"):
            with self.subTest(text=text):
                self.assertTrue(isolation.config(self.store(text)).enabled)

    def test_quoted_and_escaped_headers_are_found_too(self):
        for header in ('["isolation"]', "['isolation']", '["i\\u0073olation"]', "[ isolation ]"):
            with self.subTest(header=header):
                cfg = isolation.config(self.store(f"[shell\n\n{header}\nenabled = true\n"))
                self.assertTrue(cfg.enabled)

    def test_broken_quoted_or_escaped_isolation_header_is_refused(self):
        for header in ('["isolation"', '["i\\u0073olation"', "['isolation'", "[i", "[isol", "[Isolation"):
            with self.subTest(header=header):
                with self.assertRaises(ManifestError):
                    isolation.config(self.store(f"{header}\nenabled = true\n"))

    def test_isolation_section_with_an_error_inside_is_refused(self):
        for header in ("[isolation]", '["isolation"]'):
            with self.subTest(header=header):
                with self.assertRaises(ManifestError):
                    isolation.config(self.store(f"{header}\nenabled = tru\n\n[shell]\nidle_timeout = 5\n"))

    def test_broken_almost_isolation_header_is_refused(self):
        with self.assertRaises(ManifestError):
            isolation.config(self.store("[isolaton\nenabled = true\n"))

    def test_broken_file_with_top_level_isolation_keys_is_refused(self):
        for text in ("enabled = true\n[shell\n", "enabled = tru\n[shell]\n", "runtime = \n[shell]\n"):
            with self.subTest(text=text):
                with self.assertRaises(ManifestError):
                    isolation.config(self.store(text))

    def test_inline_isolation_table_is_found_despite_a_broken_section(self):
        self.assertTrue(isolation.config(self.store("isolation = { enabled = true }\n[shell\n")).enabled)

    # F5: bytes that are not UTF-8
    def test_store_config_reports_undecodable_and_unreadable_files_as_manifest_errors(self):
        with self.assertRaises(ManifestError):
            self.store(b"# caf\xe9\n").config()
        store = self.store(None)
        (store.home / "config.toml").mkdir()  # exists, cannot be read as a file
        with self.assertRaises(ManifestError):
            store.config()

    def test_undecodable_config_without_isolation_means_none(self):
        self.assertIsNone(isolation.config(self.store(b"# caf\xe9\n[shell]\nidle_timeout = 5\n")))

    def test_undecodable_config_with_a_valid_isolation_table_still_applies_it(self):
        cfg = isolation.config(self.store(b"# caf\xe9\n[isolation]\nenabled = true\n"))
        self.assertTrue(cfg.enabled)

    def test_undecodable_config_with_a_broken_isolation_table_is_refused(self):
        with self.assertRaises(ManifestError):
            isolation.config(self.store(b"# caf\xe9\n[isolation\nenabled = true\n"))

    def test_unreadable_config_is_refused_not_ignored(self):
        store = self.store(None)
        (store.home / "config.toml").mkdir()
        with self.assertRaises(ManifestError):
            isolation.config(store)


class RunArgv(unittest.TestCase):
    CFG = isolation.Config(runtime="docker", enabled=True)

    def manifest(self, permissions=(), container='{ cmd = "python3", args = ["/plugin/main.py"] }'):
        extra = f"\n[capabilities]\npermissions = {json.dumps(list(permissions))}\n"
        return Manifest.load(make_plugin(f"container = {container}", extra))

    def argv(self, m, env=None, cwd="/work/dir", uid_gid=(501, 20), data_dir="/home/u/.dock/data/dev.dock.sample",
             passthrough=()):
        return isolation.run_argv(self.CFG, m, name="dock-dev-dock-sample-1-0", data_dir=data_dir,
                                  cwd=cwd, uid_gid=uid_gid, env=env if env is not None else {"HOME": "/tmp"},
                                  passthrough=passthrough)

    def test_image_tag_is_id_and_version(self):
        self.assertEqual(isolation.image_tag(self.manifest()), "dock/dev.dock.sample:1.2.3")

    def test_exact_argv_without_permissions(self):
        m = self.manifest()
        env = {"LANG": "C", "HOME": "/tmp", "DOCK_PLUGIN_ID": m.id}
        self.assertEqual(self.argv(m, env), [
            "docker", "run", "-i", "--rm", "--pull", "never", "--name", "dock-dev-dock-sample-1-0",
            "--label", f"dev.dock.owner={socket.gethostname()}:{os.getpid()}",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--read-only", "--tmpfs", "/tmp",
            "--user", "501:20",
            "--network", "none",
            "-v", "/home/u/.dock/data/dev.dock.sample:/home/u/.dock/data/dev.dock.sample",
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

    def test_no_passthrough_adds_no_name_only_entries(self):
        argv = self.argv(self.manifest(), {"HOME": "/tmp"})
        values = [argv[i + 1] for i, a in enumerate(argv) if a == "-e"]
        self.assertEqual(values, ["HOME=/tmp"])

    def test_passthrough_env_lists_declared_names_that_are_set_on_the_host(self):
        m = Manifest.load(make_plugin('container = { cmd = "python3" }',
                                      '\n[capabilities]\nenv = ["MY_VAR", "UNSET_VAR", "EMPTY_VAR"]\n'))
        with mock.patch.dict(os.environ, {"MY_VAR": "v", "EMPTY_VAR": "", "OTHER": "x"}):
            os.environ.pop("UNSET_VAR", None)
            self.assertEqual(isolation.passthrough_env(m), ["MY_VAR", "EMPTY_VAR"])  # empty counts as set

    def test_passthrough_env_is_empty_when_nothing_is_declared(self):
        with mock.patch.dict(os.environ, {"OTHER": "x"}):
            self.assertEqual(isolation.passthrough_env(self.manifest()), [])

    def test_network_flag_only_without_network_permission(self):
        self.assertIn("--network", self.argv(self.manifest()))
        self.assertNotIn("--network", self.argv(self.manifest(["network"])))

    def test_cwd_is_mounted_only_with_fs_write_cwd(self):
        self.assertNotIn("/work/dir:/work/dir", self.argv(self.manifest(["network"])))
        argv = self.argv(self.manifest(["fs:write:cwd"]))
        i = argv.index("/work/dir:/work/dir")
        self.assertEqual(argv[i - 1], "-v")
        self.assertLess(argv.index("/home/u/.dock/data/dev.dock.sample:/home/u/.dock/data/dev.dock.sample"), i)

    def test_no_user_flag_without_posix_ids(self):
        self.assertNotIn("--user", self.argv(self.manifest(), uid_gid=None))

    def test_paths_with_spaces_stay_single_arguments(self):
        argv = self.argv(self.manifest(["fs:write:cwd"]), cwd="/my dir/x", data_dir="/data dir/p")
        self.assertIn("/my dir/x:/my dir/x", argv)
        self.assertIn("/data dir/p:/data dir/p", argv)

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

    def test_allowlisted_variables_unset_on_the_host_are_omitted(self):
        old = dict(os.environ)
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


class ProcessLaunch(unittest.TestCase):
    def test_a_plugin_process_cannot_be_built_without_saying_how_to_launch_it(self):
        m = Manifest.load(make_plugin('any = { cmd = "python3" }'))
        with self.assertRaises(TypeError):
            PluginProcess(m, os.devnull, {})  # no silent fallback to a native, unisolated start

    def test_start_runs_exactly_the_launch_it_was_given(self):
        m = Manifest.load(make_plugin('any = { cmd = "definitely-not-this" }'))
        launch = isolation.Launch([sys.executable, "-c", "import sys; sys.exit(0)"], dict(os.environ))
        proc = PluginProcess(m, os.devnull, {}, launch)
        proc.start()
        self.assertEqual(proc.proc.wait(timeout=30), 0)
        proc.kill()


def install(store: Store, run: str, extra: str = "", dockerfile: bool = True) -> Manifest:
    root = make_plugin(run, extra)
    if dockerfile:
        (root / "Dockerfile").write_text("FROM scratch\n")
    return store.install(root)


NATIVE_AND_CONTAINER = 'any = { cmd = "python3", args = ["main.py"] }\ncontainer = { cmd = "python3", args = ["/plugin/main.py"] }'


class FakeRuntimeCase(unittest.TestCase):
    """A throwaway DOCK_HOME whose config points at the fake runtime, which logs its calls."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dock-iso-case-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.log = self.tmp / "calls"
        self.store = Store(self.tmp / "home")
        patcher = mock.patch.dict(os.environ, {"FAKE_RUNTIME_LOG": str(self.log), "SECRET_TOKEN": "s3cret"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def configure(self, body: str = "enabled = true", runtime: str | None = None) -> None:
        runtime = runtime or str(FAKE)
        (self.store.home / "config.toml").write_text(f'[isolation]\nruntime = "{runtime}"\n{body}\n')

    def calls(self, verb: str | None = None) -> list[list[str]]:
        if not self.log.exists():
            return []
        rows = [json.loads(l) for l in self.log.read_text().splitlines()]
        return [r for r in rows if verb is None or r[0] == verb]


class Prepare(FakeRuntimeCase):
    def test_without_isolation_table_launch_is_native_and_no_runtime_is_called(self):
        m = install(self.store, NATIVE_AND_CONTAINER)
        launch = isolation.prepare(self.store, m)
        self.assertEqual(launch.argv, m.entrypoint())
        self.assertIsNone(launch.container)
        self.assertEqual(launch.env["DOCK_PLUGIN_ID"], m.id)
        self.assertEqual(launch.env["SECRET_TOKEN"], "s3cret")  # native plugins keep the host environment
        self.assertEqual(self.calls(), [])

    def test_plugin_outside_the_plugins_list_stays_native(self):
        m = install(self.store, NATIVE_AND_CONTAINER)
        self.configure('plugins = ["someone.else"]')
        with redirect_stderr(StringIO()):
            self.assertEqual(isolation.prepare(self.store, m).argv, m.entrypoint())
        self.assertEqual(self.calls(), [])

    def test_isolated_launch_runs_the_container_and_builds_the_image_first(self):
        self.configure()
        m = install(self.store, NATIVE_AND_CONTAINER)
        with redirect_stderr(StringIO()) as err:
            launch = isolation.prepare(self.store, m, cwd="/some/cwd")
        self.assertEqual(launch.argv[:2], [str(FAKE), "run"])
        self.assertEqual(launch.runtime, str(FAKE))
        self.assertEqual(launch.argv[launch.argv.index("--name") + 1], launch.container)
        self.assertEqual(launch.argv[-3:], ["dock/dev.dock.sample:1.2.3", "python3", "/plugin/main.py"])
        self.assertEqual(launch.env["SECRET_TOKEN"], "s3cret")  # the runtime client needs the host env; -e is what counts
        self.assertNotIn("SECRET_TOKEN", " ".join(launch.argv))
        self.assertIn("dock: building image dock/dev.dock.sample:1.2.3", err.getvalue())

        builds = self.calls("build")
        self.assertEqual(len(builds), 1)
        self.assertEqual(builds[0][:3], ["build", "-t", "dock/dev.dock.sample:1.2.3"])
        self.assertEqual(builds[0][3:], ["-f", str(m.root / "Dockerfile"), str(m.root)])

    def test_declared_env_is_passed_by_name_only_and_the_client_keeps_the_host_environment(self):
        self.configure()
        m = install(self.store, NATIVE_AND_CONTAINER, '\n[capabilities]\nenv = ["MY_VAR", "UNSET_VAR"]\n')
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

    def test_second_prepare_does_not_build_again(self):
        self.configure()
        m = install(self.store, NATIVE_AND_CONTAINER)
        with redirect_stderr(StringIO()):
            isolation.prepare(self.store, m)
            isolation.prepare(self.store, m)
        self.assertEqual(len(self.calls("build")), 1)

    def test_ensure_image_force_rebuilds_an_existing_image(self):
        self.configure()
        m = install(self.store, NATIVE_AND_CONTAINER)
        cfg = isolation.config(self.store)
        with redirect_stderr(StringIO()):
            isolation.ensure_image(cfg, self.store, m)
            isolation.ensure_image(cfg, self.store, m, force=True)
        self.assertEqual(len(self.calls("build")), 2)

    def test_build_output_goes_to_the_plugin_log(self):
        self.configure()
        m = install(self.store, NATIVE_AND_CONTAINER)
        with redirect_stderr(StringIO()):
            isolation.prepare(self.store, m)
        log = self.store.log_path(m.id).read_text()
        self.assertIn("fake build of dock/dev.dock.sample:1.2.3", log)
        self.assertIn("fake build log line", log)

    def test_failing_build_refuses_with_the_log_path(self):
        self.configure()
        m = install(self.store, NATIVE_AND_CONTAINER)
        with mock.patch.dict(os.environ, {"FAKE_RUNTIME_BUILD_EXIT": "1"}), redirect_stderr(StringIO()):
            with self.assertRaises(ManifestError) as cm:
                isolation.prepare(self.store, m)
        self.assertEqual(str(cm.exception), f"dev.dock.sample: image build failed (see {self.store.log_path(m.id)})")
        self.assertEqual(self.calls("run"), [])

    def test_plugin_without_container_entry_is_refused(self):
        self.configure()
        m = install(self.store, 'any = { cmd = "python3", args = ["main.py"] }')
        with self.assertRaises(ManifestError) as cm:
            isolation.prepare(self.store, m)
        self.assertEqual(str(cm.exception), (
            "dev.dock.sample: isolation is on but the plugin has no container build "
            "(add [run].container and a Dockerfile, or exclude it in [isolation])"))
        self.assertEqual(self.calls("build") + self.calls("run"), [])

    def test_plugin_with_container_entry_but_no_dockerfile_is_refused(self):
        self.configure()
        m = install(self.store, NATIVE_AND_CONTAINER, dockerfile=False)
        with self.assertRaisesRegex(ManifestError, "isolation is on but the plugin has no container build"):
            isolation.prepare(self.store, m)

    def test_missing_runtime_is_refused(self):
        self.configure(runtime="/nonexistent/runtime-xyz")
        m = install(self.store, NATIVE_AND_CONTAINER)
        with self.assertRaises(ManifestError) as cm:
            isolation.prepare(self.store, m)
        self.assertEqual(str(cm.exception), "isolation runtime '/nonexistent/runtime-xyz' not found")

    def test_runtime_found_on_path_is_used_by_its_resolved_path(self):
        self.configure(runtime="docker")
        m = install(self.store, NATIVE_AND_CONTAINER)
        with mock.patch.dict(os.environ, {"PATH": f"{FAKE.parent}{os.pathsep}{os.environ['PATH']}"}):
            with redirect_stderr(StringIO()):
                launch = isolation.prepare(self.store, m)
        self.assertEqual(launch.argv[0], str(FAKE))


class CwdMount(FakeRuntimeCase):
    """F4: with fs:write:cwd the cwd is mounted read-write, so a cwd that is too broad is refused."""

    def setUp(self):
        super().setUp()
        self.user_home = self.tmp / "user"
        (self.user_home / "project").mkdir(parents=True)
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.user_home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.configure()
        self.m = install(self.store, NATIVE_AND_CONTAINER, '\n[capabilities]\npermissions = ["fs:write:cwd"]')

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
        self.assertIn(f"{project}:{project}", launch.argv)

    def test_the_filesystem_root_is_refused(self):
        self.assertRefused("/", "filesystem root")

    def test_the_home_directory_is_refused(self):
        self.assertRefused(self.user_home, "home directory")

    def test_a_directory_containing_the_dock_home_is_refused(self):
        self.assertRefused(self.store.home.parent, "dock home")

    def test_the_dock_home_itself_and_anything_inside_it_is_refused(self):
        self.assertRefused(self.store.home, "dock home")
        self.assertRefused(self.store.home / "data" / "dev.dock.sample", "dock home")

    def test_a_symlink_to_a_refused_directory_is_refused(self):
        link = self.tmp / "innocent"
        link.symlink_to(self.user_home)
        self.assertRefused(link, "home directory")

    def test_without_fs_write_cwd_any_cwd_is_fine_because_nothing_is_mounted(self):
        m = Manifest.load(make_plugin(NATIVE_AND_CONTAINER, '\n[capabilities]\npermissions = ["network"]'))
        (m.root / "Dockerfile").write_text("FROM scratch\n")
        launch = self.prepare("/", m)
        self.assertNotIn("/:/", launch.argv)

    def test_the_command_line_refuses_with_exit_1_and_starts_nothing(self):
        self.store.install(ROOT / "plugin" / "download")
        env = {**os.environ, "DOCK_HOME": str(self.store.home), "PYTHONPATH": str(ROOT)}
        r = subprocess.run([sys.executable, "-m", "cli", "download", "http://x"], capture_output=True,
                           text=True, env=env, cwd="/", timeout=60, check=False)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("filesystem root", r.stderr)
        self.assertEqual(self.calls("run"), [])


FIXTURES = ROOT / "tests" / "fixtures"


class DockIsolated(FakeRuntimeCase):
    """Real `python3 -m cli` runs against the fake runtime."""

    PLUGINS = (ROOT / "plugin" / "hello", FIXTURES / "probe", FIXTURES / "capcaller", FIXTURES / "capprovider")

    def setUp(self):
        super().setUp()
        for path in self.PLUGINS:
            self.store.install(path)
        self.work = self.tmp / "work dir"  # a space: paths must stay single arguments
        self.work.mkdir()
        self.env = {**os.environ, "DOCK_HOME": str(self.store.home), "PYTHONPATH": str(ROOT)}

    def dock(self, *args, stdin="", env=None):
        return subprocess.run(
            [sys.executable, "-m", "cli", *args], input=stdin, capture_output=True, text=True,
            env=env or self.env, cwd=self.work, timeout=60, check=False,
        )

    def spawn(self, *args):
        p = subprocess.Popen([sys.executable, "-m", "cli", *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, env=self.env, cwd=self.work)
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
        path = self.tmp / "envprobe"
        shutil.copytree(FIXTURES / "jail", path, ignore=shutil.ignore_patterns("__pycache__"))
        manifest = (path / "plugin.toml").read_text().replace("dev.dock.jail", "dev.dock.envprobe")
        manifest = manifest.replace('env = ["DOCK_LLM_MODEL"]', 'env = ["MY_VAR", "UNSET_VAR", "EMPTY_VAR"]')
        self.assertIn("MY_VAR", manifest)
        (path / "plugin.toml").write_text(manifest)
        self.store.install(path)
        self.host_env = {k: v for k, v in self.env.items() if k != "UNSET_VAR"} | self.HOST

    def probe(self, name):
        r = self.dock("dev.dock.envprobe:env", name, env=self.host_env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def test_the_recorded_argv_has_the_name_but_never_the_value(self):
        self.configure()
        self.probe("MY_VAR")
        argv = self.run_calls()[0]
        passed = [argv[i + 1] for i, a in enumerate(argv) if a == "-e"]
        self.assertIn("MY_VAR", passed)
        self.assertIn("EMPTY_VAR", passed)  # set to the empty string on the host: still passed
        self.assertNotIn("UNSET_VAR", passed)
        text = json.dumps(argv)
        for leaked in ("hello-123", "OTHER", "nope", "UNSET_VAR"):
            self.assertNotIn(leaked, text)

    def test_a_secret_declared_variable_never_appears_in_any_runtime_call(self):
        self.configure()
        env = {**self.host_env, "MY_VAR": "secret-xyz"}
        r = self.dock("dev.dock.envprobe:env", "MY_VAR", env=env)
        self.assertEqual(r.stdout.strip(), "set:secret-xyz", r.stderr)  # the plugin inside sees it
        self.assertNotIn("secret-xyz", json.dumps(self.calls()))

    def test_the_plugin_inside_sees_the_declared_variable_and_not_the_others(self):
        self.configure()
        self.assertEqual(self.probe("MY_VAR"), "set:hello-123")
        self.assertEqual(self.probe("EMPTY_VAR"), "set:")
        self.assertEqual(self.probe("UNSET_VAR"), "unset")
        self.assertEqual(self.probe("OTHER"), "unset")

    def test_a_native_run_still_sees_the_whole_environment(self):
        self.assertEqual(self.probe("OTHER"), "set:nope")
        self.assertEqual(self.probe("MY_VAR"), "set:hello-123")
        self.assertEqual(self.calls("run"), [])


class NativeByDefault(DockIsolated):
    def test_without_isolation_table_the_runtime_is_never_called(self):
        r = self.dock("code", "3")
        self.assertEqual(r.returncode, 3, r.stderr)
        r = self.dock("hello", "Roman")
        self.assertEqual(r.stdout.strip(), "hello, Roman!")
        self.assertEqual(self.calls(), [])

    def test_isolation_for_other_plugins_leaves_the_probe_native(self):
        self.configure('plugins = ["someone.else"]')
        self.assertEqual(self.dock("code", "3").returncode, 3)
        self.assertEqual(self.calls("run") + self.calls("build"), [])  # only the startup sweep may ask

    def test_enabled_false_runs_natively(self):
        self.configure("enabled = false")
        self.assertEqual(self.dock("code", "3").returncode, 3)
        self.assertEqual(self.calls("run") + self.calls("build"), [])  # only the startup sweep may ask


class IsolatedRun(DockIsolated):
    def test_enabled_runs_the_probe_through_the_runtime(self):
        self.configure()
        r = self.dock("code", "3")
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertIn("dock: building image dock/dev.dock.probe:0.1.0", r.stderr)
        self.assertEqual(len(self.calls("build")), 1)
        self.assertEqual(len(self.run_calls()), 1)

    def test_plugins_list_isolates_only_the_listed_plugin(self):
        self.configure('plugins = ["dev.dock.probe"]')
        self.assertEqual(self.dock("code", "3").returncode, 3)
        self.assertEqual(self.dock("hello", "Roman").stdout.strip(), "hello, Roman!")
        self.assertEqual(len(self.run_calls()), 1)  # probe in a container, hello native

    def test_second_run_does_not_build_again(self):
        self.configure()
        self.dock("code", "0")
        r = self.dock("code", "0")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("building image", r.stderr)
        self.assertEqual(len(self.calls("build")), 1)
        self.assertEqual(len(self.run_calls()), 2)

    def test_failing_build_refuses_with_exit_1_and_starts_nothing(self):
        self.configure()
        env = {**self.env, "FAKE_RUNTIME_BUILD_EXIT": "1"}
        r = self.dock("code", "0", env=env)
        self.assertEqual(r.returncode, 1)
        log = self.store.log_path("dev.dock.probe")
        self.assertIn(f"dock: dev.dock.probe: image build failed (see {log})", r.stderr)
        self.assertEqual(self.run_calls(), [])

    def test_isolated_plugin_without_container_build_is_refused(self):
        self.store.install(FIXTURES / "provprobe")
        self.configure('plugins = ["dev.dock.provprobe"]')
        r = self.dock("ping")
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("dock: dev.dock.provprobe: isolation is on but the plugin has no container build "
                      "(add [run].container and a Dockerfile, or exclude it in [isolation])", r.stderr)
        self.assertEqual(self.run_calls(), [])

    def test_missing_runtime_is_refused(self):
        self.configure(runtime="no-such-runtime-xyz")
        r = self.dock("code", "0")
        self.assertEqual(r.returncode, 1)
        self.assertIn("dock: isolation runtime 'no-such-runtime-xyz' not found", r.stderr)

    def test_recorded_run_has_every_restriction_and_only_allowlisted_env(self):
        self.configure()
        env = {**self.env, "SECRET_TOKEN": "s3cret", "LANG": "C.UTF-8", "TZ": "UTC"}
        self.assertEqual(self.dock("code", "0", env=env).returncode, 0)
        argv = self.run_calls()[0]
        for flag in ("-i", "--rm", "--read-only", "--cap-drop", "--security-opt", "--user", "--label"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--network") + 1], "none")
        data_dir = str(self.store.data_root / "dev.dock.probe")
        self.assertIn(f"{data_dir}:{data_dir}", argv)
        self.assertNotIn(f"{self.work}:{self.work}", argv)  # probe declares no fs:write:cwd
        names = {argv[i + 1].split("=", 1)[0] for i, a in enumerate(argv) if a == "-e"}
        self.assertEqual(names, {"LANG", "TZ", "HOME", "DOCK_PLUGIN_ID", "DOCK_API_VERSION"})  # not PATH: the image's own
        self.assertNotIn("s3cret", " ".join(argv))

    def test_hello_one_shot_works_in_a_container(self):
        self.configure()
        r = self.dock("hello", "Roman")
        self.assertEqual(r.stdout.strip(), "hello, Roman!", r.stderr)
        self.assertEqual(len(self.run_calls()), 1)

    def test_prompt_through_the_host_works_in_a_container(self):
        self.configure()
        self.assertIn("hello, Ada!", self.dock("hello", stdin="Ada\n").stdout)

    def test_declared_inputs_reach_the_isolated_plugin(self):
        self.configure()
        r = self.dock("fetch", "http://x", "-o", "out.bin")
        self.assertEqual(r.returncode, 0, r.stderr)
        got = json.loads(r.stdout)["inputs"]
        self.assertEqual(got, {"url": "http://x", "output": "out.bin", "retries": 3, "quiet": False})

    def test_plugin_sees_only_the_allowlisted_environment(self):
        self.configure()
        env = {**self.env, "SECRET_TOKEN": "s3cret"}
        self.dock("pid", env=env)  # starts it; the probe has no env command, so look at the recorded run
        self.assertNotIn("SECRET_TOKEN", json.dumps(self.run_calls()))

    def test_shell_reuses_one_warm_container_for_two_operations(self):
        self.configure()
        r = self.dock("shell", stdin="hello a\nhello b\n")
        self.assertIn("hello, a!", r.stdout)
        self.assertIn("hello, b!", r.stdout)
        self.assertEqual(len(self.run_calls()), 1)

    def test_native_caller_uses_an_isolated_provider(self):
        self.configure('plugins = ["dev.dock.capprovider"]')
        p = self.spawn("callcap")
        out = ""
        deadline = time.monotonic() + 20
        while "tick" not in out and time.monotonic() < deadline:
            out += p.stdout.readline()
        self.assertIn("tick", out)  # events streamed provider (container) -> host -> native caller
        p.send_signal(2)
        p.communicate(timeout=30)
        self.assertEqual(p.returncode, 130)
        self.assertEqual(len(self.run_calls()), 1)
        self.assertIn("dev-dock-capprovider", self.container_names()[0])


def wait_until(predicate, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


class ContainerCleanup(DockIsolated):
    """R8/NFR4: whatever stops a plugin, its container is removed with `rm -f <name>`."""

    def assertAllRemoved(self):
        names = self.container_names()
        self.assertTrue(names, "no container was started")
        self.assertEqual(sorted(self.removed()), sorted(names))
        state = json.loads(Path(str(self.log) + ".state").read_text())
        self.assertEqual(state["running"], {})  # nothing is left in the runtime

    def test_one_shot_exit_removes_the_container(self):
        self.configure()
        self.assertEqual(self.dock("code", "0").returncode, 0)
        self.assertAllRemoved()

    def test_every_container_of_a_session_is_removed_at_exit(self):
        self.configure()
        r = self.dock("shell", stdin="hello a\ncode 0\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.container_names()), 2)
        self.assertAllRemoved()

    def test_crashed_plugin_container_is_removed(self):
        self.configure()
        self.assertEqual(self.dock("crash").returncode, 1)
        self.assertAllRemoved()

    def test_failed_initialize_removes_the_container(self):
        self.configure()
        m = self.store.get("dev.dock.probe")
        (m.root / "main.py").write_text("import sys\nsys.exit(3)\n")
        r = self.dock("code", "0")
        self.assertEqual(r.returncode, 1)
        self.assertIn("exited during 'initialize'", r.stderr)
        self.assertAllRemoved()

    def test_idle_reap_removes_the_container_while_the_shell_keeps_running(self):
        self.configure()
        with open(self.store.home / "config.toml", "a") as f:
            f.write("[shell]\nidle_timeout = 1\n")
        p = self.spawn("shell")
        p.stdin.write("code 0\n")
        p.stdin.flush()
        self.assertTrue(wait_until(lambda: len(self.run_calls()) == 1))
        time.sleep(1.5)
        p.stdin.write("\n")  # a line at the prompt reaps what has been idle too long
        p.stdin.flush()
        self.assertTrue(wait_until(lambda: self.removed() != []), "idle plugin's container was not removed")
        self.assertIsNone(p.poll())  # the shell is still up
        p.communicate("", timeout=30)
        self.assertAllRemoved()

    def test_force_kill_after_cancel_grace_removes_the_container(self):
        self.configure()
        self.env["DOCK_CANCEL_GRACE"] = "1"
        p = self.spawn("shell")
        p.stdin.write("stubborn 30\n")
        p.stdin.flush()
        self.assertTrue(wait_until(lambda: len(self.run_calls()) == 1))
        pid_file = self.store.data_root / "dev.dock.probe" / "pid"
        self.assertTrue(wait_until(pid_file.exists))
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

    def seed(self, **running):
        state = Path(str(self.log) + ".state")
        state.write_text(json.dumps({"images": {}, "running": running}))

    def with_fake_as_docker(self) -> dict:
        """An environment whose PATH has the fake runtime as `docker` (what an unconfigured dock would find)."""
        bindir = self.tmp / "bin"
        bindir.mkdir(exist_ok=True)
        if not (bindir / "docker").exists():
            (bindir / "docker").symlink_to(FAKE)
        return {**self.env, "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"}

    def test_sweep_removes_only_containers_of_dead_owners(self):
        self.configure()
        self.seed(stale=owner(dead_pid()), mine=owner(os.getpid()))
        self.assertEqual(isolation.sweep(self.store), ["stale"])
        self.assertEqual(self.removed(), ["stale"])
        ps = self.calls("ps")[0]
        self.assertEqual(ps[:5], ["ps", "-a", "--filter", "label=dev.dock.owner", "--format"])
        self.assertEqual(ps[5], '{{.Names}} {{.Label "dev.dock.owner"}}')

    def test_sweep_leaves_containers_of_another_host_alone(self):
        self.configure()
        self.seed(elsewhere=owner(dead_pid(), host="some-other-host"))  # its pid means nothing here
        self.assertEqual(isolation.sweep(self.store), [])
        self.assertEqual(self.removed(), [])

    def test_sweep_leaves_the_old_pid_only_label_alone(self):
        self.configure()
        self.seed(old=str(dead_pid()))  # not host:pid: whoever wrote it, we cannot judge it
        self.assertEqual(isolation.sweep(self.store), [])
        self.assertEqual(self.removed(), [])

    def test_sweep_ignores_unparsable_labels(self):
        self.configure()
        self.seed(odd="not-a-pid", odd2=f"{HOST}:abc", odd3=f"{HOST}:", odd4=":12",
                  huge=f"{HOST}:{'9' * 30}", zero=f"{HOST}:0")  # os.kill would raise or signal a process group
        self.assertEqual(isolation.sweep(self.store), [])
        self.assertEqual(self.removed(), [])

    def test_sweep_is_skipped_on_windows(self):
        self.configure()
        self.seed(stale=owner(dead_pid()))
        with mock.patch.object(isolation.sys, "platform", "win32"):
            self.assertEqual(isolation.sweep(self.store), [])
        self.assertEqual(self.calls(), [])

    def test_sweep_without_isolation_table_and_no_docker_calls_nothing(self):
        self.seed(stale=owner(dead_pid()))
        with mock.patch.dict(os.environ, {"PATH": str(self.tmp)}):  # no `docker` anywhere
            self.assertEqual(isolation.sweep(self.store), [])
        self.assertEqual(self.calls(), [])

    def test_sweep_without_isolation_table_uses_docker_when_it_is_on_the_path(self):
        # The user may have dropped [isolation] after a crash: dock's own leftovers should still go.
        self.seed(stale=owner(dead_pid()), live=owner(os.getpid()))
        with mock.patch.dict(os.environ, {"PATH": self.with_fake_as_docker()["PATH"]}):
            self.assertEqual(isolation.sweep(self.store), ["stale"])
        self.assertEqual(self.removed(), ["stale"])

    def test_sweep_is_capped_in_time_when_the_runtime_hangs(self):
        self.configure()
        self.seed(stale=owner(dead_pid()))
        started = time.monotonic()
        with mock.patch.object(isolation, "SWEEP_TIMEOUT", 0.5), \
                mock.patch.dict(os.environ, {"FAKE_RUNTIME_PS_SLEEP": "5"}):
            self.assertEqual(isolation.sweep(self.store), [])
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(self.removed(), [])

    def test_sweep_with_missing_runtime_is_silent(self):
        self.configure(runtime="no-such-runtime-xyz")
        self.assertEqual(isolation.sweep(self.store), [])

    def test_one_shot_start_removes_a_stale_container_and_keeps_a_live_one(self):
        self.configure()
        self.seed(stale=owner(dead_pid()), live=owner(os.getpid()))
        self.assertEqual(self.dock("code", "0").returncode, 0)
        removed = self.removed()
        self.assertIn("stale", removed)
        self.assertNotIn("live", removed)

    def test_shell_start_removes_a_stale_container(self):
        self.configure()
        self.seed(stale=owner(dead_pid()))
        self.assertEqual(self.dock("shell", stdin="").returncode, 0)
        self.assertEqual(self.removed(), ["stale"])

    def test_no_isolation_table_and_no_docker_means_no_runtime_call_at_start(self):
        self.seed(stale=owner(dead_pid()))
        env = {**self.env, "PATH": str(Path(sys.executable).parent)}  # python, but no `docker`
        self.dock("code", "0", env=env)
        self.dock("shell", stdin="", env=env)
        self.assertEqual(self.calls(), [])


class BrokenConfig(DockIsolated):
    """A config.toml that does not parse (or decode) as a whole: isolation is never quietly dropped."""

    def write(self, text: str | bytes) -> None:
        path = self.store.home / "config.toml"
        path.write_bytes(text if isinstance(text, bytes) else text.encode())

    def isolated_section(self, header: str = "[isolation]", body: str = "enabled = true") -> str:
        return f'{header}\nruntime = "{FAKE}"\n{body}\n'

    def assertRefusedWithoutStarting(self, r):
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertNotIn("hello,", r.stdout)
        self.assertIn("config.toml", r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertFalse(self.store.log_path("dev.dock.hello").exists(), "a plugin process was started")
        self.assertEqual(self.run_calls(), [])

    def test_broken_isolation_table_refuses_to_start_the_plugin(self):
        self.write(f'[isolation\nruntime = "{FAKE}"\nenabled = true\n')
        self.assertRefusedWithoutStarting(self.dock("hello", "Roman"))

    def test_broken_isolation_table_refuses_in_the_shell_too_without_starting(self):
        self.write(f'[isolation\nruntime = "{FAKE}"\nenabled = true\n')
        r = self.dock("shell", stdin="hello a\nstatus\n")
        self.assertNotIn("hello, a!", r.stdout)
        self.assertFalse(self.store.log_path("dev.dock.hello").exists())

    def test_broken_other_section_does_not_stop_a_valid_isolation_table_from_applying(self):
        self.write("[shell\n\n" + self.isolated_section())
        r = self.dock("hello", "Roman")
        self.assertEqual(r.stdout.strip(), "hello, Roman!", r.stderr)
        self.assertEqual(len(self.run_calls()), 1)  # in a container, not natively

    def test_quoted_isolation_header_with_an_error_elsewhere_still_applies(self):
        self.write('[shell\n\n' + self.isolated_section('["isolation"]'))
        r = self.dock("hello", "Roman")
        self.assertEqual(r.stdout.strip(), "hello, Roman!", r.stderr)
        self.assertEqual(len(self.run_calls()), 1)

    def test_quoted_isolation_header_with_an_error_inside_refuses(self):
        self.write(self.isolated_section('["isolation"]', "enabled = tru"))
        self.assertRefusedWithoutStarting(self.dock("hello", "Roman"))

    def test_broken_file_without_isolation_still_runs_plugins_natively(self):
        self.write("[shell\n")
        r = self.dock("hello", "Roman")
        self.assertEqual(r.stdout.strip(), "hello, Roman!", r.stderr)
        self.assertEqual(self.run_calls(), [])

    def test_misspelt_isolation_table_refuses_instead_of_running_natively(self):
        self.write(self.isolated_section("[isolaton]"))
        r = self.dock("hello", "Roman")
        self.assertRefusedWithoutStarting(r)
        self.assertIn("[isolation]", r.stderr)

    def test_unknown_key_refuses_naming_it(self):
        self.write(self.isolated_section(body="enabeld = true"))
        r = self.dock("hello", "Roman")
        self.assertRefusedWithoutStarting(r)
        self.assertIn("enabeld", r.stderr)

    def test_non_utf8_config_gives_no_traceback_and_runs_natively_without_isolation_settings(self):
        self.write(b"# caf\xe9\n[providers]\nllm = 'x'\n")
        r = self.dock("hello", "Roman")
        self.assertEqual(r.stdout.strip(), "hello, Roman!", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_non_utf8_config_with_isolation_table_still_isolates(self):
        self.write(b"# caf\xe9\n" + self.isolated_section().encode())
        r = self.dock("hello", "Roman")
        self.assertEqual(r.stdout.strip(), "hello, Roman!", r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertEqual(len(self.run_calls()), 1)

    def test_non_utf8_config_with_broken_isolation_refuses_without_traceback(self):
        self.write(b"# caf\xe9\n[isolation\nenabled = true\n")
        self.assertRefusedWithoutStarting(self.dock("hello", "Roman"))

    def test_non_utf8_config_in_the_shell_survives_without_traceback(self):
        self.write(b"# caf\xe9\n")
        r = self.dock("shell", stdin="hello a\nhello b\n")
        self.assertIn("hello, a!", r.stdout)
        self.assertIn("hello, b!", r.stdout)
        self.assertNotIn("Traceback", r.stderr)

    def test_unreadable_config_refuses_without_traceback(self):
        (self.store.home / "config.toml").mkdir()
        r = self.dock("hello", "Roman")
        self.assertRefusedWithoutStarting(r)


class RecoverIsolated(DockIsolated):
    def test_recover_cleans_up_a_left_provisioned_plugin_in_a_container(self):
        self.configure('plugins = ["dev.dock.capprovider"]')
        marker = self.store.marker("dev.dock.capprovider")
        marker.write_text("{}")
        r = self.dock("code", "0")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("left provisioned", r.stderr)
        self.assertFalse(marker.exists())
        names = self.container_names()
        self.assertEqual(len(names), 1)
        self.assertEqual(self.removed(), names)
        events = (self.store.data_root / "dev.dock.capprovider" / "events").read_text()
        self.assertIn("unprovision", events)

    def test_recover_with_unbuildable_plugin_keeps_the_marker_and_reports(self):
        self.configure('plugins = ["dev.dock.capprovider"]')
        marker = self.store.marker("dev.dock.capprovider")
        marker.write_text("{}")
        r = self.dock("code", "0", env={**self.env, "FAKE_RUNTIME_BUILD_EXIT": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)  # the probe itself runs natively
        self.assertIn("cleanup of dev.dock.capprovider failed", r.stderr)
        self.assertTrue(marker.exists())


class FakeRuntimeSelfTest(unittest.TestCase):
    """The fixture itself: build -> inspect -> run -> ps -> rm."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dock-fake-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env = {**os.environ, "FAKE_RUNTIME_LOG": str(self.tmp / "calls")}
        self.ctx = self.tmp / "ctx"
        self.ctx.mkdir()
        (self.ctx / "hi.sh").write_text("#!/bin/sh\necho \"$GREETING from $(pwd)\"\n")
        (self.ctx / "hi.sh").chmod(0o755)

    def call(self, *args):
        return subprocess.run([str(FAKE), *args], capture_output=True, text=True, env=self.env)

    def calls(self):
        return [json.loads(l) for l in (self.tmp / "calls").read_text().splitlines()]

    def test_build_inspect_run_ps_rm(self):
        self.assertNotEqual(self.call("image", "inspect", "t:1").returncode, 0)
        self.assertEqual(self.call("build", "-t", "t:1", "-f", str(self.ctx / "Dockerfile"), str(self.ctx)).returncode, 0)
        self.assertEqual(self.call("image", "inspect", "t:1").returncode, 0)

        r = self.call("run", "-i", "--rm", "--name", "c1", "--label", "dev.dock.owner=42",
                      "-e", "GREETING=hey", "t:1", "/plugin/hi.sh")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), f"hey from {self.ctx.resolve()}")

        self.assertEqual(self.call("ps", "-a").stdout.strip(), "c1 42")
        self.assertEqual(self.call("rm", "-f", "c1").returncode, 0)
        self.assertEqual(self.call("ps", "-a").stdout.strip(), "")
        self.assertEqual([c[0] for c in self.calls()],
                         ["image", "build", "image", "run", "ps", "rm", "ps"])

    def test_run_gives_the_command_only_the_dash_e_variables(self):
        self.env["SECRET_TOKEN"] = "s3cret"
        self.call("build", "-t", "t:1", "-f", "x", str(self.ctx))
        r = self.call("run", "--name", "c", "--label", "dev.dock.owner=1", "t:1", "/usr/bin/env")
        self.assertNotIn("SECRET_TOKEN", r.stdout)

    def test_a_dash_e_name_without_a_value_is_read_from_the_runtime_clients_environment(self):
        self.env.update({"PASSED": "from-host", "NOT_PASSED": "nope"})
        self.call("build", "-t", "t:1", "-f", "x", str(self.ctx))
        r = self.call("run", "--name", "c", "--label", "dev.dock.owner=1", "-e", "PASSED", "-e", "UNSET_ONE",
                      "-e", "EMPTY=", "t:1", "/usr/bin/env")
        self.assertEqual(r.returncode, 0, r.stderr)
        got = dict(l.split("=", 1) for l in r.stdout.splitlines())
        self.assertEqual(got["PASSED"], "from-host")
        self.assertEqual(got["EMPTY"], "")
        self.assertNotIn("UNSET_ONE", got)  # like Docker: a name that is unset is not passed
        self.assertNotIn("NOT_PASSED", got)

    def test_failing_build_exit_builds_no_image(self):
        self.env["FAKE_RUNTIME_BUILD_EXIT"] = "1"
        self.assertEqual(self.call("build", "-t", "t:1", "-f", "x", str(self.ctx)).returncode, 1)
        self.assertNotEqual(self.call("image", "inspect", "t:1").returncode, 0)


class PluginBuild(DockIsolated):
    """R11: `dock plugin build <id>` (re)builds the image, isolation on or off."""

    def test_builds_the_image_and_reports_success_without_any_isolation_config(self):
        # No config.toml: the runtime defaults to `docker`, so point PATH at a dir with the fake as `docker`.
        bindir = self.tmp / "bin"
        bindir.mkdir()
        (bindir / "docker").symlink_to(FAKE)
        env = {**self.env, "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"}
        r = self.dock("plugin", "build", "dev.dock.hello", env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("built dock/dev.dock.hello:0.1.0", r.stdout)
        builds = self.calls("build")
        self.assertEqual(len(builds), 1)
        self.assertEqual(builds[0][builds[0].index("-t") + 1], "dock/dev.dock.hello:0.1.0")
        self.assertEqual(builds[0][-1], str(self.store.get("dev.dock.hello").root))
        self.assertEqual(self.run_calls(), [])  # building starts nothing

    def test_rebuilds_even_when_the_image_exists(self):
        self.configure()
        self.assertEqual(self.dock("hello", "a").returncode, 0)  # first use builds it
        self.assertEqual(len(self.calls("build")), 1)
        r = self.dock("plugin", "build", "dev.dock.hello")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.calls("build")), 2)
        self.assertEqual(self.calls("image"), [["image", "inspect", "dock/dev.dock.hello:0.1.0"]])  # not asked again

    def test_failing_build_prints_the_build_failed_message_and_exits_1(self):
        self.configure()
        r = self.dock("plugin", "build", "dev.dock.hello", env={**self.env, "FAKE_RUNTIME_BUILD_EXIT": "1"})
        self.assertEqual(r.returncode, 1)
        log = self.store.log_path("dev.dock.hello")
        self.assertIn(f"dock: dev.dock.hello: image build failed (see {log})", r.stderr)
        self.assertNotIn("built dock/", r.stdout)

    def test_plugin_without_a_container_build_is_refused(self):
        self.store.install(FIXTURES / "provprobe")
        self.configure()
        r = self.dock("plugin", "build", "dev.dock.provprobe")
        self.assertEqual(r.returncode, 1)
        self.assertIn("dock: dev.dock.provprobe: the plugin has no container build "
                      "(add [run].container and a Dockerfile)", r.stderr)
        self.assertEqual(self.calls("build"), [])

    def test_missing_runtime_is_refused(self):
        self.configure(runtime="no-such-runtime-xyz")
        r = self.dock("plugin", "build", "dev.dock.hello")
        self.assertEqual(r.returncode, 1)
        self.assertIn("dock: isolation runtime 'no-such-runtime-xyz' not found", r.stderr)

    def test_unknown_plugin_is_reported(self):
        r = self.dock("plugin", "build", "dev.dock.nope")
        self.assertEqual(r.returncode, 1)
        self.assertIn("dev.dock.nope is not installed", r.stderr)

    def test_build_needs_exactly_one_id(self):
        r = self.dock("plugin", "build")
        self.assertEqual(r.returncode, 2)
        self.assertIn("plugin build <id>", r.stderr)

    def test_works_from_the_shell_and_the_help_lists_it(self):
        self.configure()
        r = self.dock("shell", stdin="plugin build dev.dock.hello\nhelp\n")
        self.assertIn("built dock/dev.dock.hello:0.1.0", r.stdout)
        self.assertEqual(len(self.calls("build")), 1)
        self.assertEqual(r.stdout.count("plugin build <id>"), 1)  # in the shell's help
        self.assertIn("plugin build <id>", self.dock("help").stdout)


class PluginInfoIsolation(DockIsolated):
    """R12: the `isolation:` line of `plugin info`."""

    def isolation_line(self, plugin_id):
        out = self.dock("plugin", "info", plugin_id).stdout
        return next(l for l in out.splitlines() if l.startswith("isolation:"))

    def test_off_without_an_isolation_table(self):
        self.assertEqual(self.isolation_line("dev.dock.hello"), "isolation:   off")
        self.assertEqual(self.calls(), [])

    def test_off_for_a_plugin_outside_the_plugins_list(self):
        self.configure('plugins = ["dev.dock.probe"]')
        self.assertEqual(self.isolation_line("dev.dock.hello"), "isolation:   off")

    def test_runtime_without_network_and_cwd_access(self):
        self.configure()
        self.assertEqual(self.isolation_line("dev.dock.hello"), "isolation:   docker (no network, no cwd access)")
        self.assertEqual(self.calls(), [])  # info asks the runtime nothing

    def test_runtime_with_network_and_cwd_access(self):
        self.store.install(ROOT / "plugin" / "download")
        self.configure()
        self.assertEqual(self.isolation_line("dev.dock.download"), "isolation:   docker (network, cwd)")

    def test_network_without_cwd(self):
        install(self.store, NATIVE_AND_CONTAINER, '\n[capabilities]\npermissions = ["network"]')
        self.configure()
        self.assertEqual(self.isolation_line("dev.dock.sample"), "isolation:   docker (network, no cwd access)")

    def test_on_but_not_supported_without_a_container_build(self):
        self.store.install(FIXTURES / "provprobe")
        self.configure()
        self.assertEqual(self.isolation_line("dev.dock.provprobe"), "isolation:   on, but not supported by this plugin")

    def test_on_but_not_supported_when_the_dockerfile_is_missing(self):
        install(self.store, NATIVE_AND_CONTAINER, dockerfile=False)
        self.configure()
        self.assertEqual(self.isolation_line("dev.dock.sample"), "isolation:   on, but not supported by this plugin")

    def test_broken_config_does_not_break_info(self):
        (self.store.home / "config.toml").write_text("[isolation\n")
        self.assertIn("isolation:   unknown", self.isolation_line("dev.dock.hello"))


class PluginInfoEnv(DockIsolated):
    """R9: the `env:` line of `plugin info`, and the install summary."""

    def line(self, plugin_id, key="env:"):
        out = self.dock("plugin", "info", plugin_id).stdout
        return next(l for l in out.splitlines() if l.startswith(key))

    def test_dash_without_declared_env(self):
        self.assertEqual(self.line("dev.dock.hello"), "env:         -")

    def test_declared_names_are_listed_after_the_permissions(self):
        install(self.store, NATIVE_AND_CONTAINER, '\n[capabilities]\npermissions = ["network"]\nenv = ["A_ONE", "B_TWO"]\n')
        self.assertEqual(self.line("dev.dock.sample"), "env:         A_ONE, B_TWO")
        lines = self.dock("plugin", "info", "dev.dock.sample").stdout.splitlines()
        keys = [l.split(":")[0] for l in lines]
        self.assertEqual(keys.index("env"), keys.index("permissions") + 1)

    def test_the_bundled_llm_declares_its_backend_variables(self):
        self.store.install(ROOT / "plugin" / "llm")
        self.assertEqual(self.line("dev.dock.llm"), "env:         DOCK_LLM_BASE_URL, DOCK_LLM_MODEL, DOCK_LLM_API_KEY")
        self.assertEqual(self.line("dev.dock.llm", "permissions:"), "permissions: network")

    def test_the_bundled_llm_can_run_isolated(self):
        self.store.install(ROOT / "plugin" / "llm")
        self.configure()
        self.assertEqual(self.line("dev.dock.llm", "isolation:"), "isolation:   docker (network, no cwd access)")

    def test_install_summary_lists_env_only_when_declared(self):
        root = make_plugin(NATIVE_AND_CONTAINER, '\n[capabilities]\nenv = ["A_ONE", "B_TWO"]\n')
        (root / "Dockerfile").write_text("FROM scratch\n")
        out = self.dock("plugin", "install", str(root)).stdout
        self.assertIn("  env:         A_ONE, B_TWO", out.splitlines())
        out = self.dock("plugin", "install", str(ROOT / "plugin" / "hello")).stdout
        self.assertNotIn("env:", out)

    def test_a_bad_env_fails_the_install_with_exit_1_naming_the_entry(self):
        root = make_plugin(NATIVE_AND_CONTAINER, '\n[capabilities]\nenv = ["lower"]\n')
        r = self.dock("plugin", "install", str(root))
        self.assertEqual(r.returncode, 1)
        self.assertIn("env entry 'lower'", r.stderr)


class ShellStatusIsolation(DockIsolated):
    """R12: shell `status` marks isolated plugins."""

    @staticmethod
    def status_line(stdout, plugin_id):
        lines = (l.replace("dock> ", "") for l in stdout.splitlines())  # prompts precede the first output
        return next(l for l in lines if l.startswith(plugin_id) and "running" in l)

    def test_status_shows_the_runtime_for_an_isolated_plugin(self):
        self.configure('plugins = ["dev.dock.hello"]')
        r = self.dock("shell", stdin="hello a\nstatus\n")
        line = self.status_line(r.stdout, "dev.dock.hello")
        self.assertTrue(line.endswith("isolated: docker"), line)

    def test_status_has_no_suffix_for_a_native_plugin(self):
        self.configure('plugins = ["dev.dock.hello"]')
        r = self.dock("shell", stdin="code 0\nstatus\n")
        line = self.status_line(r.stdout, "dev.dock.probe")
        self.assertNotIn("isolated", line)

    def test_status_without_isolation_has_no_suffix(self):
        r = self.dock("shell", stdin="hello a\nstatus\n")
        self.assertIn("running", r.stdout)
        self.assertNotIn("isolated", r.stdout)


class ReinstallDropsTheImage(FakeRuntimeCase):
    """F6: an image is named by id and version, so a reinstall of the same version must not reuse a stale one."""

    TAG = "dock/dev.dock.sample:1.2.3"

    def image_rms(self):
        return [c for c in self.calls("image") if c[1] == "rm"]

    def build_image(self, m):
        with redirect_stderr(StringIO()):
            isolation.ensure_image(isolation.config(self.store), self.store, m)

    def test_reinstalling_removes_the_old_image_through_the_configured_runtime(self):
        self.configure()
        m = install(self.store, NATIVE_AND_CONTAINER)
        self.build_image(m)
        self.assertEqual(len(self.calls("build")), 1)
        install(self.store, NATIVE_AND_CONTAINER)
        self.assertIn(["image", "rm", "-f", self.TAG], self.image_rms())
        state = json.loads(Path(str(self.log) + ".state").read_text())
        self.assertNotIn(self.TAG, state["images"])

    def test_the_next_run_rebuilds_the_image(self):
        self.configure()
        m = install(self.store, NATIVE_AND_CONTAINER)
        self.build_image(m)
        m = install(self.store, NATIVE_AND_CONTAINER)
        with redirect_stderr(StringIO()):
            isolation.prepare(self.store, m)
        self.assertEqual(len(self.calls("build")), 2)

    def test_without_an_isolation_table_no_runtime_is_called(self):
        bindir = self.tmp / "bin"
        bindir.mkdir()
        (bindir / "docker").symlink_to(FAKE)  # a docker that is there, but nobody asked for isolation
        with mock.patch.dict(os.environ, {"PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"}):
            install(self.store, NATIVE_AND_CONTAINER)
        self.assertEqual(self.calls(), [])

    def test_a_plugin_without_a_container_entry_is_left_alone(self):
        self.configure()
        install(self.store, 'any = { cmd = "python3", args = ["main.py"] }')
        self.assertEqual(self.calls(), [])

    def test_a_missing_runtime_does_not_fail_the_install(self):
        self.configure(runtime="/nonexistent/runtime-xyz")
        m = install(self.store, NATIVE_AND_CONTAINER)
        self.assertIn(m.id, self.store.index())

    def test_a_failing_image_rm_does_not_fail_the_install(self):
        self.configure()  # nothing was built: the fake answers `No such image` with exit 1
        m = install(self.store, NATIVE_AND_CONTAINER)
        self.assertEqual(self.image_rms(), [["image", "rm", "-f", self.TAG]])
        self.assertIn(m.id, self.store.index())

    def test_a_broken_config_does_not_fail_the_install(self):
        (self.store.home / "config.toml").write_text("[isolation\n")
        m = install(self.store, NATIVE_AND_CONTAINER)
        self.assertIn(m.id, self.store.index())
        self.assertEqual(self.calls(), [])

    def test_a_hanging_runtime_does_not_hold_the_install_up_forever(self):
        self.configure()
        with mock.patch.object(isolation, "REMOVE_TIMEOUT", 0.5), \
                mock.patch.dict(os.environ, {"FAKE_RUNTIME_IMAGE_SLEEP": "5"}):
            started = time.monotonic()
            install(self.store, NATIVE_AND_CONTAINER)
        self.assertLess(time.monotonic() - started, 4)


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
        m = self.store.install(self.plugin(NATIVE_AND_CONTAINER))
        names = sorted(p.name for p in m.root.iterdir())
        self.assertEqual(names, ["Dockerfile", "go.mod", "lib.go", "main.go", "plugin.toml"])

    def test_plugin_without_container_keeps_todays_exclusions(self):
        m = self.store.install(self.plugin('any = { cmd = "python3", args = ["main.py"] }'))
        names = sorted(p.name for p in m.root.iterdir())
        self.assertEqual(names, ["Dockerfile", "plugin.toml"])

    def test_container_only_manifest_installs_without_a_native_build(self):
        m = self.store.install(self.plugin('container = { cmd = "/plugin/x" }'))
        self.assertEqual(m.container.cmd, "/plugin/x")
        self.assertIn(m.id, self.store.index())

    def test_native_only_manifest_without_a_build_for_this_machine_still_fails(self):
        with self.assertRaisesRegex(ManifestError, "no build for"):
            self.store.install(self.plugin('someos-nocpu = { cmd = "x" }'))

    def test_bundled_container_plugins_keep_native_builds_out_of_the_image_context(self):
        for name in ("hello", "download", "llm", "chat"):
            with self.subTest(plugin=name):
                m = self.store.install(ROOT / "plugin" / name)
                lines = (m.root / ".dockerignore").read_text().splitlines()
                self.assertIn("bin/", lines)

    def test_bundled_download_plugin_installs_with_its_sources(self):
        m = self.store.install(ROOT / "plugin" / "download")
        names = {p.name for p in m.root.iterdir()}
        self.assertTrue({"main.go", "go.mod", "Dockerfile", "plugin.toml"} <= names, names)
        self.assertIsNotNone(m.container)


if __name__ == "__main__":
    unittest.main()
