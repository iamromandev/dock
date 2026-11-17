"""WASM runner: manifest form, [wasm] config, argv, store sealing, and the host wiring against a fake wasmtime."""

import hashlib
import io
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from contextlib import redirect_stderr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from dataclasses import replace
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cli import API_VERSION, isolation, wasm  # noqa: E402
from cli.manifest import Manifest, ManifestError, WasmRun  # noqa: E402
from cli.store import Store  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
FAKE = FIXTURES / "fakeruntime" / "wasmtime"

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
    """A temporary directory that is removed when this module's tests are done (sealed folders unsealed first)."""
    path = Path(tempfile.mkdtemp(prefix=prefix))
    unittest.addModuleCleanup(_remove, path)
    return path


def _remove(path: Path) -> None:
    for root, dirs, files in os.walk(path):  # a sealed tree cannot be deleted before it is made writable
        os.chmod(root, 0o755)
        for name in dirs:
            if not os.path.islink(os.path.join(root, name)):
                os.chmod(os.path.join(root, name), 0o755)
    shutil.rmtree(path, ignore_errors=True)


def make_plugin(run: str, extra: str = "", files=("main.py", "plugin.wasm")) -> Path:
    root = temp_dir("dock-wasm-plugin-")
    (root / "plugin.toml").write_text(BASE.format(run=run) + extra)
    for name in files:
        (root / name).write_text("# fixture\n")
    return root


MODULE = 'wasm = { module = "plugin.wasm" }'
PYTHON = 'wasm = { python = "3.15", script = "main.py" }'


class ManifestWasm(unittest.TestCase):
    """R1, R3: the [run].wasm forms."""

    def test_module_form_is_parsed(self):
        m = Manifest.load(make_plugin(MODULE))
        self.assertEqual(m.wasm, WasmRun(module="plugin.wasm"))

    def test_python_form_is_parsed_with_args(self):
        m = Manifest.load(make_plugin('wasm = { python = "3.15", script = "main.py", args = ["-x", "y"] }'))
        self.assertEqual(m.wasm, WasmRun(python="3.15", script="main.py", args=["-x", "y"]))

    def test_no_wasm_entry_means_none(self):
        m = Manifest.load(make_plugin('any = { cmd = "python3" }'))
        self.assertIsNone(m.wasm)

    def test_both_forms_at_once_are_rejected(self):
        with self.assertRaisesRegex(ManifestError, r"\[run\]\.wasm.*exactly one"):
            Manifest.load(make_plugin('wasm = { module = "plugin.wasm", python = "3.15", script = "main.py" }'))

    def test_neither_form_is_rejected(self):
        for spec in ("{}", '{ args = ["a"] }', '{ python = "3.15" }', '{ script = "main.py" }'):
            with self.subTest(spec=spec):
                with self.assertRaisesRegex(ManifestError, r"\[run\]\.wasm"):
                    Manifest.load(make_plugin(f"wasm = {spec}"))

    def test_module_outside_the_folder_is_rejected(self):
        for path in ("../x.wasm", "/etc/passwd", "a/../../x.wasm"):
            with self.subTest(path=path):
                with self.assertRaisesRegex(ManifestError, r"\[run\]\.wasm.*inside the plugin folder"):
                    Manifest.load(make_plugin(f'wasm = {{ module = "{path}" }}'))

    def test_module_symlink_escaping_the_folder_is_rejected(self):
        root = make_plugin('wasm = { module = "link.wasm" }')
        outside = temp_dir("dock-wasm-out-")
        (outside / "m.wasm").write_text("x")
        (root / "link.wasm").symlink_to(outside / "m.wasm")
        with self.assertRaisesRegex(ManifestError, "inside the plugin folder"):
            Manifest.load(root)

    def test_missing_module_is_rejected(self):
        with self.assertRaisesRegex(ManifestError, r"\[run\]\.wasm.*nope\.wasm.*not found"):
            Manifest.load(make_plugin('wasm = { module = "nope.wasm" }'))

    def test_missing_script_is_rejected(self):
        with self.assertRaisesRegex(ManifestError, r"\[run\]\.wasm.*nope\.py.*not found"):
            Manifest.load(make_plugin('wasm = { python = "3.15", script = "nope.py" }'))

    def test_script_outside_the_folder_is_rejected(self):
        with self.assertRaisesRegex(ManifestError, "inside the plugin folder"):
            Manifest.load(make_plugin('wasm = { python = "3.15", script = "../main.py" }'))

    def test_unknown_python_version_is_rejected_listing_the_known_ones(self):
        with self.assertRaisesRegex(ManifestError, r"python '3\.9'.*known: 3\.15"):
            Manifest.load(make_plugin('wasm = { python = "3.9", script = "main.py" }'))

    def test_unknown_key_is_rejected_naming_it(self):
        with self.assertRaisesRegex(ManifestError, r"unknown key 'modul'"):
            Manifest.load(make_plugin('wasm = { modul = "plugin.wasm" }'))

    def test_wrong_types_are_rejected(self):
        for spec in ('"plugin.wasm"', '{ module = 3 }', '{ python = 3.15, script = "main.py" }',
                     '{ python = "3.15", script = "main.py", args = "x" }',
                     '{ python = "3.15", script = "main.py", args = [1] }'):
            with self.subTest(spec=spec):
                with self.assertRaisesRegex(ManifestError, r"\[run\]\.wasm"):
                    Manifest.load(make_plugin(f"wasm = {spec}"))

    def test_network_permission_is_refused(self):
        root = make_plugin(MODULE, '\n[capabilities]\npermissions = ["fs:write:cwd", "network"]\n')
        with self.assertRaises(ManifestError) as cm:
            Manifest.load(root)
        self.assertEqual(str(cm.exception), "dev.dock.sample: WASM plugins cannot use 'network'")

    def test_other_permissions_are_fine(self):
        m = Manifest.load(make_plugin(MODULE, '\n[capabilities]\npermissions = ["fs:write:cwd"]\n'))
        self.assertEqual(m.permissions, ["fs:write:cwd"])

    def test_entrypoint_ignores_the_wasm_key(self):
        m = Manifest.load(make_plugin(MODULE + '\nany = { cmd = "python3", args = ["m.py"] }'))
        self.assertEqual(m.entrypoint(), ["python3", "m.py"])

    def test_wasm_only_manifest_has_no_native_build(self):
        with self.assertRaisesRegex(ManifestError, "no native build"):
            Manifest.load(make_plugin(MODULE)).entrypoint()

    def test_wasm_does_not_count_as_a_container_build(self):
        m = Manifest.load(make_plugin(MODULE))
        (m.root / "Dockerfile").write_text("FROM scratch\n")
        self.assertFalse(isolation.has_container_build(m))


class InstallWasm(unittest.TestCase):
    def setUp(self):
        self.store = Store(temp_dir("dock-wasm-home-") / "home")

    def test_each_valid_form_installs_without_a_native_entry(self):
        for run in (MODULE, PYTHON):
            with self.subTest(run=run):
                m = self.store.install(make_plugin(run))
                self.assertIsNotNone(m.wasm)
                self.assertIn(m.id, self.store.index())

    def test_an_invalid_form_fails_the_install_and_adds_nothing(self):
        with self.assertRaisesRegex(ManifestError, r"\[run\]\.wasm"):
            self.store.install(make_plugin('wasm = { module = "nope.wasm" }'))
        self.assertEqual(self.store.index(), {})

    def test_network_fails_the_install(self):
        root = make_plugin(MODULE, '\n[capabilities]\npermissions = ["network"]\n')
        with self.assertRaisesRegex(ManifestError, "dev.dock.sample: WASM plugins cannot use 'network'"):
            self.store.install(root)
        self.assertEqual(self.store.index(), {})


class WasmConfig(unittest.TestCase):
    """R4: `[wasm] runtime` in config.toml, and finding the runtime."""

    def store(self, config=None) -> Store:
        home = temp_dir("dock-wasm-home-")
        if config is not None:
            (home / "config.toml").write_text(config)
        return Store(home)

    def test_default_runtime_is_wasmtime(self):
        self.assertEqual(wasm.config(self.store()), "wasmtime")
        self.assertEqual(wasm.config(self.store("[shell]\nidle_timeout = 5\n")), "wasmtime")
        self.assertEqual(wasm.config(self.store("[wasm]\n")), "wasmtime")

    def test_runtime_comes_from_the_wasm_table(self):
        self.assertEqual(wasm.config(self.store('[wasm]\nruntime = "/opt/wt/wasmtime"\n')), "/opt/wt/wasmtime")

    def test_an_unknown_key_is_refused_naming_it_and_the_known_ones(self):
        store = self.store('[wasm]\nruntme = "x"\n')
        with self.assertRaises(ManifestError) as cm:
            wasm.config(store)
        self.assertEqual(str(cm.exception), f"{store.home / 'config.toml'}: [wasm] has unknown key 'runtme' (known: runtime)")

    def test_several_unknown_keys_are_all_named(self):
        with self.assertRaisesRegex(ManifestError, "unknown keys 'a', 'b'"):
            wasm.config(self.store('[wasm]\na = 1\nb = 2\n'))

    def test_wrong_types_are_refused(self):
        for body in ("[wasm]\nruntime = 3\n", '[wasm]\nruntime = ""\n', "wasm = 5\n"):
            with self.subTest(body=body):
                with self.assertRaisesRegex(ManifestError, r"\[wasm\]"):
                    wasm.config(self.store(body))

    def test_a_config_that_does_not_parse_is_refused(self):
        with self.assertRaisesRegex(ManifestError, "config.toml"):
            wasm.config(self.store("[wasm\nruntime = 1\n"))

    def test_find_runtime_returns_the_resolved_path(self):
        with mock.patch.dict(os.environ, {"PATH": f"{FAKE.parent}{os.pathsep}{os.environ['PATH']}"}):
            self.assertEqual(wasm.find_runtime("wasmtime"), str(FAKE))
        self.assertEqual(wasm.find_runtime(str(FAKE)), str(FAKE))

    def test_a_missing_runtime_is_refused_with_the_install_hint(self):
        with mock.patch.dict(os.environ, {"PATH": ""}):
            with self.assertRaises(ManifestError) as cm:
                wasm.find_runtime("wasmtime")
        self.assertEqual(str(cm.exception), "WASM runtime 'wasmtime' not found (install wasmtime)")
        with self.assertRaisesRegex(ManifestError, r"WASM runtime '/nonexistent/wt' not found"):
            wasm.find_runtime("/nonexistent/wt")


