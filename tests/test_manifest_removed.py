"""Manifests and config of removed features fail loudly; they are never half-honoured. No runtime needed."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cli.manifest import Manifest, ManifestError  # noqa: E402
from cli.store import Store  # noqa: E402

BASE = """\
id = "dev.dock.sample"
version = "1.2.3"
api_version = "1"
description = "x"

[run]
{run}

[[commands]]
name = "go"
"""


def make_plugin(run: str, extra: str = "") -> Path:
    root = Path(tempfile.mkdtemp(prefix="dock-removed-"))
    (root / "plugin.toml").write_text(BASE.format(run=run) + extra)
    (root / "Dockerfile").write_text("FROM scratch\n")
    return root


CONTAINER = 'container = { cmd = "python3", args = ["/plugin/main.py"] }'
NATIVE = 'any = { cmd = "python3", args = ["main.py"] }'
BOTH = NATIVE + "\n" + CONTAINER
ONLY_CONTAINER = "dock runs plugins only in a container ([run].container)"


class RunEntries(unittest.TestCase):
    """R8, R9 (AC9): `[run]` is exactly `{ container = ... }`; anything else is named and refused."""

    def error(self, run: str) -> str:
        with self.assertRaises(ManifestError) as cm:
            Manifest.load(make_plugin(run))
        return str(cm.exception)

    def test_a_native_any_entry_fails_naming_the_key(self):
        self.assertEqual(self.error(NATIVE), f"dev.dock.sample: [run].any: native plugins are no longer supported; {ONLY_CONTAINER}")

    def test_a_platform_entry_fails_naming_the_key(self):
        for key in ("darwin-arm64", "linux-x86_64", "windows-x86_64"):
            with self.subTest(key=key):
                self.assertEqual(self.error(f'{key} = {{ cmd = "x" }}'),
                                 f"dev.dock.sample: [run].{key}: native plugins are no longer supported; {ONLY_CONTAINER}")

    def test_an_entry_of_any_other_name_fails_naming_the_key(self):
        self.assertEqual(self.error('whatever = { cmd = "x" }'),
                         f"dev.dock.sample: [run].whatever: native plugins are no longer supported; {ONLY_CONTAINER}")

    def test_a_wasm_entry_fails_naming_the_key(self):
        self.assertEqual(self.error('wasm = { module = "plugin.wasm" }'),
                         f"dev.dock.sample: [run].wasm: WASM plugins are no longer supported; {ONLY_CONTAINER}")

    def test_any_next_to_container_fails_on_any_it_is_not_quietly_accepted(self):
        self.assertEqual(self.error(BOTH), f"dev.dock.sample: [run].any: native plugins are no longer supported; {ONLY_CONTAINER}")
        self.assertEqual(self.error(CONTAINER + '\nany = { cmd = "python3" }'),
                         f"dev.dock.sample: [run].any: native plugins are no longer supported; {ONLY_CONTAINER}")

    def test_a_wasm_entry_next_to_container_still_fails_as_wasm(self):
        self.assertEqual(self.error(BOTH + '\nwasm = { module = "plugin.wasm" }'),
                         f"dev.dock.sample: [run].wasm: WASM plugins are no longer supported; {ONLY_CONTAINER}")

    def test_a_manifest_without_a_container_entry_fails(self):
        self.assertEqual(self.error(""), "dev.dock.sample: [run].container is required: dock runs plugins only in a container")

    def test_run_must_be_a_table(self):
        root = make_plugin(CONTAINER)
        text = (root / "plugin.toml").read_text().replace("[run]\n" + CONTAINER, 'run = "x"')
        (root / "plugin.toml").write_text(text)
        with self.assertRaisesRegex(ManifestError, r"\[run\] must be a table"):
            Manifest.load(root)

    def test_a_good_container_entry_loads(self):
        m = Manifest.load(make_plugin(CONTAINER))
        self.assertEqual(m.container.cmd, "python3")

    def test_each_refused_install_copies_nothing_into_the_store(self):
        for label, run in (("any", NATIVE), ("darwin-arm64", 'darwin-arm64 = { cmd = "x" }'),
                           ("wasm", 'wasm = { module = "plugin.wasm" }'), ("none", ""), ("both", BOTH)):
            with self.subTest(label=label):
                store = Store(Path(tempfile.mkdtemp(prefix="dock-removed-home-")))
                with self.assertRaises(ManifestError):
                    store.install(make_plugin(run))
                self.assertEqual(list(store.plugin_dir.iterdir()), [])
                self.assertEqual(store.index(), {})

    def test_the_id_and_version_must_fit_an_image_tag_for_every_plugin(self):
        root = make_plugin(CONTAINER)
        (root / "plugin.toml").write_text((root / "plugin.toml").read_text().replace('version = "1.2.3"', 'version = "1.0+build"'))
        with self.assertRaisesRegex(ManifestError, r"version '1\.0\+build' cannot be used with \[run\]\.container"):
            Manifest.load(root)

    def test_nothing_of_the_native_runner_is_left_on_a_manifest(self):
        m = Manifest.load(make_plugin(CONTAINER))
        self.assertFalse(hasattr(m, "entrypoint"))


class OldInstalls(unittest.TestCase):
    """An install from before the change has no `[run].container`: it fails at start, but can still be listed and removed."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="dock-old-home-"))
        self.addCleanup(shutil.rmtree, self.home, True)
        self.store = Store(self.home)
        self.store.install(make_plugin(CONTAINER))
        installed = self.store.plugin_dir / "dev.dock.sample" / "1.2.3" / "plugin.toml"
        installed.write_text(installed.read_text().replace(CONTAINER, NATIVE))  # what an old dock left behind
        self.env = {**os.environ, "DOCK_HOME": str(self.home), "PYTHONPATH": str(ROOT)}

    def dock(self, *args):
        return subprocess.run([sys.executable, "-m", "cli", *args], capture_output=True, text=True, env=self.env,
                              cwd=ROOT, timeout=60, check=False)

    def test_starting_it_fails_with_the_message_and_a_hint_to_reinstall(self):
        r = self.dock("go")
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("dev.dock.sample: [run].any: native plugins are no longer supported", r.stderr)
        self.assertIn("reinstall", r.stderr)
        self.assertFalse((self.home / "logs" / "dev.dock.sample.log").exists())

    def test_plugin_list_and_help_still_work(self):
        self.assertIn("dev.dock.sample", self.dock("plugin", "list").stdout)
        self.assertEqual(self.dock("help").returncode, 0)

    def test_plugin_remove_still_works(self):
        r = self.dock("plugin", "remove", "dev.dock.sample")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.store.index(), {})


