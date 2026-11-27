"""Plugin discovery without installing: [catalog] in config.toml, keywords, search, hint, install/info by id.

No test here needs a container runtime (the NoRuntime class proves dock never calls one).
"""

import json
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
sys.path.insert(0, str(ROOT / "tests"))

import dockenv
from dock_host import catalog
from dock_host.manifest import ManifestError
from dock_host.store import Store

REAL_BUILTIN_DIR = catalog.builtin_dir  # before any test patches it


def make_plugin(
    parent,
    name,
    plugin_id,
    version="0.1.0",
    commands=(("hello", "Greet someone", ["greet"]),),
    keywords_raw=None,
):
    """A plugin folder `parent/name`: plugin.toml and a one-line Dockerfile. commands: (name, summary, keywords)."""
    root = Path(parent) / name
    root.mkdir(parents=True, exist_ok=True)
    text = (
        f'id = "{plugin_id}"\nversion = "{version}"\napi_version = "2"\ndescription = "A python sample"\n\n'
        '[run]\ncontainer = { cmd = "python3", args = ["/plugin/main.py"] }\n'
    )
    for cname, summary, kws in commands:
        text += f'\n[[commands]]\nname = "{cname}"\nsummary = "{summary}"\n'
        if keywords_raw is not None:
            text += f"keywords = {keywords_raw}\n"
        elif kws:
            text += "keywords = [" + ", ".join(json.dumps(k) for k in kws) + "]\n"
    (root / "plugin.toml").write_text(text)
    (root / "Dockerfile").write_text("FROM scratch\n")
    return root


def greeter(parent):
    """A two-command plugin folder `parent/greeter` (dev.dock.greeter 0.2.0) with keywords on both commands."""
    return make_plugin(
        parent,
        "greeter",
        "dev.dock.greeter",
        "0.2.0",
        commands=(
            ("hello", "Greet someone: hello [name]", ["greet", "welcome", "name"]),
            ("count", "Count with a progress bar: count [n]", ["progress", "number", "demo"]),
        ),
    )


class TmpCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dock-catalog-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.cat = self.tmp / "cat"
        self.cat.mkdir()
        patcher = mock.patch.object(
            catalog, "builtin_dir", return_value=None
        )  # the repo's plugins are not these tests' catalog
        patcher.start()
        self.addCleanup(patcher.stop)

    def config(self, text):
        (self.home / "config.toml").write_text(text)

    def catalog_config(self, *folders):
        self.config("[catalog]\npaths = [" + ", ".join(json.dumps(str(f)) for f in folders) + "]\n")

    def store(self):
        return Store(self.home)


class CatalogConfig(TmpCase):
    def paths_error(self, text):
        self.config(text)
        with self.assertRaises(ManifestError) as cm:
            catalog.paths(self.store())
        return str(cm.exception)

    def test_bad_catalog_config_is_refused(self):  # AC1
        where = self.home / "config.toml"
        cases = [
            (
                "relative path",
                '[catalog]\npaths = ["relative/dir"]\n',
                f"{where}: [catalog] paths entry 'relative/dir' must be an absolute path or start with ~",
            ),
            (
                "unknown key",
                "[catalog]\nfoo = 1\n",
                f"{where}: [catalog] has unknown key 'foo' (known: paths)",
            ),
            ("catalog must be a table", 'catalog = "x"\n', f"{where}: [catalog] must be a table"),
            (
                "top-level paths",
                'paths = ["/x"]\n',
                f"{where}: 'paths' is at the top level; catalog settings belong in a [catalog] table",
            ),
        ]
        for raw in ('"x"', "[1]", "5"):
            cases.append(
                (
                    f"paths = {raw}",
                    f"[catalog]\npaths = {raw}\n",
                    f"{where}: [catalog] paths must be a list of folder paths",
                )
            )
        for name in ("catalgo", "catlog", "catalogs"):
            cases.append(
                (
                    f"misspelt [{name}]",
                    f"[{name}]\npaths = []\n",
                    f"{where}: [{name}] is not a known table; did you mean [catalog]?",
                )
            )
        for label, text, expected in cases:
            with self.subTest(label):
                self.assertEqual(self.paths_error(text), expected)