class RunArgv(unittest.TestCase):
    """R6: the exact wasmtime command line, fixed order."""

    def manifest(self, run=MODULE, permissions=()):
        extra = f"\n[capabilities]\npermissions = {json.dumps(list(permissions))}\n" if permissions else ""
        return Manifest.load(make_plugin(run, extra))

    def argv(self, m, **kw):
        args = dict(plugin_root="/p/plug", data_dir="/h/data/x", cwd="/work/dir", home="/h/data/x/.home",
                    env=wasm.guest_env(m, "/h/data/x/.home"), passthrough=[])
        return wasm.run_argv("/bin/wasmtime", m, **{**args, **kw})

    ENV = ["--env", "HOME=/h/data/x/.home", "--env", "DOCK_PLUGIN_ID=dev.dock.sample",
           "--env", f"DOCK_API_VERSION={API_VERSION}"]

    def test_exact_argv_for_a_module_without_permissions(self):
        m = self.manifest()
        self.assertEqual(self.argv(m), [
            "/bin/wasmtime", "run", "--dir", "/p/plug::/plugin", "--dir", "/h/data/x::/h/data/x",
            *self.ENV, "--", "/p/plug/plugin.wasm"])

    def test_exact_argv_for_python_with_cwd_passthrough_and_args(self):
        m = self.manifest('wasm = { python = "3.15", script = "main.py", args = ["--fast", "x y"] }', ["fs:write:cwd"])
        self.assertEqual(self.argv(m, python_root="/h/wasm/python-3.15.2", passthrough=["MY_VAR", "OTHER"]), [
            "/bin/wasmtime", "run", "--dir", "/p/plug::/plugin", "--dir", "/h/wasm/python-3.15.2::/python",
            "--dir", "/h/data/x::/h/data/x", "--dir", "/work/dir::/work/dir",
            *self.ENV, "--env", "PYTHONHOME=/python", "--env", "MY_VAR", "--env", "OTHER",
            "--", "/h/wasm/python-3.15.2/python.wasm", "/plugin/main.py", "--fast", "x y"])

    def test_module_args_follow_the_module(self):
        m = self.manifest('wasm = { module = "plugin.wasm", args = ["a"] }')
        self.assertEqual(self.argv(m)[-2:], ["/p/plug/plugin.wasm", "a"])

    def test_the_cwd_is_granted_only_with_fs_write_cwd(self):
        self.assertNotIn("/work/dir::/work/dir", self.argv(self.manifest()))
        self.assertIn("/work/dir::/work/dir", self.argv(self.manifest(permissions=["fs:write:cwd"])))

    def test_no_network_or_other_runtime_option_is_ever_passed(self):
        for perms in ([], ["fs:write:cwd"]):
            for run in (MODULE, PYTHON):
                m = self.manifest(run, perms)
                argv = self.argv(m, python_root="/h/py" if m.wasm.python else None)
                options = {a for a in argv[2:argv.index("--")] if a.startswith("-")}
                self.assertEqual(options, {"--dir", "--env"})

    def test_values_are_given_only_for_the_variables_dock_sets(self):
        argv = self.argv(self.manifest(), passthrough=["MY_VAR"])
        pairs = [argv[i + 1] for i, a in enumerate(argv) if a == "--env"]
        self.assertEqual(pairs, ["HOME=/h/data/x/.home", "DOCK_PLUGIN_ID=dev.dock.sample",
                                 f"DOCK_API_VERSION={API_VERSION}", "MY_VAR"])

    def test_guest_env_is_home_and_the_dock_variables_only(self):
        self.assertEqual(wasm.guest_env(self.manifest(), "/h"), {
            "HOME": "/h", "DOCK_PLUGIN_ID": "dev.dock.sample", "DOCK_API_VERSION": API_VERSION})

    def test_paths_with_spaces_stay_single_arguments(self):
        argv = self.argv(self.manifest(permissions=["fs:write:cwd"]), plugin_root="/p/my plug",
                         data_dir="/h/my data", cwd="/my work")
        self.assertIn("/p/my plug::/plugin", argv)
        self.assertIn("/h/my data::/h/my data", argv)
        self.assertIn("/my work::/my work", argv)
        self.assertEqual(argv[-1], "/p/my plug/plugin.wasm")

    def test_a_path_containing_the_separator_is_refused(self):
        m = self.manifest(permissions=["fs:write:cwd"])
        for key in ("plugin_root", "data_dir", "cwd"):
            with self.subTest(key=key):
                with self.assertRaisesRegex(ManifestError, rf"dev.dock.sample: the {key.replace('_', ' ').replace('plugin root', 'plugin folder')} path contains '::'"):
                    self.argv(m, **{key: "/a::b/c"})
        with self.assertRaisesRegex(ManifestError, "the python folder path contains '::'"):
            self.argv(self.manifest(PYTHON), python_root="/a::b")

    def test_a_path_ending_in_a_colon_is_refused_because_it_runs_into_the_separator(self):
        m = self.manifest(permissions=["fs:write:cwd"])
        for key in ("plugin_root", "data_dir", "cwd"):
            with self.subTest(key=key):
                with self.assertRaisesRegex(ManifestError, "ends with ':'"):
                    self.argv(m, **{key: "/x/proj:"})
        with self.assertRaisesRegex(ManifestError, "the python folder path ends with ':'"):
            self.argv(self.manifest(PYTHON), python_root="/x/py:")

    def test_a_colon_inside_a_path_is_fine(self):
        argv = self.argv(self.manifest(permissions=["fs:write:cwd"]), cwd="/x:y/proj")
        self.assertIn("/x:y/proj::/x:y/proj", argv)

    def test_a_cwd_with_the_separator_does_not_matter_when_it_is_not_granted(self):
        self.assertNotIn("/a::b", " ".join(self.argv(self.manifest(), cwd="/a::b")))


def writable(path: Path) -> bool:
    return bool(path.stat().st_mode & 0o222)


class PluginIdSafety(unittest.TestCase):
    """Review 1: the id names folders under the store, so it must be one plain path component."""

    def load(self, plugin_id: str) -> Manifest:
        root = make_plugin(MODULE)
        text = (root / "plugin.toml").read_text().replace('id = "dev.dock.sample"', f'id = {json.dumps(plugin_id)}')
        (root / "plugin.toml").write_text(text)
        return Manifest.load(root)

    def test_ids_that_could_leave_the_store_are_refused(self):
        for bad in ("../..", "..", "a/../..", "dev/dock.x", "dev.dock/../../x", "/etc.passwd", "a\\b.c", ".hidden.x",
                    "-dash.x", "dev.dock.x/", "dev.dock.x ", "dev.dock.\u00e9"):
            with self.subTest(id=bad):
                with self.assertRaisesRegex(ManifestError, "id"):
                    self.load(bad)

    def test_an_id_that_is_not_text_is_refused(self):
        root = make_plugin(MODULE)
        (root / "plugin.toml").write_text((root / "plugin.toml").read_text().replace('id = "dev.dock.sample"', "id = 5"))
        with self.assertRaisesRegex(ManifestError, "id"):
            Manifest.load(root)

    def test_ordinary_ids_still_load(self):
        for good in ("dev.dock.sample", "vendor.name", "a.b-c_d", "acme.tool2", "x..y"):
            with self.subTest(id=good):
                self.assertEqual(self.load(good).id, good)

    def test_the_bundled_plugins_have_valid_ids(self):
        for toml in sorted((ROOT / "plugin").glob("*/plugin.toml")):
            with self.subTest(plugin=toml.parent.name):
                self.assertTrue(Manifest.load(toml.parent).id)

    def test_the_store_refuses_a_data_dir_or_install_path_that_is_not_a_direct_child(self):
        store = Store(temp_dir("dock-wasm-home-") / "home")
        for bad in ("../..", "..", ".", "", "a/b", "/abs"):
            with self.subTest(id=bad):
                with self.assertRaises(ManifestError):
                    store.data_dir(bad)
        self.assertEqual(store.data_dir("dev.dock.ok"), store.data_root / "dev.dock.ok")

    def test_a_traversing_id_installs_nothing_and_touches_nothing_outside_the_store(self):
        base = temp_dir("dock-wasm-base-")
        store = Store(base / "home")
        victim = base / "victim"
        victim.mkdir()
        (victim / "keep").write_text("x")
        for bad in ("../..", "../../victim"):
            with self.subTest(id=bad):
                root = make_plugin(MODULE)
                (root / "plugin.toml").write_text(
                    (root / "plugin.toml").read_text().replace('id = "dev.dock.sample"', f'id = "{bad}"'))
                with self.assertRaisesRegex(ManifestError, "id"):
                    store.install(root)
        self.assertEqual((victim / "keep").read_text(), "x")
        self.assertEqual(store.index(), {})

    def test_a_traversing_version_installs_nothing(self):
        store = Store(temp_dir("dock-wasm-home-") / "home")
        root = make_plugin(MODULE)
        (root / "plugin.toml").write_text((root / "plugin.toml").read_text().replace('version = "1.2.3"', 'version = "../.."'))
        with self.assertRaisesRegex(ManifestError, "plugin version"):
            store.install(root)
        self.assertEqual(store.index(), {})