class RemovedCapabilities(unittest.TestCase):
    def load(self, extra: str) -> str:
        with self.assertRaises(ManifestError) as cm:
            Manifest.load(make_plugin(CONTAINER, extra))
        return str(cm.exception)

    def test_capabilities_http_fails_the_install(self):
        self.assertEqual(self.load('\n[capabilities]\nhttp = ["example.com"]\n'),
                         "dev.dock.sample: [capabilities].http: HTTP through dock is no longer supported")

    def test_provides_fails_the_install(self):
        self.assertEqual(self.load('\n[capabilities]\nprovides = ["llm"]\n'),
                         "dev.dock.sample: [capabilities].provides: capabilities are no longer supported")

    def test_requires_fails_the_install(self):
        self.assertEqual(self.load('\n[capabilities]\nrequires = ["llm"]\n'),
                         "dev.dock.sample: [capabilities].requires: capabilities are no longer supported")

    def test_a_manifest_without_commands_fails_the_install(self):
        root = make_plugin(CONTAINER)
        (root / "plugin.toml").write_text(BASE.format(run=CONTAINER).split("[[commands]]")[0])
        with self.assertRaisesRegex(ManifestError, r"a plugin needs at least one command$"):
            Manifest.load(root)

    def test_lifecycle_provision_fails_the_install(self):
        self.assertEqual(self.load('\n[lifecycle]\nprovision = true\n'),
                         "dev.dock.sample: [lifecycle].provision is no longer supported")

    def test_the_network_listen_permission_fails_the_install(self):
        self.assertEqual(self.load('\n[capabilities]\npermissions = ["network", "network:listen"]\n'),
                         "dev.dock.sample: permission 'network:listen' is no longer supported")

    def test_requires_permission_on_a_command_fails_the_install(self):
        root = make_plugin(CONTAINER)
        text = (root / "plugin.toml").read_text().replace(
            'name = "go"\n', 'name = "go"\ninputs = [{ name = "x", type = "bool" }]\nrequires_permission = { x = "network" }\n')
        (root / "plugin.toml").write_text(text)
        with self.assertRaises(ManifestError) as cm:
            Manifest.load(root)
        self.assertEqual(str(cm.exception), "dev.dock.sample: go: requires_permission is no longer supported")