class CatalogScan(TmpCase):
    def load(self):
        warnings = []
        found = catalog.load(self.store(), warnings.append)
        return found, warnings

    def test_entry_that_is_a_file_warns(self):
        f = self.tmp / "file"
        f.write_text("x")
        self.catalog_config(f)
        found, warnings = self.load()
        self.assertEqual(found, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn(str(f), warnings[0])
        self.assertIn("skipped", warnings[0])

    def test_broken_plugin_is_skipped_with_a_warning_naming_its_folder_and_error(self):
        with self.subTest("bad keywords"):
            cat = self.tmp / "cat-keywords"
            bad = make_plugin(cat, "bad", "dev.t.bad", commands=(("go", "Go", []),), keywords_raw='"greet"')
            make_plugin(cat, "good", "dev.t.good")
            self.catalog_config(cat)
            found, warnings = self.load()
            self.assertEqual([a.manifest.id for a in found], ["dev.t.good"])
            self.assertEqual(len(warnings), 1)
            self.assertTrue(warnings[0].startswith(f"catalog plugin {bad} skipped: "), warnings[0])
            self.assertIn("go: keywords must be a list of non-empty strings", warnings[0])
        with self.subTest("removed feature"):
            cat = self.tmp / "cat-removed"
            old = make_plugin(cat, "old", "dev.t.old")
            text = (old / "plugin.toml").read_text().replace("container = {", "wasm = {")
            (old / "plugin.toml").write_text(text)
            self.catalog_config(cat)
            found, warnings = self.load()
            self.assertEqual(found, [])
            self.assertEqual(len(warnings), 1)
            self.assertIn("no longer supported", warnings[0])


class SearchUnit(TmpCase):
    def search(self, *words):
        warnings = []
        found = catalog.search(self.store(), list(words), warnings.append)
        return found, warnings

    def test_installed_and_in_catalog_is_one_line_and_names_a_different_catalog_version(self):
        self.store().install(make_plugin(self.tmp, "i", "dev.t.a", "0.1.0"))
        with self.subTest("catalog has other versions"):
            cat = self.tmp / "cat-other"
            make_plugin(cat, "a", "dev.t.a", "0.2.0")
            make_plugin(cat, "b", "dev.t.a", "0.3.0")
            self.catalog_config(cat)
            found, _ = self.search("hello")
            self.assertEqual(len(found), 1)
            self.assertTrue(found[0].installed)
            self.assertEqual(found[0].version, "0.1.0")
            self.assertEqual(found[0].catalog_version, "0.2.0, 0.3.0")
            self.assertIsNone(found[0].install)
        with self.subTest("same version in catalog adds no catalog version"):
            cat = self.tmp / "cat-same"
            make_plugin(cat, "a", "dev.t.a", "0.1.0")
            self.catalog_config(cat)
            found, _ = self.search("hello")
            self.assertEqual(len(found), 1)
            self.assertIsNone(found[0].catalog_version)


FAKE_RUNTIME = "#!/bin/sh\necho \"$@\" >> '{log}'\nexit 1\n"


class CliCase(TmpCase):
    """`python -m src` in a throwaway DOCK_HOME whose container runtime is a script that records its calls."""

    def setUp(self):
        super().setUp()
        self.log = self.tmp / "runtime-calls"
        self.runtime = self.tmp / "docker"
        self.runtime.write_text(FAKE_RUNTIME.format(log=self.log))
        self.runtime.chmod(0o755)
        self.config(f'[container]\nruntime = "{self.runtime}"\n')
        self.env = {
            **os.environ,
            "DOCK_HOME": str(self.home),
            "PYTHONPATH": str(ROOT),
            "DOCK_NO_READLINE": "1",
            "HOME": str(self.tmp),
        }
        self.hello = greeter(self.cat)
        self.outside = greeter(
            self.tmp / "outside"
        )  # the same plugin, in a folder that is not in the catalog
        self.catalog_config(self.cat)

    def catalog_config(self, *folders):
        dockenv.add_config(
            self.home, "[catalog]\npaths = [" + ", ".join(json.dumps(str(f)) for f in folders) + "]\n"
        )

    def dock(self, *args, stdin="", env=None):
        return subprocess.run(
            [sys.executable, "-m", "src", *args],
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
            env=env or self.env,
            cwd=self.tmp,
            timeout=60,
            **dockenv.SIGINT_DEFAULT,
        )

    def runtime_calls(self):
        return self.log.read_text() if self.log.exists() else ""

    def index(self):
        return self.store().index()


class Hint(CliCase):
    def test_installed_command_never_reads_the_catalog(self):  # AC6
        self.store().install(self.hello)
        store = self.store()
        with mock.patch.object(catalog, "load", side_effect=AssertionError("catalog read")):
            manifest, command = store.resolve("hello")
        self.assertEqual((manifest.id, command), ("dev.dock.greeter", "hello"))

    def test_id_in_two_folders_is_named_by_folder(self):
        other = make_plugin(self.cat, "other", "dev.dock.greeter")
        text = catalog.hint(self.store(), "hello", lambda m: None)
        self.assertIn(f"dock plugin install {other}", text)
        self.assertIn(f"dock plugin install {self.hello}", text)


class NoRuntime(CliCase):
    """search, the hint and `plugin info <catalog id>` never call the container runtime (the fake one logs calls)."""


class BuiltIn(TmpCase):
    """The <repo>/plugins folder is a catalog source with no config; [catalog] paths come first."""

    def setUp(self):
        super().setUp()
        self.builtin = self.tmp / "builtin"
        make_plugin(self.builtin, "g", "dev.t.g", "0.1.0", commands=(("hi", "Say hi", ["greet"]),))
        patcher = mock.patch.object(catalog, "builtin_dir", return_value=self.builtin)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_search_marks_a_builtin_not_installed_with_the_install_hint(
        self,
    ):  # catches: search ignoring builtins
        found = catalog.search(self.store(), ["greet"])
        self.assertEqual(
            [(m.plugin, m.installed, m.install) for m in found],
            [("dev.t.g", False, "dock plugin install dev.t.g")],
        )

    def test_builtin_dir_needs_the_checkout_marker(self):  # catches: trusting any parents[3]/plugins
        (self.tmp / "other" / "plugins").mkdir(parents=True)
        here = self.tmp / "other" / "packages" / "host" / "dock_host" / "catalog.py"
        here.parent.mkdir(parents=True)
        here.write_text("")
        self.assertEqual(REAL_BUILTIN_DIR(here), self.tmp / "other" / "plugins")
        lone = self.tmp / "lone" / "a" / "b" / "dock_host" / "catalog.py"
        (self.tmp / "lone" / "plugins").mkdir(parents=True)
        lone.parent.mkdir(parents=True)
        lone.write_text("")
        self.assertIsNone(REAL_BUILTIN_DIR(lone))