class InstallSymlinks(unittest.TestCase):
    """Review 2: an installed copy must not smuggle host files in through symlinks."""

    def setUp(self):
        self.base = temp_dir("dock-wasm-links-")
        self.store = Store(self.base / "home")
        self.secret = self.base / "secret.txt"
        self.secret.write_text("host secret")

    def plugin(self, run=MODULE) -> Path:
        root = make_plugin(run)
        (root / "sub").mkdir()
        (root / "sub" / "data.txt").write_text("inside")
        return root

    def test_a_symlink_to_a_host_file_outside_the_folder_is_refused_for_every_kind_of_plugin(self):
        for run in (MODULE, 'any = { cmd = "python3" }'):
            with self.subTest(run=run):
                root = self.plugin(run)
                (root / "leak.txt").symlink_to(self.secret)
                with self.assertRaisesRegex(ManifestError, r"symlink.*leak\.txt.*outside"):
                    self.store.install(root)
                self.assertEqual(self.store.index(), {})

    def test_a_relative_symlink_climbing_out_is_refused(self):
        root = self.plugin()
        (root / "sub" / "up").symlink_to("../../secret.txt")
        with self.assertRaisesRegex(ManifestError, r"symlink.*outside"):
            self.store.install(root)

    def test_a_directory_symlink_to_the_parent_is_refused(self):
        root = self.plugin()
        (root / "sub" / "loop").symlink_to(root.parent)
        with self.assertRaisesRegex(ManifestError, r"symlink.*outside"):
            self.store.install(root)

    def test_a_refused_install_keeps_the_previous_install_working(self):
        root = self.plugin()
        self.store.install(root)
        (root / "leak.txt").symlink_to(self.secret)
        with self.assertRaises(ManifestError):
            self.store.install(root)
        self.assertTrue((self.store.get("dev.dock.sample").root / "plugin.wasm").is_file())

    def test_symlinks_that_stay_inside_are_kept_as_symlinks(self):
        root = self.plugin()
        (root / "alias.txt").symlink_to("sub/data.txt")
        (root / "subalias").symlink_to("sub")
        m = self.store.install(root)
        self.assertTrue((m.root / "alias.txt").is_symlink())
        self.assertEqual((m.root / "alias.txt").read_text(), "inside")
        self.assertTrue((m.root / "subalias").is_symlink())

    def test_a_symlink_inside_an_ignored_folder_is_not_looked_at(self):
        root = self.plugin()
        (root / "node_modules").mkdir()
        (root / "node_modules" / "x").symlink_to(self.secret)
        m = self.store.install(root)
        self.assertFalse((m.root / "node_modules").exists())

    def test_the_installed_copy_never_holds_the_host_file(self):
        root = self.plugin()
        (root / "ok").symlink_to("sub/data.txt")
        m = self.store.install(root)
        self.assertNotIn("host secret", "".join(p.read_text() for p in m.root.rglob("*") if p.is_file()))


class Sealing(unittest.TestCase):
    """Read-only plugin folders come from host permissions: wasmtime has no read-only --dir."""

    def setUp(self):
        self.tmp = temp_dir("dock-wasm-seal-")
        self.tree = self.tmp / "tree"
        (self.tree / "sub" / "deeper").mkdir(parents=True)
        (self.tree / "a.txt").write_text("a")
        (self.tree / "run.sh").write_text("#!/bin/sh\n")
        (self.tree / "run.sh").chmod(0o755)
        (self.tree / "sub" / "deeper" / "b.txt").write_text("b")
        (self.tree / "link").symlink_to("a.txt")

    def test_seal_clears_every_write_bit_and_keeps_the_rest(self):
        wasm.seal(self.tree)
        self.assertEqual(self.tree.stat().st_mode & 0o777, 0o555)
        self.assertEqual((self.tree / "sub" / "deeper").stat().st_mode & 0o777, 0o555)
        self.assertEqual((self.tree / "a.txt").stat().st_mode & 0o777, 0o444)
        self.assertEqual((self.tree / "run.sh").stat().st_mode & 0o777, 0o555)  # x bits stay
        self.assertEqual((self.tree / "sub" / "deeper" / "b.txt").stat().st_mode & 0o777, 0o444)

    def test_a_sealed_tree_cannot_be_written_to_or_added_to(self):
        wasm.seal(self.tree)
        with self.assertRaises(PermissionError):
            (self.tree / "a.txt").write_text("changed")
        with self.assertRaises(PermissionError):
            (self.tree / "new.txt").write_text("new")

    def test_unseal_makes_the_owner_able_to_write_again(self):
        wasm.seal(self.tree)
        wasm.unseal(self.tree)
        (self.tree / "a.txt").write_text("changed")
        (self.tree / "sub" / "new.txt").write_text("new")
        self.assertEqual((self.tree / "a.txt").stat().st_mode & 0o200, 0o200)

    def test_unseal_then_rmtree_works_where_rmtree_alone_does_not(self):
        wasm.seal(self.tree)
        with self.assertRaises(PermissionError):
            shutil.rmtree(self.tree)
        wasm.unseal(self.tree)
        shutil.rmtree(self.tree)
        self.assertFalse(self.tree.exists())

    def test_sealing_does_not_follow_symlinks_out_of_the_tree(self):
        outside = self.tmp / "outside.txt"
        outside.write_text("x")
        (self.tree / "escape").symlink_to(outside)
        wasm.seal(self.tree)
        self.assertTrue(writable(outside))

    def test_sealing_and_unsealing_a_missing_path_is_a_no_op(self):
        wasm.seal(self.tmp / "nope")
        wasm.unseal(self.tmp / "nope")


class StoreSealing(unittest.TestCase):
    def setUp(self):
        self.store = Store(temp_dir("dock-wasm-home-") / "home")

    def installed_files(self, m):
        return [p for p in m.root.rglob("*") if not p.is_symlink()]

    def test_a_wasm_plugin_is_sealed_after_install(self):
        m = self.store.install(make_plugin(MODULE))
        self.assertFalse(writable(m.root))
        self.assertFalse(any(writable(p) for p in self.installed_files(m)))
        with self.assertRaises(PermissionError):
            (m.root / "plugin.wasm").write_text("tampered")

    def test_a_plugin_without_wasm_is_not_sealed(self):
        m = self.store.install(make_plugin('any = { cmd = "python3", args = ["main.py"] }'))
        self.assertTrue(writable(m.root))
        self.assertTrue(writable(m.root / "main.py"))

    def test_the_data_dir_stays_writable(self):
        m = self.store.install(make_plugin(MODULE))
        data = self.store.data_dir(m.id)
        (data / "x.txt").write_text("ok")
        self.assertTrue(writable(data))

    def test_reinstalling_the_same_version_replaces_the_sealed_folder(self):
        src = make_plugin(MODULE)
        self.store.install(src)
        (src / "plugin.wasm").write_text("# second\n")
        m = self.store.install(src)
        self.assertEqual((m.root / "plugin.wasm").read_text(), "# second\n")
        self.assertFalse(writable(m.root))

    def test_reinstalling_a_wasm_plugin_as_a_native_one_works(self):
        src = make_plugin(MODULE)
        self.store.install(src)
        run = 'any = { cmd = "python3", args = ["main.py"] }'
        m = self.store.install(make_plugin(run))
        self.assertIsNone(m.wasm)
        self.assertTrue(writable(m.root))

    def test_remove_deletes_a_sealed_plugin_completely(self):
        m = self.store.install(make_plugin(MODULE))
        self.store.remove(m.id)
        self.assertFalse((self.store.plugin_dir / m.id).exists())
        self.assertEqual(self.store.index(), {})


WASMMOD = FIXTURES / "wasmmod"
DOCKER = FIXTURES / "fakeruntime" / "docker"
GUEST_PYTHON_LIB = "lib/python3.15"


def wasmmod_as_python(dest: Path) -> Path:
    """The wasmmod fixture in the Python form: the same program as main.py on the shared interpreter."""
    shutil.copytree(WASMMOD, dest)
    (dest / "plugin.wasm").rename(dest / "main.py")
    toml = (dest / "plugin.toml").read_text().replace(
        'wasm = { module = "plugin.wasm", args = ["--flag"] }', 'wasm = { python = "3.15", script = "main.py", args = ["--flag"] }')
    (dest / "plugin.toml").write_text(toml)
    return dest


def provide_python(store: Store) -> Path:
    """Stand in for the verified interpreter: a fake python.wasm and a standard library folder, marked verified."""
    root = wasm.python_home(store, "3.15")
    (root / GUEST_PYTHON_LIB).mkdir(parents=True)
    (root / "python.wasm").write_text("# not a real interpreter\n")
    (root / ".verified").write_text(wasm.PYTHONS["3.15"].sha256)
    wasm.seal(root)  # a verified interpreter is always sealed; prepare refuses one that is not
    return root