class RemovedConfigTables(unittest.TestCase):
    def store(self, text: str) -> Store:
        store = Store(Path(tempfile.mkdtemp(prefix="dock-removed-home-")))
        (store.home / "config.toml").write_text(text)
        return store

    def test_an_http_table_fails_config(self):
        store = self.store('[http]\nmax_response_bytes = 5\n')
        with self.assertRaises(ManifestError) as cm:
            store.config()
        self.assertEqual(str(cm.exception),
                         f"{store.home / 'config.toml'}: [http] is no longer supported: HTTP through dock was removed")

    def test_a_providers_table_fails_config(self):
        store = self.store('[providers]\nllm = "x"\n')
        with self.assertRaises(ManifestError) as cm:
            store.config()
        self.assertEqual(str(cm.exception),
                         f"{store.home / 'config.toml'}: [providers] is no longer supported: capabilities were removed")

    def test_an_isolation_table_fails_config_and_points_to_container_runtime(self):
        store = self.store('[isolation]\nenabled = true\n')
        with self.assertRaises(ManifestError) as cm:
            store.config()
        self.assertEqual(str(cm.exception), f"{store.home / 'config.toml'}: [isolation] was replaced by "
                                            '[container] runtime = "docker" (or podman, or a path)')

    def test_a_wasm_table_fails_config(self):
        store = self.store('[wasm]\nruntime = "x"\n')
        with self.assertRaises(ManifestError) as cm:
            store.config()
        self.assertEqual(str(cm.exception), f"{store.home / 'config.toml'}: [wasm] is no longer supported: "
                                            "dock runs plugins only in a container")

    def test_a_config_without_removed_tables_loads(self):
        self.assertEqual(self.store("[shell]\nidle_timeout = 5\n").config(), {"shell": {"idle_timeout": 5}})


class ReadOnlyFolders(unittest.TestCase):
    """R15: a folder an earlier WASM install left without write bits can still be removed or replaced."""

    def setUp(self):
        self.store = Store(Path(tempfile.mkdtemp(prefix="dock-removed-home-")))
        self.addCleanup(self.cleanup)
        self.src = make_plugin('container = { cmd = "python3" }')
        (self.src / "sub").mkdir()
        (self.src / "sub" / "f.txt").write_text("x")
        self.store.install(self.src)
        self.dest = self.store.plugin_dir / "dev.dock.sample" / "1.2.3"

    def cleanup(self):
        for here, dirs, files in os.walk(self.store.home):
            for name in dirs + files:
                p = Path(here, name)
                if not p.is_symlink():
                    p.chmod(p.stat().st_mode | 0o200)
        shutil.rmtree(self.store.home, ignore_errors=True)

    def lock(self):
        for here, dirs, files in os.walk(self.dest, topdown=False):
            for name in dirs + files:
                p = Path(here, name)
                if not p.is_symlink():
                    p.chmod(p.stat().st_mode & ~0o222)
        self.dest.chmod(self.dest.stat().st_mode & ~0o222)

    def test_remove_deletes_a_read_only_folder(self):
        self.lock()
        self.store.remove("dev.dock.sample")
        self.assertFalse((self.store.plugin_dir / "dev.dock.sample").exists())
        self.assertNotIn("dev.dock.sample", self.store.index())

    def test_remove_does_not_follow_a_symlinked_plugin_folder(self):
        outside = Path(tempfile.mkdtemp(prefix="dock-outside-"))
        self.addCleanup(shutil.rmtree, outside, True)
        (outside / "f.txt").write_text("x")
        outside.chmod(0o500)
        (outside / "f.txt").chmod(0o400)
        shutil.rmtree(self.store.plugin_dir / "dev.dock.sample")
        (self.store.plugin_dir / "dev.dock.sample").symlink_to(outside)
        self.addCleanup(outside.chmod, 0o700)
        self.store.remove("dev.dock.sample")
        self.assertEqual(outside.stat().st_mode & 0o777, 0o500)
        self.assertEqual((outside / "f.txt").stat().st_mode & 0o777, 0o400)

    def test_remove_deletes_a_folder_that_cannot_be_listed(self):
        (self.dest / "sub").chmod(0)
        self.store.remove("dev.dock.sample")
        self.assertFalse((self.store.plugin_dir / "dev.dock.sample").exists())
        self.assertNotIn("dev.dock.sample", self.store.index())

    def test_reinstall_replaces_a_folder_that_cannot_be_listed(self):
        (self.dest / "sub").chmod(0)
        (self.src / "sub" / "f.txt").write_text("new")
        self.store.install(self.src)
        self.assertEqual((self.dest / "sub" / "f.txt").read_text(), "new")

    def test_remove_keeps_the_index_entry_when_the_folder_cannot_be_deleted(self):
        with mock.patch("cli.store.shutil.rmtree", side_effect=OSError("busy")):
            with self.assertRaisesRegex(ManifestError, "cannot remove .*dev.dock.sample.*busy"):
                self.store.remove("dev.dock.sample")
        self.assertIn("dev.dock.sample", self.store.index())

    def test_reinstall_replaces_a_read_only_folder(self):
        self.lock()
        (self.src / "sub" / "f.txt").write_text("new")
        self.store.install(self.src)
        self.assertEqual((self.dest / "sub" / "f.txt").read_text(), "new")


if __name__ == "__main__":
    unittest.main()