class WasmCase(unittest.TestCase):
    """A throwaway DOCK_HOME whose config points at the fake wasmtime, which logs its calls."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dock-wasm-case-"))
        self.addCleanup(_remove, self.tmp)
        self.log = self.tmp / "wasmtime-calls"
        self.docker_log = self.tmp / "docker-calls"
        self.store = Store(self.tmp / "home")
        patcher = mock.patch.dict(os.environ, {
            "FAKE_WASMTIME_LOG": str(self.log), "FAKE_RUNTIME_LOG": str(self.docker_log), "SECRET_TOKEN": "s3cret"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.configure()

    def configure(self, extra: str = "", runtime: str | None = None) -> None:
        (self.store.home / "config.toml").write_text(f'[wasm]\nruntime = "{runtime or FAKE}"\n{extra}')

    def calls(self) -> list[list[str]]:
        return [json.loads(l) for l in self.log.read_text().splitlines()] if self.log.exists() else []

    def docker_calls(self, *verbs) -> list[list[str]]:
        rows = [json.loads(l) for l in self.docker_log.read_text().splitlines()] if self.docker_log.exists() else []
        return [r for r in rows if r[0] in verbs]

    def install_mod(self, permissions=()) -> Manifest:
        src = temp_dir("dock-wasm-src-") / "plug"
        shutil.copytree(WASMMOD, src)
        if permissions:
            with open(src / "plugin.toml", "a") as f:
                f.write(f"permissions = {json.dumps(list(permissions))}\n")
        return self.store.install(src)

    def install_python(self, permissions=()) -> Manifest:
        src = wasmmod_as_python(temp_dir("dock-wasm-src-") / "plug")
        if permissions:
            with open(src / "plugin.toml", "a") as f:
                f.write(f"permissions = {json.dumps(list(permissions))}\n")
        return self.store.install(src)


class Prepare(WasmCase):
    """wasm.prepare: from a manifest to a Launch."""

    def test_a_module_plugin_launches_through_the_runtime_with_exactly_its_grants(self):
        m = self.install_mod()
        data = self.store.data_dir(m.id)
        launch = wasm.prepare(self.store, m, cwd="/some/cwd")
        self.assertEqual(launch.argv, [
            str(FAKE), "run", "--dir", f"{m.root}::/plugin", "--dir", f"{data}::{data}",
            "--env", f"HOME={data}/.home", "--env", f"DOCK_PLUGIN_ID={m.id}", "--env", f"DOCK_API_VERSION={API_VERSION}",
            "--", f"{m.root}/plugin.wasm", "--flag"])
        self.assertEqual(launch.runner, "wasm")
        self.assertIsNone(launch.container)
        self.assertIsNone(launch.runtime)

    def test_home_is_a_folder_inside_the_data_dir_that_exists(self):
        m = self.install_mod()
        wasm.prepare(self.store, m)
        self.assertTrue((self.store.data_dir(m.id) / ".home").is_dir())

    def test_the_runtime_client_keeps_the_host_environment_but_no_value_reaches_the_command_line(self):
        m = self.install_mod()
        launch = wasm.prepare(self.store, m)
        self.assertEqual(launch.env["SECRET_TOKEN"], "s3cret")  # wasmtime resolves `--env NAME` from this
        self.assertNotIn("s3cret", " ".join(launch.argv))
        self.assertNotIn("SECRET_TOKEN", " ".join(launch.argv))

    def test_declared_env_is_passed_by_name_only_and_only_when_set_on_the_host(self):
        m = self.install_mod()
        with mock.patch.dict(os.environ, {"MY_VAR": "hello-123"}):
            launch = wasm.prepare(self.store, m)
        passed = [launch.argv[i + 1] for i, a in enumerate(launch.argv) if a == "--env"]
        self.assertIn("MY_VAR", passed)
        self.assertNotIn("hello-123", " ".join(launch.argv))
        with mock.patch.dict(os.environ):
            os.environ.pop("MY_VAR", None)
            launch = wasm.prepare(self.store, m)
        self.assertNotIn("MY_VAR", launch.argv)

    def test_the_python_form_maps_the_verified_interpreter_read_only_folder(self):
        m = self.install_python()
        py = provide_python(self.store)
        launch = wasm.prepare(self.store, m)
        self.assertIn(f"{py}::/python", launch.argv)
        self.assertIn("PYTHONHOME=/python", launch.argv)
        self.assertEqual(launch.argv[launch.argv.index("--"):],
                         ["--", f"{py}/python.wasm", "/plugin/main.py", "--flag"])

    def test_the_python_form_without_an_interpreter_installed_is_refused_not_started(self):
        m = self.install_python()
        with mock.patch.dict(os.environ, unreachable_override()), redirect_stderr(StringIO()):  # never the real download
            with self.assertRaisesRegex(ManifestError, r"python 3\.15\.2 for WASI"):
                wasm.prepare(self.store, m)
        self.assertEqual(self.calls(), [])

    def test_ensure_python_returns_the_verified_folder_without_fetching(self):
        py = provide_python(self.store)
        self.assertEqual(wasm.ensure_python(self.store, "3.15"), py)
        self.assertEqual(py, self.store.home / "wasm" / "python-3.15.2")

    def test_a_missing_runtime_is_refused_with_the_message_and_nothing_is_prepared(self):
        self.configure(runtime="/nonexistent/wasmtime")
        m = self.install_python()
        with self.assertRaises(ManifestError) as cm:
            wasm.prepare(self.store, m)
        self.assertEqual(str(cm.exception), "WASM runtime '/nonexistent/wasmtime' not found (install wasmtime)")

    def test_wasmtime_is_found_on_path_without_any_config(self):
        (self.store.home / "config.toml").unlink()
        m = self.install_mod()
        with mock.patch.dict(os.environ, {"PATH": f"{FAKE.parent}{os.pathsep}{os.environ['PATH']}"}):
            self.assertEqual(wasm.prepare(self.store, m).argv[0], str(FAKE))
        with mock.patch.dict(os.environ, {"PATH": ""}):
            with self.assertRaisesRegex(ManifestError, "WASM runtime 'wasmtime' not found"):
                wasm.prepare(self.store, m)

    def test_an_unknown_key_in_the_wasm_table_refuses_to_start(self):
        (self.store.home / "config.toml").write_text('[wasm]\nruntme = "x"\n')
        with self.assertRaisesRegex(ManifestError, "unknown key 'runtme'"):
            wasm.prepare(self.store, self.install_mod())


class ReservedEnvNames(unittest.TestCase):
    """Review 5: a declared env name must not override a variable dock sets for the guest."""

    def load(self, run, names):
        return Manifest.load(make_plugin(run, f"\n[capabilities]\nenv = {json.dumps(names)}\n"))

    def test_names_dock_sets_are_refused_for_both_forms(self):
        for run in (MODULE, PYTHON):
            for name in ("HOME", "DOCK_PLUGIN_ID", "DOCK_API_VERSION", "PYTHONHOME"):
                with self.subTest(run=run, name=name):
                    with self.assertRaisesRegex(ManifestError, rf"env entry '{name}' is set by dock"):
                        self.load(run, [name])

    def test_the_python_form_refuses_every_python_variable(self):
        for name in ("PYTHONPATH", "PYTHONSTARTUP", "PYTHONUTF8", "PYTHONDONTWRITEBYTECODE"):
            with self.subTest(name=name):
                with self.assertRaisesRegex(ManifestError, rf"env entry '{name}'.*Python"):
                    self.load(PYTHON, [name])

    def test_the_module_form_may_declare_other_python_variables(self):
        self.assertEqual(self.load(MODULE, ["PYTHONPATH"]).env, ["PYTHONPATH"])

    def test_ordinary_names_are_fine_in_both_forms(self):
        for run in (MODULE, PYTHON):
            self.assertEqual(self.load(run, ["MY_TOKEN", "DOCK_WASMPY_DECLARED"]).env, ["MY_TOKEN", "DOCK_WASMPY_DECLARED"])

    def test_native_plugins_are_not_affected(self):
        m = Manifest.load(make_plugin('any = { cmd = "python3" }', '\n[capabilities]\nenv = ["PYTHONPATH"]\n'))
        self.assertEqual(m.env, ["PYTHONPATH"])

    def test_the_install_fails_and_adds_nothing(self):
        store = Store(temp_dir("dock-wasm-home-") / "home")
        with self.assertRaisesRegex(ManifestError, "PYTHONHOME"):
            store.install(make_plugin(PYTHON, '\n[capabilities]\nenv = ["PYTHONHOME"]\n'))
        self.assertEqual(store.index(), {})

    def test_run_argv_drops_a_passthrough_name_that_is_already_set_by_value(self):
        m = Manifest.load(make_plugin(PYTHON))
        argv = wasm.run_argv("/bin/wasmtime", m, plugin_root="/p", data_dir="/d", cwd="/c", home="/d/.home",
                             env={**wasm.guest_env(m, "/d/.home"), "EXTRA": "1"},
                             passthrough=["PYTHONHOME", "HOME", "EXTRA", "OK_NAME"], python_root="/py")
        pairs = [argv[i + 1] for i, a in enumerate(argv) if a == "--env"]
        self.assertEqual([p for p in pairs if "=" not in p], ["OK_NAME"])
        self.assertEqual(pairs.count("PYTHONHOME=/python"), 1)


class SealedGrants(WasmCase):
    """Review 4: the read-only grants are host permissions, so prepare checks them and fails closed."""

    def test_a_sealed_plugin_and_interpreter_start(self):
        m = self.install_python()
        provide_python(self.store)
        self.assertEqual(wasm.prepare(self.store, m).runner, "wasm")

    def test_a_writable_plugin_folder_is_refused_with_how_to_fix_it(self):
        m = self.install_mod()
        os.chmod(m.root, 0o755)
        with self.assertRaises(ManifestError) as cm:
            wasm.prepare(self.store, m)
        msg = str(cm.exception)
        self.assertIn(m.id, msg)
        self.assertIn(str(m.root), msg)
        self.assertIn("writable", msg)
        self.assertIn("reinstall", msg)
        self.assertEqual(self.calls(), [])

    def test_a_writable_file_or_nested_folder_in_the_plugin_is_refused(self):
        for rel in ("plugin.wasm", "plugin.toml"):
            with self.subTest(rel=rel):
                m = self.install_mod()
                target = m.root / rel
                os.chmod(target, 0o644)
                with self.assertRaisesRegex(ManifestError, rf"writable.*{rel}"):
                    wasm.prepare(self.store, m)
                os.chmod(target, 0o444)
        m = self.install_mod()
        wasm.unseal(m.root)
        (m.root / "sub").mkdir()
        wasm.seal(m.root)
        os.chmod(m.root / "sub", 0o755)
        with self.assertRaisesRegex(ManifestError, "writable.*sub"):
            wasm.prepare(self.store, m)

    def test_a_writable_interpreter_folder_is_refused_telling_how_to_refetch_it(self):
        m = self.install_python()
        py = provide_python(self.store)
        os.chmod(py / "python.wasm", 0o644)
        with self.assertRaises(ManifestError) as cm:
            wasm.prepare(self.store, m)
        self.assertIn("writable", str(cm.exception))
        self.assertIn(str(py), str(cm.exception))
        self.assertIn("delete", str(cm.exception))
        self.assertIn("python-3.15.2", str(cm.exception))

    def test_a_writable_library_file_deep_in_the_interpreter_is_refused(self):
        m = self.install_python()
        py = provide_python(self.store)
        wasm.unseal(py)
        (py / GUEST_PYTHON_LIB / "os.py").write_text("x")
        wasm.seal(py)
        os.chmod(py / GUEST_PYTHON_LIB / "os.py", 0o600)
        with self.assertRaisesRegex(ManifestError, "writable.*os.py"):
            wasm.prepare(self.store, m)

    def test_symlinks_in_the_folders_are_not_followed_by_the_check(self):
        m = self.install_mod()
        outside = self.tmp / "outside.txt"
        outside.write_text("x")
        wasm.unseal(m.root)
        (m.root / "link").symlink_to(outside)  # writable target, but it is a link: not part of the grant
        wasm.seal(m.root)
        self.assertEqual(wasm.prepare(self.store, m).runner, "wasm")

    def test_running_as_root_is_refused_because_permission_bits_do_not_bind_root(self):
        m = self.install_mod()
        with mock.patch("os.geteuid", return_value=0):
            with self.assertRaisesRegex(ManifestError, "root"):
                wasm.prepare(self.store, m)
        self.assertEqual(self.calls(), [])


class CwdGrant(WasmCase):
    """R6: the cwd is read-write only with fs:write:cwd, and refused where it would hand over too much."""

    def setUp(self):
        super().setUp()
        self.user_home = self.tmp / "user"
        (self.user_home / "project").mkdir(parents=True)
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.user_home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.m = self.install_mod(["fs:write:cwd"])

    def assertRefused(self, cwd, reason):
        with self.assertRaises(ManifestError) as cm:
            wasm.prepare(self.store, self.m, cwd=str(cwd))
        self.assertIn(reason, str(cm.exception))
        self.assertIn("dev.dock.wasmmod", str(cm.exception))

    def test_a_cwd_ending_in_a_colon_is_refused_before_anything_is_mounted(self):
        project = self.user_home / "proj:"
        project.mkdir()
        with self.assertRaisesRegex(ManifestError, "ends with ':'"):
            wasm.prepare(self.store, self.m, cwd=str(project))

    def test_a_project_directory_is_granted(self):
        project = (self.user_home / "project").resolve()
        self.assertIn(f"{project}::{project}", wasm.prepare(self.store, self.m, cwd=str(project)).argv)

    def test_the_filesystem_root_is_refused(self):
        self.assertRefused("/", "filesystem root")

    def test_the_home_directory_is_refused(self):
        self.assertRefused(self.user_home, "home directory")

    def test_a_directory_containing_the_dock_home_is_refused(self):
        self.assertRefused(self.store.home.parent, "dock home")

    def test_the_dock_home_itself_and_anything_inside_it_is_refused(self):
        self.assertRefused(self.store.home, "dock home")
        self.assertRefused(self.store.home / "data" / self.m.id, "dock home")

    def test_a_symlink_to_a_refused_directory_is_refused(self):
        link = self.tmp / "innocent"
        link.symlink_to(self.user_home)
        self.assertRefused(link, "home directory")

    def test_without_fs_write_cwd_any_cwd_is_fine_because_nothing_is_granted(self):
        m = self.install_python()  # no permissions
        provide_python(self.store)
        launch = wasm.prepare(self.store, m, cwd="/")
        self.assertNotIn("/::/", launch.argv)
        self.assertEqual(len([a for a in launch.argv if a == "--dir"]), 3)  # plugin, python, data

    def test_the_default_cwd_is_the_process_cwd(self):
        project = (self.user_home / "project").resolve()
        old = os.getcwd()
        os.chdir(project)
        self.addCleanup(os.chdir, old)
        self.assertIn(f"{project}::{project}", wasm.prepare(self.store, self.m).argv)


class Dispatch(WasmCase):
    """R2: a plugin with a wasm entry is run by the WASM runner, whatever else it declares and whatever [isolation] says."""

    def isolation(self, body="enabled = true"):
        with open(self.store.home / "config.toml", "a") as f:
            f.write(f'[isolation]\nruntime = "{DOCKER}"\n{body}\n')

    def test_isolation_prepare_hands_a_wasm_plugin_to_the_wasm_runner(self):
        m = self.install_mod()
        launch = isolation.prepare(self.store, m, cwd="/x")
        self.assertEqual(launch.runner, "wasm")
        self.assertEqual(launch.argv[:2], [str(FAKE), "run"])

    def test_isolation_on_does_not_change_it_and_no_container_runtime_is_called(self):
        self.isolation()
        m = self.install_mod()
        self.assertTrue(isolation.has_container_build(m))  # the container path was there to take
        launch = isolation.prepare(self.store, m, cwd="/x")
        self.assertEqual(launch.runner, "wasm")
        self.assertIsNone(launch.container)
        self.assertEqual(self.docker_calls("build", "run", "image"), [])

    def test_isolation_listing_the_plugin_does_not_change_it_either(self):
        self.isolation('plugins = ["dev.dock.wasmmod"]')
        launch = isolation.prepare(self.store, self.install_mod(), cwd="/x")
        self.assertEqual(launch.runner, "wasm")
        self.assertEqual(self.docker_calls("build", "run", "image"), [])

    def test_a_wasm_plugin_never_starts_natively_when_the_runtime_is_missing(self):
        self.configure(runtime="/nonexistent/wasmtime")
        with self.assertRaisesRegex(ManifestError, "WASM runtime .* not found"):
            isolation.prepare(self.store, self.install_mod(), cwd="/x")

    def test_launch_runner_names_the_other_runners_too(self):
        native = self.store.install(FIXTURES / "probe")
        self.assertEqual(isolation.prepare(self.store, native).runner, "native")
        self.isolation()
        with redirect_stderr(StringIO()):
            self.assertEqual(isolation.prepare(self.store, native).runner, "container")

    def test_a_launch_defaults_to_the_native_runner(self):
        self.assertEqual(isolation.Launch(["x"], {}).runner, "native")


def wait_until(predicate, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


class DockWasm(WasmCase):
    """Real `python3 -m cli` runs against the fake wasmtime: R2, R4, R6, R7."""

    def setUp(self):
        super().setUp()
        self.work = self.tmp / "work dir"  # a space: paths must stay single arguments
        self.work.mkdir()
        self.env = {**os.environ, "DOCK_HOME": str(self.store.home), "PYTHONPATH": str(ROOT)}
        self.m = self.install_mod()

    def dock(self, *args, stdin="", env=None, cwd=None):
        return subprocess.run(
            [sys.executable, "-m", "cli", *args], input=stdin, capture_output=True, text=True,
            env=env or self.env, cwd=cwd or self.work, timeout=60, check=False,
        )

    def spawn(self, *args):
        p = subprocess.Popen([sys.executable, "-m", "cli", *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, env=self.env, cwd=self.work)
        self.addCleanup(p.kill)
        return p

    def isolate(self):
        with open(self.store.home / "config.toml", "a") as f:
            f.write(f'[isolation]\nruntime = "{DOCKER}"\nenabled = true\n')


class OneShot(DockWasm):
    def test_a_one_shot_command_runs_through_wasmtime_and_returns_the_plugins_exit_code(self):
        self.assertEqual(self.dock("code", "0").returncode, 0)
        self.assertEqual(self.dock("code", "3").returncode, 3)
        self.assertEqual(len(self.calls()), 2)

    def test_the_native_and_container_entries_are_never_used_even_with_isolation_enabled(self):
        self.isolate()  # the native entry exits 99 and the container entry 98: a plain 0 proves neither ran
        r = self.dock("code", "0")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(self.docker_calls("build", "run", "image", "rm"), [])

    def test_the_recorded_command_grants_the_plugin_folder_and_data_dir_and_nothing_else(self):
        self.dock("code", "0")
        argv = self.calls()[0]
        data = self.store.data_dir(self.m.id)
        dirs = [argv[i + 1] for i, a in enumerate(argv) if a == "--dir"]
        self.assertEqual(dirs, [f"{self.m.root}::/plugin", f"{data}::{data}"])
        self.assertEqual({a for a in argv[:argv.index("--")] if a.startswith("-")}, {"--dir", "--env"})

    def test_the_python_form_runs_on_the_interpreter_folder(self):
        provide_python(self.store)
        self.store.install(wasmmod_as_python(self.tmp / "pysrc"))
        r = self.dock("code", "0")
        self.assertEqual(r.returncode, 0, r.stderr)
        argv = self.calls()[0]
        self.assertTrue(argv[argv.index("--") + 1].endswith("/python.wasm"))
        self.assertEqual(argv[-2:], ["/plugin/main.py", "--flag"])

    def test_declared_inputs_arrive(self):
        r = self.dock("typed", "hello", "--times", "3")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), {"word": "hello", "times": 3})

    def test_module_args_reach_the_guest(self):
        self.assertEqual(json.loads(self.dock("argv").stdout), ["--flag"])

    def test_a_crashing_plugin_fails_the_command_cleanly(self):
        r = self.dock("crash")
        self.assertEqual(r.returncode, 1)
        self.assertIn("wasmmod", r.stderr)


class GuestEnvironment(DockWasm):
    """R6: the guest sees HOME, the dock variables and declared names that are set; never the rest of the host."""

    def see(self, name, **host):
        r = self.dock("env", name, env={**self.env, **host})
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def test_an_undeclared_host_variable_is_absent(self):
        self.assertEqual(self.see("SECRET_TOKEN"), "SECRET_TOKEN=<unset>")

    def test_a_declared_variable_that_is_set_is_present(self):
        self.assertEqual(self.see("MY_VAR", MY_VAR="hello-123"), "MY_VAR=hello-123")

    def test_a_declared_variable_that_is_not_set_is_absent(self):
        env = {k: v for k, v in self.env.items() if k != "MY_VAR"}
        self.assertEqual(self.dock("env", "MY_VAR", env=env).stdout.strip(), "MY_VAR=<unset>")

    def test_home_and_the_dock_variables_are_set(self):
        self.assertEqual(self.see("HOME"), f"HOME={self.store.data_dir(self.m.id)}/.home")
        self.assertEqual(self.see("DOCK_PLUGIN_ID"), f"DOCK_PLUGIN_ID={self.m.id}")
        self.assertEqual(self.see("DOCK_API_VERSION"), f"DOCK_API_VERSION={API_VERSION}")

    def test_no_secret_value_is_on_the_recorded_command_line(self):
        self.see("MY_VAR", MY_VAR="hello-123")
        self.assertNotIn("hello-123", json.dumps(self.calls()))
        self.assertNotIn("s3cret", json.dumps(self.calls()))


class RuntimeLookup(DockWasm):
    """R4 end to end: a missing runtime or a typo in [wasm] stops the command before anything starts."""

    def test_no_wasmtime_anywhere_exits_1_with_the_message_and_starts_nothing(self):
        (self.store.home / "config.toml").unlink()
        empty = self.tmp / "empty-bin"
        empty.mkdir()
        r = self.dock("code", "0", env={**self.env, "PATH": str(empty)})
        self.assertEqual(r.returncode, 1)
        self.assertIn("dock: WASM runtime 'wasmtime' not found (install wasmtime)", r.stderr)
        self.assertEqual(self.calls(), [])

    def test_a_configured_runtime_that_does_not_exist_exits_1(self):
        self.configure(runtime="/nonexistent/wt")
        r = self.dock("code", "0")
        self.assertEqual((r.returncode, r.stderr.strip()),
                         (1, "dock: WASM runtime '/nonexistent/wt' not found (install wasmtime)"))

    def test_wasmtime_on_path_is_used_without_config(self):
        (self.store.home / "config.toml").unlink()
        r = self.dock("code", "0", env={**self.env, "PATH": f"{FAKE.parent}{os.pathsep}{os.environ['PATH']}"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.calls()), 1)

    def test_a_misspelt_key_exits_1_naming_it_and_starts_nothing(self):
        (self.store.home / "config.toml").write_text(f'[wasm]\nruntme = "{FAKE}"\n')
        r = self.dock("code", "0")
        self.assertEqual(r.returncode, 1)
        self.assertIn("[wasm] has unknown key 'runtme'", r.stderr)
        self.assertEqual(self.calls(), [])

    def test_the_cwd_is_refused_before_anything_starts(self):
        self.install_mod(["fs:write:cwd"])  # replaces the plugin without the permission
        r = self.dock("code", "0", cwd="/")
        self.assertEqual(r.returncode, 1)
        self.assertIn("filesystem root", r.stderr)
        self.assertEqual(self.calls(), [])


class ShellSession(DockWasm):
    """R7: warm reuse, idle reaping and force kill work for a plugin that is not a container."""

    def test_the_shell_reuses_one_warm_process_for_two_commands(self):
        r = self.dock("shell", stdin="code 0\ncode 0\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(self.calls()), 1)

    def test_a_stubborn_command_is_force_killed_after_the_cancel_grace(self):
        self.env["DOCK_CANCEL_GRACE"] = "1"
        p = self.spawn("shell")
        p.stdin.write("stubborn 30\n")
        p.stdin.flush()
        pid_file = self.store.data_root / self.m.id / "pid"
        self.assertTrue(wait_until(pid_file.exists))
        time.sleep(0.3)
        pid = int(pid_file.read_text())
        p.send_signal(2)  # Ctrl-C: the plugin does not see cancel, the grace period ends, it is killed

        def gone():
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            return False

        self.assertTrue(wait_until(gone), "the stuck plugin was not killed")
        self.assertIsNone(p.poll())  # the shell is still up
        p.communicate("", timeout=30)

    def test_an_idle_plugin_is_reaped_while_the_shell_keeps_running(self):
        with open(self.store.home / "config.toml", "a") as f:
            f.write("[shell]\nidle_timeout = 1\n")
        p = self.spawn("shell")
        p.stdin.write("pid\n")
        p.stdin.flush()
        pid_file = self.store.data_root / self.m.id / "pid"
        self.assertTrue(wait_until(pid_file.exists))
        time.sleep(0.3)
        pid = int(pid_file.read_text())
        time.sleep(1.5)
        p.stdin.write("\n")  # a line at the prompt reaps what has been idle too long
        p.stdin.flush()

        def gone():
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            return False

        self.assertTrue(wait_until(gone), "the idle plugin was not stopped")
        self.assertIsNone(p.poll())
        p.communicate("", timeout=30)

    def test_removing_a_sealed_plugin_works_from_the_command_line(self):
        r = self.dock("plugin", "remove", self.m.id)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse((self.store.plugin_dir / self.m.id).exists())


class RunnerLine(DockWasm):
    """R8: `plugin info` shows `runner:`, and shell `status` marks WASM plugins."""

    def info(self, plugin_id):
        r = self.dock("plugin", "info", plugin_id)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def runner(self, plugin_id):
        return next(l for l in self.info(plugin_id).splitlines() if l.startswith("runner:"))

    def test_a_module_plugin_shows_wasm_and_the_runtime(self):
        self.assertEqual(self.runner(self.m.id), "runner:      wasm (wasmtime)")

    def test_the_runtime_is_named_without_its_folder(self):
        self.assertEqual(wasm.runtime_label(str(FAKE)), "wasmtime")
        self.configure(runtime="/opt/wt/bin/mywt")
        self.assertEqual(self.runner(self.m.id), "runner:      wasm (mywt)")

    def test_a_python_plugin_shows_the_python_version_too(self):
        self.store.install(wasmmod_as_python(self.tmp / "pysrc"))
        self.assertEqual(self.runner(self.m.id), "runner:      wasm (wasmtime, python 3.15)")

    def test_the_line_does_not_need_the_runtime_to_exist(self):
        self.configure(runtime="/nonexistent/wt")
        self.assertEqual(self.runner(self.m.id), "runner:      wasm (wt)")

    def test_a_native_plugin_shows_native(self):
        self.store.install(FIXTURES / "probe")
        self.assertEqual(self.runner("dev.dock.probe"), "runner:      native")

    def test_an_isolated_plugin_shows_container_and_its_runtime(self):
        self.store.install(FIXTURES / "probe")
        self.isolate()
        self.assertEqual(self.runner("dev.dock.probe"), "runner:      container (docker)")

    def test_a_wasm_plugin_stays_wasm_when_isolation_is_on(self):
        self.isolate()
        self.assertEqual(self.runner(self.m.id), "runner:      wasm (wasmtime)")

    def test_the_isolation_line_does_not_claim_a_container_for_a_wasm_plugin(self):
        self.isolate()
        line = next(l for l in self.info(self.m.id).splitlines() if l.startswith("isolation:"))
        self.assertNotIn("docker", line)

    def test_a_broken_wasm_config_is_reported_on_the_line_not_raised(self):
        (self.store.home / "config.toml").write_text('[wasm]\nruntme = "x"\n')
        self.assertIn("unknown key 'runtme'", self.runner(self.m.id))

    def test_status_appends_wasm_for_a_running_wasm_plugin(self):
        r = self.dock("shell", stdin="code 0\nstatus\n")
        line = next(l.replace("dock> ", "") for l in r.stdout.splitlines() if "running" in l)
        self.assertTrue(line.startswith(self.m.id) and line.endswith("   wasm"), line)

    def test_status_has_no_wasm_mark_for_a_native_plugin(self):
        self.store.install(FIXTURES / "probe")
        r = self.dock("shell", stdin="dev.dock.probe:code 0\nstatus\n")
        line = next(l.replace("dock> ", "") for l in r.stdout.splitlines() if "running" in l)
        self.assertTrue(line.startswith("dev.dock.probe"), line)
        self.assertNotIn("wasm", line)


def python_zip(members=None) -> bytes:
    """A zip shaped like the release archive: python.wasm, LICENSE and the standard library folder."""
    members = members or {
        "python.wasm": b"\0asm fake interpreter", "LICENSE": b"license",
        f"{GUEST_PYTHON_LIB}/os.py": b"# stdlib\n", f"{GUEST_PYTHON_LIB}/json/__init__.py": b"# json\n"}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


def override(url: str, body: bytes) -> dict[str, str]:
    return {"DOCK_TEST_WASM_PYTHON_URL": url, "DOCK_TEST_WASM_PYTHON_SHA256": hashlib.sha256(body).hexdigest()}


def unreachable_override() -> dict[str, str]:
    """A URL nothing listens on (a port that was just free) with some hash."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return {"DOCK_TEST_WASM_PYTHON_URL": f"http://127.0.0.1:{port}/python.zip", "DOCK_TEST_WASM_PYTHON_SHA256": "0" * 64}


class ZipServer:
    """A local HTTP server in a thread that serves one body and counts the requests it gets."""

    def __init__(self, body: bytes, status: int = 200):
        self.hits = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                outer.hits += 1
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/python.zip"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join()


def test_root(store: Store, body: bytes) -> Path:
    """Where an interpreter fetched through the test override lives: apart from the pinned one."""
    return wasm.python_home(store, "3.15").with_name(f"python-3.15.2-test-{hashlib.sha256(body).hexdigest()[:12]}")


class EnsurePython(WasmCase):
    """R5, NFR2: the interpreter is fetched once, checked against the pinned hash, and installed atomically."""

    def fetch(self, body: bytes, server: ZipServer, **kw):
        stderr = StringIO()
        with mock.patch.dict(os.environ, override(server.url, kw.get("hashed", body))), redirect_stderr(stderr):
            return wasm.ensure_python(self.store, "3.15"), stderr.getvalue()

    def wasm_dir_entries(self) -> list[str]:
        d = self.store.home / "wasm"
        return sorted(p.name for p in d.iterdir()) if d.exists() else []

    def test_the_first_use_downloads_verifies_and_installs_a_sealed_interpreter(self):
        body = python_zip()
        with ZipServer(body) as srv:
            root, _ = self.fetch(body, srv)
        self.assertEqual(root, test_root(self.store, body))
        self.assertEqual((root / "python.wasm").read_bytes(), b"\0asm fake interpreter")
        self.assertTrue((root / GUEST_PYTHON_LIB / "json" / "__init__.py").is_file())
        self.assertTrue((root / ".verified").is_file())
        self.assertFalse(any(os.access(p, os.W_OK) for p in (root, root / "python.wasm", root / GUEST_PYTHON_LIB)))
        self.assertEqual(self.wasm_dir_entries(), [root.name])

    def test_it_says_on_stderr_what_it_fetches(self):
        body = python_zip()
        with ZipServer(body) as srv:
            _, err = self.fetch(body, srv)
        self.assertEqual(err, "dock: fetching python 3.15.2 for WASI (15.5 MB) \u2026\n")

    def test_the_second_use_does_not_touch_the_server_or_print_anything(self):
        body = python_zip()
        with ZipServer(body) as srv:
            first, _ = self.fetch(body, srv)
            second, err = self.fetch(body, srv)
            self.assertEqual(srv.hits, 1)
        self.assertEqual(first, second)
        self.assertEqual(err, "")

    def test_a_tampered_download_is_refused_and_nothing_is_installed(self):
        good = python_zip()
        evil = python_zip({"python.wasm": b"evil", f"{GUEST_PYTHON_LIB}/os.py": b"# evil\n"})
        with ZipServer(evil) as srv:
            with self.assertRaisesRegex(ManifestError, r"python 3\.15\.2 for WASI: checksum mismatch"):
                self.fetch(good, srv, hashed=good)
        self.assertEqual(self.wasm_dir_entries(), [])
        self.assertFalse(wasm.python_home(self.store, "3.15").exists())

    def test_an_unreachable_server_is_refused_with_the_reason_and_nothing_is_left(self):
        with mock.patch.dict(os.environ, unreachable_override()), redirect_stderr(StringIO()):
            with self.assertRaisesRegex(ManifestError, r"python 3\.15\.2 for WASI: .+"):
                wasm.ensure_python(self.store, "3.15")
        self.assertEqual(self.wasm_dir_entries(), [])

    def test_an_http_error_is_refused(self):
        body = python_zip()
        with ZipServer(body, status=404) as srv:
            with self.assertRaisesRegex(ManifestError, r"python 3\.15\.2 for WASI: .*404"):
                self.fetch(body, srv)
        self.assertEqual(self.wasm_dir_entries(), [])

    def test_a_download_that_is_not_a_zip_is_refused_even_with_a_matching_hash(self):
        body = b"definitely not a zip"
        with ZipServer(body) as srv:
            with self.assertRaisesRegex(ManifestError, r"python 3\.15\.2 for WASI: .+"):
                self.fetch(body, srv)
        self.assertEqual(self.wasm_dir_entries(), [])

    def test_an_archive_without_the_interpreter_or_the_library_is_refused(self):
        for name, members in (("no python.wasm", {f"{GUEST_PYTHON_LIB}/os.py": b"x"}),
                              ("no library", {"python.wasm": b"x"})):
            with self.subTest(name):
                body = python_zip(members)
                with ZipServer(body) as srv:
                    with self.assertRaisesRegex(ManifestError, r"python 3\.15\.2 for WASI: .*(python\.wasm|lib/python3\.15)"):
                        self.fetch(body, srv)
                self.assertEqual(self.wasm_dir_entries(), [])

    def test_unsafe_archive_members_are_refused_and_nothing_is_written(self):
        outside = self.tmp / "escaped"
        link = zipfile.ZipInfo("lib/link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        cases = {
            "absolute": {"/etc/escaped": b"x"},
            "parent": {"../escaped": b"x"},
            "nested parent": {"lib/../../escaped": b"x"},
            "backslash parent": {"..\\escaped": b"x"},
        }
        bodies = {name: python_zip({**members, "python.wasm": b"x"}) for name, members in cases.items()}
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("python.wasm", b"x")
            z.writestr(f"{GUEST_PYTHON_LIB}/os.py", b"x")
            z.writestr(link, "/etc")
        bodies["symlink"] = buf.getvalue()
        for name, body in bodies.items():
            with self.subTest(name), ZipServer(body) as srv:
                with self.assertRaisesRegex(ManifestError, r"python 3\.15\.2 for WASI: .*unsafe"):
                    self.fetch(body, srv)
                self.assertEqual(self.wasm_dir_entries(), [])
                self.assertFalse(outside.exists())

    def test_an_interrupted_download_leaves_no_partial_files(self):
        class Dying:
            def __enter__(self): return self
            def __exit__(self, *exc): return False
            def __init__(self): self.sent = False
            def read(self, n=-1):
                if self.sent:
                    raise KeyboardInterrupt
                self.sent = True
                return b"PK partial"

        with mock.patch.dict(os.environ, unreachable_override()), redirect_stderr(StringIO()):
            with mock.patch("urllib.request.urlopen", return_value=Dying()):
                with self.assertRaises(KeyboardInterrupt):
                    wasm.ensure_python(self.store, "3.15")
        self.assertEqual(self.wasm_dir_entries(), [])

    def test_an_interrupt_after_extraction_leaves_no_files_and_installs_nothing(self):
        body = python_zip()
        with ZipServer(body) as srv, mock.patch("os.replace", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.fetch(body, srv)
        self.assertEqual(self.wasm_dir_entries(), [])

    def test_a_process_that_loses_the_race_discards_its_copy_and_uses_the_winner(self):
        body = python_zip()
        root = test_root(self.store, body)
        real_replace = os.replace

        def other_process_wins(src, dst):
            Path(dst).mkdir()  # the other process finished first: a full folder is already at the target
            (Path(dst) / ".verified").write_text(hashlib.sha256(body).hexdigest())
            (Path(dst) / "marker").write_text("winner")
            return real_replace(src, dst)  # ours now fails: a rename onto a non-empty folder

        with ZipServer(body) as srv, mock.patch("os.replace", side_effect=other_process_wins):
            got, _ = self.fetch(body, srv)
        self.assertEqual(got, root)
        self.assertEqual((root / "marker").read_text(), "winner")
        self.assertEqual(self.wasm_dir_entries(), [root.name])

    def test_the_overrides_only_take_effect_together(self):
        pin = wasm.PYTHONS["3.15"]
        for env in ({"DOCK_TEST_WASM_PYTHON_URL": "http://x/y.zip"}, {"DOCK_TEST_WASM_PYTHON_SHA256": "ab" * 32}, {}):
            with self.subTest(env=env), mock.patch.dict(os.environ, env):
                for k in ("DOCK_TEST_WASM_PYTHON_URL", "DOCK_TEST_WASM_PYTHON_SHA256"):
                    if k not in env:
                        os.environ.pop(k, None)
                self.assertEqual(wasm.effective_pin("3.15"), pin)
        with mock.patch.dict(os.environ, {"DOCK_TEST_WASM_PYTHON_URL": "http://x/y.zip",
                                          "DOCK_TEST_WASM_PYTHON_SHA256": "AB" * 32}):
            got = wasm.effective_pin("3.15")
        self.assertEqual((got.url, got.sha256, got.version, got.lib), ("http://x/y.zip", "ab" * 32, pin.version, pin.lib))

    def test_a_verified_folder_is_used_even_when_the_download_would_fail(self):
        py = provide_python(self.store)
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("fetched")):
            self.assertEqual(wasm.ensure_python(self.store, "3.15"), py)

    def test_the_fetched_interpreter_is_what_prepare_maps(self):
        m = self.install_python()
        body = python_zip()
        with ZipServer(body) as srv, mock.patch.dict(os.environ, override(srv.url, body)), redirect_stderr(StringIO()):
            launch = wasm.prepare(self.store, m)
        py = test_root(self.store, body)
        self.assertIn(f"{py}::/python", launch.argv)

    def test_the_sealed_interpreter_can_be_removed_by_dock_after_unseal(self):
        body = python_zip()
        with ZipServer(body) as srv:
            root, _ = self.fetch(body, srv)
        wasm.unseal(root)
        shutil.rmtree(root)
        self.assertFalse(root.exists())


class TrustedInterpreter(WasmCase):
    """Review 6: `.verified` records which hash was checked, and a test override never lands in the production folder."""

    def pinned(self, body: bytes, server: ZipServer):
        """Make the pin itself (not the test override) the fake archive: its folder is the production one."""
        pin = wasm.PYTHONS["3.15"]
        patcher = mock.patch.dict(wasm.PYTHONS, {"3.15": replace(pin, url=server.url, sha256=hashlib.sha256(body).hexdigest())})
        patcher.start()
        self.addCleanup(patcher.stop)

    def ensure(self):
        with redirect_stderr(StringIO()):
            return wasm.ensure_python(self.store, "3.15")

    def test_the_marker_holds_the_hash_that_was_checked(self):
        body = python_zip()
        with ZipServer(body) as srv, mock.patch.dict(os.environ, override(srv.url, body)), redirect_stderr(StringIO()):
            root = wasm.ensure_python(self.store, "3.15")
        self.assertEqual((root / ".verified").read_text(), hashlib.sha256(body).hexdigest())

    def test_the_test_override_installs_beside_the_production_folder_not_into_it(self):
        body = python_zip()
        with ZipServer(body) as srv, mock.patch.dict(os.environ, override(srv.url, body)), redirect_stderr(StringIO()):
            root = wasm.ensure_python(self.store, "3.15")
        self.assertNotEqual(root, wasm.python_home(self.store, "3.15"))
        self.assertTrue(root.name.startswith("python-3.15.2-test-"))
        self.assertFalse(wasm.python_home(self.store, "3.15").exists())

    def test_an_override_with_the_pinned_hash_is_the_production_folder(self):
        pin = wasm.PYTHONS["3.15"]
        with mock.patch.dict(os.environ, {"DOCK_TEST_WASM_PYTHON_URL": "http://x/y.zip",
                                          "DOCK_TEST_WASM_PYTHON_SHA256": pin.sha256}):
            self.assertEqual(wasm.effective_root(self.store, "3.15"), wasm.python_home(self.store, "3.15"))

    def test_a_folder_installed_through_the_override_is_not_used_without_it(self):
        body = python_zip()
        with ZipServer(body) as srv, mock.patch.dict(os.environ, override(srv.url, body)), redirect_stderr(StringIO()):
            wasm.ensure_python(self.store, "3.15")
        # no override now: the pinned folder is wanted, and it is not there, so a fetch of the real URL starts
        with mock.patch.dict(os.environ, unreachable_override()), mock.patch.dict(
                wasm.PYTHONS, {"3.15": replace(wasm.PYTHONS["3.15"], url=unreachable_override()["DOCK_TEST_WASM_PYTHON_URL"])}):
            os.environ.pop("DOCK_TEST_WASM_PYTHON_URL")
            os.environ.pop("DOCK_TEST_WASM_PYTHON_SHA256")
            with redirect_stderr(StringIO()), self.assertRaisesRegex(ManifestError, "python 3.15.2 for WASI"):
                wasm.ensure_python(self.store, "3.15")

    def test_a_marker_without_the_expected_hash_is_not_trusted_and_the_folder_is_fetched_again(self):
        good = python_zip()
        stale = provide_python(self.store)
        wasm.unseal(stale)
        (stale / ".verified").write_text("")  # what an older dock wrote
        (stale / "leftover").write_text("old")
        wasm.seal(stale)
        with ZipServer(good) as srv:
            self.pinned(good, srv)
            root = self.ensure()
            self.assertEqual(srv.hits, 1)
        self.assertEqual(root, stale)
        self.assertEqual((root / "python.wasm").read_bytes(), b"\0asm fake interpreter")
        self.assertFalse((root / "leftover").exists())
        self.assertEqual((root / ".verified").read_text(), hashlib.sha256(good).hexdigest())
        self.assertFalse(os.access(root, os.W_OK))
        self.assertEqual([p.name for p in (self.store.home / "wasm").iterdir()], [root.name])

    def test_a_marker_for_another_hash_is_not_trusted_either(self):
        good = python_zip()
        root = provide_python(self.store)  # marked with the real pinned hash
        with ZipServer(good) as srv:
            self.pinned(good, srv)  # the pin now is something else
            self.ensure()
            self.assertEqual(srv.hits, 1)
        self.assertEqual((root / ".verified").read_text(), hashlib.sha256(good).hexdigest())

    def test_a_matching_marker_means_no_fetch(self):
        good = python_zip()
        with ZipServer(good) as srv:
            self.pinned(good, srv)
            self.ensure()
            self.ensure()
            self.assertEqual(srv.hits, 1)

    def test_a_failed_refetch_leaves_the_old_folder_alone(self):
        stale = provide_python(self.store)
        wasm.unseal(stale)
        (stale / ".verified").write_text("")
        wasm.seal(stale)
        with mock.patch.dict(os.environ, unreachable_override()):
            os.environ.pop("DOCK_TEST_WASM_PYTHON_URL")
            os.environ.pop("DOCK_TEST_WASM_PYTHON_SHA256")
            with mock.patch.dict(wasm.PYTHONS, {"3.15": replace(wasm.PYTHONS["3.15"], url=unreachable_override()["DOCK_TEST_WASM_PYTHON_URL"])}):
                with redirect_stderr(StringIO()), self.assertRaisesRegex(ManifestError, "python 3.15.2 for WASI"):
                    wasm.ensure_python(self.store, "3.15")
        self.assertTrue((stale / "python.wasm").exists())


class DownloadCap(WasmCase):
    """Review 7: the download stops at a size cap, however the server behaves."""

    def entries(self):
        d = self.store.home / "wasm"
        return sorted(p.name for p in d.iterdir()) if d.exists() else []

    def test_a_body_over_the_cap_is_refused_even_when_its_hash_matches(self):
        body = python_zip({"python.wasm": b"x" * 5000, f"{GUEST_PYTHON_LIB}/os.py": b"x"})
        with ZipServer(body) as srv, mock.patch.dict(os.environ, override(srv.url, body)), \
                mock.patch.object(wasm, "MAX_DOWNLOAD_BYTES", 1000), redirect_stderr(StringIO()):
            with self.assertRaisesRegex(ManifestError, r"python 3\.15\.2 for WASI: .*larger than"):
                wasm.ensure_python(self.store, "3.15")
        self.assertEqual(self.entries(), [])

    def test_a_body_exactly_at_the_cap_is_accepted(self):
        body = python_zip()
        with ZipServer(body) as srv, mock.patch.dict(os.environ, override(srv.url, body)), \
                mock.patch.object(wasm, "MAX_DOWNLOAD_BYTES", len(body)), redirect_stderr(StringIO()):
            wasm.ensure_python(self.store, "3.15")

    def test_an_endless_body_is_cut_off_soon_after_the_cap(self):
        class Endless:
            def __init__(self): self.read_bytes = 0
            def __enter__(self): return self
            def __exit__(self, *exc): return False
            def read(self, n=-1):
                n = 65536 if n is None or n < 0 else n
                self.read_bytes += n
                if self.read_bytes > 50 * 1024 * 1024:
                    raise AssertionError("read far past the cap")
                return b"x" * n

        endless = Endless()
        with mock.patch.dict(os.environ, unreachable_override()), mock.patch.object(wasm, "MAX_DOWNLOAD_BYTES", 200_000), \
                mock.patch("urllib.request.urlopen", return_value=endless), redirect_stderr(StringIO()):
            with self.assertRaisesRegex(ManifestError, "larger than"):
                wasm.ensure_python(self.store, "3.15")
        self.assertLessEqual(endless.read_bytes, 200_000 + 65536)
        self.assertEqual(self.entries(), [])

    def test_the_default_cap_is_64_mib(self):
        self.assertEqual(wasm.MAX_DOWNLOAD_BYTES, 64 * 1024 * 1024)


class StaleTempFiles(WasmCase):
    """Nit: files a killed dock left in $DOCK_HOME/wasm are swept, but never a live process's."""

    def dead_pid(self) -> int:
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        return p.pid

    def setUp(self):
        super().setUp()
        self.base = self.store.home / "wasm"
        self.base.mkdir(parents=True)

    def fetch(self):
        body = python_zip()
        with ZipServer(body) as srv, mock.patch.dict(os.environ, override(srv.url, body)), redirect_stderr(StringIO()):
            return wasm.ensure_python(self.store, "3.15")

    def test_leftovers_of_dead_processes_are_removed_sealed_folders_included(self):
        dead = self.dead_pid()
        part = self.base / f"python-3.15.2.zip.part-{dead}"
        part.write_bytes(b"partial")
        tmp = self.base / f"python-3.15.2.tmp-{dead}"
        (tmp / "lib").mkdir(parents=True)
        (tmp / "lib" / "f").write_text("x")
        wasm.seal(tmp)
        root = self.fetch()
        self.assertEqual(sorted(p.name for p in self.base.iterdir()), [root.name])

    def test_leftovers_of_a_live_process_are_kept(self):
        live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(live.kill)
        part = self.base / f"python-3.15.2.zip.part-{live.pid}"
        part.write_bytes(b"in progress")
        self.fetch()
        self.assertTrue(part.exists())

    def test_other_files_are_never_touched(self):
        (self.base / "notes.txt").write_text("mine")
        (self.base / "python-3.15.2.zip.part-notapid").write_text("x")
        self.fetch()
        self.assertTrue((self.base / "notes.txt").exists())
        self.assertTrue((self.base / "python-3.15.2.zip.part-notapid").exists())


class WasmTableLookalikes(unittest.TestCase):
    """Nit: a misspelt [wasm] table must not silently mean 'use wasmtime from PATH'."""

    def config(self, body: str):
        home = temp_dir("dock-wasm-home-")
        (home / "config.toml").write_text(body)
        return wasm.config(Store(home))

    def test_a_misspelt_or_miscased_table_is_refused(self):
        for name in ("Wasm", "WASM", "wasmm", "wasn", "wassm"):
            with self.subTest(name=name):
                with self.assertRaisesRegex(ManifestError, rf"\[{name}\] is not a known table; did you mean \[wasm\]"):
                    self.config(f'[{name}]\nruntime = "/x/wt"\n')

    def test_a_top_level_runtime_key_is_refused(self):
        with self.assertRaisesRegex(ManifestError, "'runtime' is at the top level; .*\\[wasm\\] table"):
            self.config('runtime = "/x/wt"\n')

    def test_the_other_tables_are_not_mistaken_for_it(self):
        self.assertEqual(self.config('[isolation]\nenabled = false\n[shell]\nidle_timeout = 5\n[providers]\nx = "y"\n'),
                         "wasmtime")

    def test_a_real_wasm_table_next_to_a_lookalike_is_fine(self):
        self.assertEqual(self.config('[wasm]\nruntime = "/x/wt"\n[shell]\nidle_timeout = 5\n'), "/x/wt")


class FakeWasmtime(unittest.TestCase):
    """The fixture itself: what the tests below rely on it to do."""

    def setUp(self):
        self.tmp = temp_dir("dock-wasm-fake-")
        self.log = self.tmp / "calls"
        self.guest = self.tmp / "guest folder"
        self.guest.mkdir()
        (self.guest / "show.py").write_text(
            "import json, os, sys\nprint(json.dumps({'argv': sys.argv[1:], 'env': dict(os.environ)}))\n")

    def run_fake(self, *args, env=None):
        return subprocess.run([str(FAKE), *args], capture_output=True, text=True, check=False, timeout=30,
                              env={"PATH": os.environ["PATH"], "FAKE_WASMTIME_LOG": str(self.log), **(env or {})})

    def shown(self, r):
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_it_logs_its_argv_one_json_list_per_call(self):
        self.run_fake("run", "--", str(self.guest / "show.py"))
        self.run_fake("run", "--", str(self.guest / "show.py"), "a")
        rows = [json.loads(l) for l in self.log.read_text().splitlines()]
        self.assertEqual(rows, [["run", "--", str(self.guest / "show.py")],
                                ["run", "--", str(self.guest / "show.py"), "a"]])

    def test_a_module_is_run_with_its_args(self):
        shown = self.shown(self.run_fake("run", "--", str(self.guest / "show.py"), "x", "y"))
        self.assertEqual(shown["argv"], ["x", "y"])

    def test_a_python_wasm_runs_the_first_argument_with_guest_paths_mapped_to_host_folders(self):
        r = self.run_fake("run", "--dir", f"{self.guest}::/plugin", "--env", "PYTHONHOME=/python", "--",
                          "/anywhere/python.wasm", "/plugin/show.py", "/plugin/data.txt", "/other")
        self.assertEqual(self.shown(r)["argv"], [f"{self.guest}/data.txt", "/other"])

    def test_env_is_only_the_env_options_plus_path_and_value_free_names_come_from_the_client(self):
        r = self.run_fake("run", "--env", "A=1", "--env", "DECLARED", "--env", "UNSET", "--env", "PYTHONHOME=/python",
                          "--", str(self.guest / "show.py"), env={"DECLARED": "yes", "SECRET_TOKEN": "s3"})
        env = self.shown(r)["env"]
        for added_by_the_os_or_python in ("LC_CTYPE", "__CF_USER_TEXT_ENCODING"):
            env.pop(added_by_the_os_or_python, None)
        self.assertEqual(env, {"A": "1", "DECLARED": "yes", "PATH": os.environ["PATH"]})

    def test_any_other_option_is_refused_so_a_network_flag_cannot_slip_in(self):
        for extra in (["-S", "tcp"], ["-S", "inherit-network"], ["--wasi", "tcp"]):
            with self.subTest(extra=extra):
                r = self.run_fake("run", *extra, "--", str(self.guest / "show.py"))
                self.assertEqual(r.returncode, 125)
                self.assertIn("unsupported option", r.stderr)


if __name__ == "__main__":
    unittest.main()
