"""Plugin discovery without installing: [catalog] in config.toml, keywords, search, hint, install/info by id.

No test here needs a container runtime (the NoRuntime class proves dock never calls one).
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import dockenv
from src import catalog
from src.manifest import Manifest, ManifestError
from src.store import Store

REAL_BUILTIN_DIR = catalog.builtin_dir  # before any test patches it


def make_plugin(parent, name, plugin_id, version="0.1.0", commands=(("hello", "Greet someone", ["greet"]),),
                keywords_raw=None):
    """A plugin folder `parent/name`: plugin.toml and a one-line Dockerfile. commands: (name, summary, keywords)."""
    root = Path(parent) / name
    root.mkdir(parents=True, exist_ok=True)
    text = (f'id = "{plugin_id}"\nversion = "{version}"\napi_version = "2"\ndescription = "A python sample"\n\n'
            '[run]\ncontainer = { cmd = "python3", args = ["/plugin/main.py"] }\n')
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
    return make_plugin(parent, "greeter", "dev.dock.greeter", "0.2.0",
                       commands=(("hello", "Greet someone: hello [name]", ["greet", "welcome", "name"]),
                                 ("count", "Count with a progress bar: count [n]", ["progress", "number", "demo"])))


class TmpCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dock-catalog-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.cat = self.tmp / "cat"
        self.cat.mkdir()
        patcher = mock.patch.object(catalog, "builtin_dir", return_value=None)  # the repo's plugins are not these tests' catalog
        patcher.start()
        self.addCleanup(patcher.stop)

    def config(self, text):
        (self.home / "config.toml").write_text(text)

    def catalog_config(self, *folders):
        self.config("[catalog]\npaths = [" + ", ".join(json.dumps(str(f)) for f in folders) + "]\n")

    def store(self):
        return Store(self.home)


class Keywords(TmpCase):
    def test_keywords_are_loaded(self):
        for kws, expected in ((["a", "b"], ["a", "b"]), ([], [])):
            with self.subTest(keywords=kws):
                root = make_plugin(self.tmp, "p", "dev.t.p", commands=(("go", "Go", kws),))
                self.assertEqual(Manifest.load(root).commands[0].keywords, expected)

    def test_bad_keywords_fail(self):  # AC4
        with self.subTest("not a list fails install naming the command"):
            root = make_plugin(self.tmp, "p", "dev.t.p", commands=(("go", "Go", []),), keywords_raw='"greet"')
            with self.assertRaises(ManifestError) as cm:
                self.store().install(root)
            self.assertIn("go: keywords must be a list of non-empty strings", str(cm.exception))
        for raw in ("[1]", '[""]', '["  "]'):
            root = make_plugin(self.tmp, "p", "dev.t.p", commands=(("go", "Go", []),), keywords_raw=raw)
            with self.subTest(raw=raw), self.assertRaises(ManifestError):
                Manifest.load(root)

    def test_install_writes_keywords_only_for_commands_that_have_them(self):
        root = make_plugin(self.tmp, "p", "dev.t.p", commands=(("go", "Go", ["a"]), ("stop", "Stop", [])))
        store = self.store()
        store.install(root)
        entry = store.index()["dev.t.p"]
        self.assertEqual(entry["keywords"], {"go": ["a"]})
        self.assertEqual(entry["commands"], {"go": "Go", "stop": "Stop"})


class CatalogConfig(TmpCase):
    def paths_error(self, text):
        self.config(text)
        with self.assertRaises(ManifestError) as cm:
            catalog.paths(self.store())
        return str(cm.exception)

    def test_valid_catalog_config_returns_folders(self):
        with self.subTest("no config file"):
            self.assertEqual(catalog.paths(self.store()), [])
        with self.subTest("no catalog table"):
            self.config('[container]\nruntime = "docker"\n')
            self.assertEqual(catalog.paths(self.store()), [])
        with self.subTest("absolute paths are returned"):
            self.catalog_config(self.cat)
            self.assertEqual(catalog.paths(self.store()), [self.cat])
        with self.subTest("tilde is expanded"):
            self.config('[catalog]\npaths = ["~/cat"]\n')
            with mock.patch.dict(os.environ, {"HOME": str(self.tmp)}):
                self.assertEqual(catalog.paths(self.store()), [self.tmp / "cat"])
        with self.subTest("other tables are not lookalikes"):
            self.config('[container]\nruntime = "docker"\n[shell]\nidle_timeout = 5\n')
            self.assertEqual(catalog.paths(self.store()), [])

    def test_bad_catalog_config_is_refused(self):  # AC1
        where = self.home / "config.toml"
        cases = [
            ("relative path", '[catalog]\npaths = ["relative/dir"]\n',
             f"{where}: [catalog] paths entry 'relative/dir' must be an absolute path or start with ~"),
            ("unknown key", "[catalog]\nfoo = 1\n", f"{where}: [catalog] has unknown key 'foo' (known: paths)"),
            ("catalog must be a table", 'catalog = "x"\n', f"{where}: [catalog] must be a table"),
            ("top-level paths", 'paths = ["/x"]\n',
             f"{where}: 'paths' is at the top level; catalog settings belong in a [catalog] table"),
        ]
        for raw in ('"x"', "[1]", "5"):
            cases.append((f"paths = {raw}", f"[catalog]\npaths = {raw}\n",
                          f"{where}: [catalog] paths must be a list of folder paths"))
        for name in ("catalgo", "catlog", "catalogs"):
            cases.append((f"misspelt [{name}]", f"[{name}]\npaths = []\n",
                          f"{where}: [{name}] is not a known table; did you mean [catalog]?"))
        for label, text, expected in cases:
            with self.subTest(label):
                self.assertEqual(self.paths_error(text), expected)


class CatalogScan(TmpCase):
    def load(self):
        warnings = []
        found = catalog.load(self.store(), warnings.append)
        return found, warnings

    def test_entry_that_is_a_plugin_and_entry_that_holds_plugins(self):
        direct = make_plugin(self.tmp, "direct", "dev.t.direct")
        parent = self.tmp / "many"
        make_plugin(parent, "a", "dev.t.a")
        make_plugin(parent, "b", "dev.t.b")
        make_plugin(parent / "group", "deep", "dev.t.deep")  # two levels down: not searched
        self.catalog_config(direct, parent)
        found, warnings = self.load()
        self.assertEqual(sorted(a.manifest.id for a in found), ["dev.t.a", "dev.t.b", "dev.t.direct"])
        self.assertEqual(warnings, [])
        self.assertTrue(all(isinstance(a.folder, Path) for a in found))

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

    def test_same_folder_listed_twice_counts_once(self):
        make_plugin(self.cat, "a", "dev.t.a")
        link = self.tmp / "link"
        link.symlink_to(self.cat)
        self.catalog_config(self.cat, self.cat, link)
        found, warnings = self.load()
        self.assertEqual([a.manifest.id for a in found], ["dev.t.a"])
        self.assertEqual(warnings, [])


class SearchUnit(TmpCase):
    def search(self, *words):
        warnings = []
        found = catalog.search(self.store(), list(words), warnings.append)
        return found, warnings

    def test_installed_entry_without_keywords_matches_by_name(self):  # AC18
        (self.home / "index.json").write_text(json.dumps({"dev.t.old": {
            "version": "1.0", "path": str(self.tmp / "nowhere"), "description": "d",
            "commands": {"oldcmd": "An old command"}}}))
        found, warnings = self.search("oldcmd")
        self.assertEqual([(m.name, m.plugin, m.installed) for m in found], [("oldcmd", "dev.t.old", True)])
        self.assertEqual(found[0].keywords, ())
        self.assertEqual(warnings, [])

    def test_installed_keywords_are_searched(self):
        store = self.store()
        store.install(make_plugin(self.tmp, "p", "dev.t.p", commands=(("go", "Go now", ["welcome"]),)))
        found, _ = self.search("welcome")
        self.assertEqual([m.name for m in found], ["go"])
        self.assertEqual(found[0].keywords, ("welcome",))

    def test_words_match_name_summary_and_keywords_but_not_plugin_id_or_description(self):  # AC8
        with self.subTest("every word must match one field and case is ignored"):
            cat = self.tmp / "cat-words"
            make_plugin(cat, "a", "dev.t.a", commands=(("hello", "Greet someone", ["welcome"]),))
            self.catalog_config(cat)
            self.assertEqual(len(self.search("HELLO", "greet", "Welcome")[0]), 1)
            self.assertEqual(self.search("greet", "count")[0], [])
        with self.subTest("plugin id and description are not matched"):
            cat = self.tmp / "cat-id"
            make_plugin(cat, "a", "dev.t.a")
            self.catalog_config(cat)
            self.assertEqual(self.search("python")[0], [])
            self.assertEqual(self.search("dev.t.a")[0], [])

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

    def test_not_installed_match_names_its_install_command(self):
        with self.subTest("by id, with its folder"):
            cat = self.tmp / "cat-one"
            folder = make_plugin(cat, "a", "dev.t.a")
            self.catalog_config(cat)
            (m,) = self.search("hello")[0]
            self.assertFalse(m.installed)
            self.assertEqual(m.install, "dock plugin install dev.t.a")
            self.assertEqual(m.folder, folder)
            self.assertEqual(m.manifest.id, "dev.t.a")
        with self.subTest("id in two folders installs by folder"):
            cat = self.tmp / "cat-two"
            a = make_plugin(cat, "a", "dev.t.a", "0.1.0")
            b = make_plugin(cat, "b", "dev.t.a", "0.2.0")
            self.catalog_config(cat)
            found, _ = self.search("hello")
            self.assertEqual([m.install for m in found], [f"dock plugin install {a}", f"dock plugin install {b}"])

    def test_search_over_200_catalog_plugins_takes_under_a_second(self):  # AC25
        for i in range(200):
            make_plugin(self.cat, f"p{i}", f"dev.t.p{i}", commands=((f"cmd{i}", f"Does thing {i}", [f"kw{i}"]),))
        self.catalog_config(self.cat)
        store = self.store()
        start = time.perf_counter()
        found = catalog.search(store, ["kw150"], lambda m: None)
        self.assertLess(time.perf_counter() - start, 1.0)
        self.assertEqual([m.plugin for m in found], ["dev.t.p150"])


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
        self.env = {**os.environ, "DOCK_HOME": str(self.home), "PYTHONPATH": str(ROOT), "DOCK_NO_READLINE": "1",
                    "HOME": str(self.tmp)}
        self.hello = greeter(self.cat)
        self.outside = greeter(self.tmp / "outside")  # the same plugin, in a folder that is not in the catalog
        self.catalog_config(self.cat)

    def catalog_config(self, *folders):
        dockenv.add_config(self.home, "[catalog]\npaths = [" + ", ".join(json.dumps(str(f)) for f in folders) + "]\n")

    def dock(self, *args, stdin="", env=None):
        return subprocess.run([sys.executable, "-m", "src", *args], input=stdin, capture_output=True, text=True,
                              check=False, env=env or self.env, cwd=self.tmp, timeout=60, **dockenv.SIGINT_DEFAULT)

    def runtime_calls(self):
        return self.log.read_text() if self.log.exists() else ""

    def index(self):
        return self.store().index()


class SearchCli(CliCase):
    def test_not_installed_match_is_one_line(self):  # AC7
        r = self.dock("search", "hello", "greet")
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stdout.splitlines()
        self.assertEqual(lines, [f"{'hello':<16} Greet someone: hello [name]  dev.dock.greeter 0.2.0  "
                                 "not installed: dock plugin install dev.dock.greeter"])

    def test_installed_line_shows_catalog_version(self):  # AC9
        self.store().install(self.hello)
        newer = make_plugin(self.tmp / "newer", "hello", "dev.dock.greeter", "0.3.0",
                            commands=(("hello", "Greet someone: hello [name]", ["greet"]),))
        shutil.rmtree(self.hello)
        shutil.copytree(newer, self.hello)
        r = self.dock("search", "hello")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.splitlines(),
                         [f"{'hello':<16} Greet someone: hello [name]  dev.dock.greeter 0.2.0  "
                          "installed (catalog has 0.3.0)"])

    def newer_hello_with_count(self):
        """Installed: dev.dock.greeter 0.1.0 with no `count`; the catalog's copy is 0.2.0 and has `count`."""
        old = make_plugin(self.tmp / "old", "hello", "dev.dock.greeter", "0.1.0",
                          commands=(("hello", "Greet someone: hello [name]", ["greet"]),))
        self.store().install(old)
        newer = make_plugin(self.tmp / "newer", "hello", "dev.dock.greeter", "0.2.0",
                            commands=(("hello", "Greet someone: hello [name]", ["greet"]),
                                      ("count", "Count up", ["number"])))
        shutil.rmtree(self.hello)
        shutil.copytree(newer, self.hello)

    def test_command_only_in_the_catalog_copy_of_an_installed_plugin_is_listed(self):
        self.newer_hello_with_count()
        r = self.dock("search", "count")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.splitlines(),
                         [f"{'count':<16} Count up  dev.dock.greeter 0.2.0  "
                          "not in installed 0.1.0: dock plugin install dev.dock.greeter"])

    def test_installed_line_without_catalog_difference(self):
        self.store().install(self.hello)
        r = self.dock("search", "count")
        self.assertTrue(r.stdout.rstrip().endswith("dev.dock.greeter 0.2.0  installed"), r.stdout)

    def test_two_plugins_with_the_same_command_give_two_lines_sorted_by_id(self):  # AC10
        make_plugin(self.cat, "other", "dev.aaa.other", commands=(("hello", "Other hello", []),))
        r = self.dock("search", "hello")
        lines = r.stdout.splitlines()
        self.assertEqual(len(lines), 2, r.stdout)
        self.assertIn("dev.aaa.other", lines[0])
        self.assertIn("dev.dock.greeter", lines[1])

    def test_no_match_and_no_words(self):  # AC11
        r = self.dock("search", "greet", "count")
        self.assertEqual((r.returncode, r.stderr), (1, "dock: no commands match 'greet count'\n"))
        for args in ((), ("",), ("  ",)):
            r = self.dock("search", *args)
            self.assertEqual(r.returncode, 2, args)
            self.assertEqual(r.stderr.strip(), "usage: dock search <word>...")

    def test_bad_catalog_config_exits_1_naming_the_problem(self):  # AC1
        self.config(f'[container]\nruntime = "{self.runtime}"\n[catalog]\npaths = ["relative/dir"]\n')
        r = self.dock("search", "x")
        self.assertEqual(r.returncode, 1)
        self.assertIn("paths entry 'relative/dir' must be an absolute path or start with ~", r.stderr)

    def test_missing_catalog_entry_warns_once_and_other_matches_are_listed(self):  # AC3
        gone = self.tmp / "gone"
        self.config(f'[container]\nruntime = "{self.runtime}"\n')
        self.catalog_config(gone, self.cat)
        r = self.dock("search", "hello")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stderr, f"dock: catalog folder {gone} not found, skipped\n")
        self.assertIn("dev.dock.greeter", r.stdout)

    def test_search_works_in_the_shell_and_help_lists_it(self):  # AC12
        r = self.dock("shell", stdin="search greet\n")
        self.assertIn("dev.dock.greeter", r.stdout)
        self.assertNotIn("exit ", r.stderr)
        self.assertIn("search <word>...", self.dock("help").stdout)
        self.assertIn("search <word>...", self.dock("shell", stdin="help\n").stdout)


class Hint(CliCase):
    def test_installed_command_never_reads_the_catalog(self):  # AC6
        self.store().install(self.hello)
        store = self.store()
        with mock.patch.object(catalog, "load", side_effect=AssertionError("catalog read")):
            manifest, command = store.resolve("hello")
        self.assertEqual((manifest.id, command), ("dev.dock.greeter", "hello"))

    def test_installed_command_run_prints_no_catalog_warning(self):  # AC6
        self.store().install(self.hello)
        self.config(f'[container]\nruntime = "{self.runtime}"\n')
        self.catalog_config(self.tmp / "gone")
        r = self.dock("hello", "Ada")  # fails at the fake runtime, which is fine: only the warning matters
        self.assertNotIn("not found, skipped", r.stderr)

    def test_one_shot_names_the_catalog_plugin_and_installs_nothing(self):  # AC13
        r = self.dock("hello", "Ada")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stderr, "dock: unknown command 'hello'. dev.dock.greeter has it (not installed): "
                                   "dock plugin install dev.dock.greeter\n")
        self.assertEqual(self.index(), {})
        self.assertEqual(self.runtime_calls(), "")

    def test_shell_prints_the_hint_and_keeps_running(self):  # AC13
        r = self.dock("shell", stdin="hello Ada\nsearch greet\n")
        self.assertIn("dock: unknown command 'hello'. dev.dock.greeter has it (not installed)", r.stderr)
        self.assertIn("dev.dock.greeter", r.stdout)  # the next line still ran
        self.assertEqual(self.index(), {})

    def test_owner_installed_at_another_version_says_so(self):
        old = make_plugin(self.tmp / "old", "hello", "dev.dock.greeter", "0.1.0",
                          commands=(("hello", "Greet someone", []),))
        self.store().install(old)
        shutil.rmtree(self.hello)
        make_plugin(self.cat, "hello", "dev.dock.greeter", "0.2.0",
                    commands=(("hello", "Greet someone", []), ("count", "Count up", [])))
        r = self.dock("count")
        self.assertEqual(r.stderr, "dock: unknown command 'count'. dev.dock.greeter 0.2.0 in the catalog has it "
                                   "(installed: 0.1.0): dock plugin install dev.dock.greeter\n")

    def test_several_owners_mark_the_installed_one(self):
        old = make_plugin(self.tmp / "old", "hello", "dev.dock.greeter", "0.1.0",
                          commands=(("hello", "Greet someone", []),))
        self.store().install(old)
        shutil.rmtree(self.hello)
        make_plugin(self.cat, "hello", "dev.dock.greeter", "0.2.0",
                    commands=(("hello", "Greet someone", []), ("count", "Count up", [])))
        make_plugin(self.cat, "other", "dev.aaa.other", commands=(("count", "Other count", []),))
        r = self.dock("count")
        self.assertEqual(r.stderr, "dock: unknown command 'count'. Not installed, in the catalog: "
                                   "dev.aaa.other (dock plugin install dev.aaa.other); "
                                   "dev.dock.greeter 0.2.0 (installed: 0.1.0; dock plugin install dev.dock.greeter)\n")

    def test_id_in_two_folders_is_named_by_folder(self):
        other = make_plugin(self.cat, "other", "dev.dock.greeter")
        text = catalog.hint(self.store(), "hello", lambda m: None)
        self.assertIn(f"dock plugin install {other}", text)
        self.assertIn(f"dock plugin install {self.hello}", text)

    def test_no_owner_anywhere_keeps_the_old_message(self):  # AC14
        r = self.dock("nope")
        self.assertEqual((r.returncode, r.stderr), (1, "dock: unknown command 'nope'. Run 'dock help'.\n"))


class InstallById(CliCase):
    def test_install_by_catalog_id_or_by_path(self):  # AC15
        with self.subTest("by catalog id"):
            r = self.dock("plugin", "install", "dev.dock.greeter")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue(r.stdout.startswith("installed dev.dock.greeter 0.2.0"), r.stdout)
            self.assertIn("dev.dock.greeter", self.dock("plugin", "list").stdout)
        with self.subTest("by path"):
            shutil.rmtree(self.home)  # a fresh DOCK_HOME, as the original test had
            self.home.mkdir()
            self.config(f'[container]\nruntime = "{self.runtime}"\n')
            self.catalog_config(self.cat)
            r = self.dock("plugin", "install", str(self.outside))
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("dev.dock.greeter", self.index())

    def test_unknown_id_is_not_found(self):  # AC15
        r = self.dock("plugin", "install", "dev.dock.nope")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stderr, "dock: no plugin folder or catalog plugin named 'dev.dock.nope'\n")

    def test_id_in_two_folders_lists_both_and_installs_nothing(self):  # AC16
        other = make_plugin(self.cat, "other", "dev.dock.greeter", "0.3.0")
        r = self.dock("plugin", "install", "dev.dock.greeter")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stderr, f"dock: dev.dock.greeter is in several catalog folders; install one by path: "
                                   f"{self.hello} (0.2.0), {other} (0.3.0)\n")
        self.assertEqual(self.index(), {})

    def test_escaping_symlink_is_listed_by_search_and_refused_by_install(self):  # AC24
        evil = make_plugin(self.cat, "evil", "dev.t.evil", commands=(("evilcmd", "Evil", []),))
        (self.tmp / "secret").write_text("s")
        (evil / "leak").symlink_to(self.tmp / "secret")
        self.assertIn("dev.t.evil", self.dock("search", "evilcmd").stdout)
        r = self.dock("plugin", "install", "dev.t.evil")
        self.assertEqual(r.returncode, 1)
        self.assertIn("symlink leak points outside the plugin folder", r.stderr)
        self.assertNotIn("dev.t.evil", self.index())


class ShellInstallReadsTheCatalogOnce(CliCase):
    def test_missing_entry_warning_is_printed_once(self):
        gone = self.tmp / "gone"
        self.config(f'[container]\nruntime = "{self.runtime}"\n')
        self.catalog_config(gone, self.cat)
        r = self.dock("shell", stdin="plugin install dev.dock.greeter\nexit\n")
        self.assertEqual(r.stderr, f"dock: catalog folder {gone} not found, skipped\n")
        self.assertIn("installed dev.dock.greeter 0.2.0", r.stdout)
        self.assertIn("dev.dock.greeter", self.index())

    def test_failed_install_is_reported_once_and_the_shell_keeps_running(self):
        with self.subTest("not found id"):
            r = self.dock("shell", stdin="plugin install dev.dock.nope\nversion\nexit\n")
            self.assertEqual(r.stderr, "dock: no plugin folder or catalog plugin named 'dev.dock.nope'\n")
            self.assertIn("dock ", r.stdout)
        with self.subTest("broken manifest"):
            empty = self.tmp / "empty"
            empty.mkdir()
            r = self.dock("shell", stdin=f"plugin install {empty}\nversion\nexit\n")
            self.assertEqual(r.stderr.count("no plugin.toml in"), 1, r.stderr)
            self.assertIn("dock ", r.stdout)


class InfoById(CliCase):
    def fields(self, r):
        return dict(line.split(":", 1) for line in r.stdout.splitlines())

    def test_info_of_a_catalog_id_or_a_path_shows_keywords_and_where_it_is(self):  # AC17
        with self.subTest("catalog id"):
            r = self.dock("plugin", "info", "dev.dock.greeter")
            self.assertEqual(r.returncode, 0, r.stderr)
            f = self.fields(r)
            self.assertEqual(f["id"].strip(), "dev.dock.greeter")
            self.assertEqual(f["keywords"].strip(), "hello: greet, welcome, name; count: progress, number, demo")
            self.assertEqual(f["location"].strip(), str(self.hello))
            self.assertEqual(r.stdout.splitlines()[-1], f"installed:   no ({self.hello})")
            self.assertLess(r.stdout.index("env:"), r.stdout.index("keywords:"))
        with self.subTest("path"):
            r = self.dock("plugin", "info", str(self.outside))
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.splitlines()[-1], f"installed:   no ({self.outside})")

    def test_info_of_a_folder_whose_id_is_installed_says_so(self):
        self.store().install(self.hello)
        r = self.dock("plugin", "info", str(self.outside))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.splitlines()[-1], f"installed:   yes, 0.2.0 ({self.index()['dev.dock.greeter']['path']})")
        self.assertIn(f"location:    {self.outside}", r.stdout)

    def test_info_of_an_installed_id_has_a_keywords_line_and_no_installed_line(self):  # AC17
        with self.subTest("with keywords"):
            self.store().install(self.hello)
            r = self.dock("plugin", "info", "dev.dock.greeter")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("keywords:    hello: greet, welcome, name; count: progress, number, demo", r.stdout)
            self.assertNotIn("installed:", r.stdout)
        with self.subTest("a dash without keywords"):
            self.store().install(make_plugin(self.tmp, "p", "dev.t.p", commands=(("go", "Go", []),)))
            r = self.dock("plugin", "info", "dev.t.p")
            self.assertIn("keywords:    -\n", r.stdout)

    def test_unknown_id_and_ambiguous_id_fail(self):
        r = self.dock("plugin", "info", "dev.dock.nope")
        self.assertEqual((r.returncode, r.stderr), (1, "dock: no plugin folder or catalog plugin named 'dev.dock.nope'\n"))
        make_plugin(self.cat, "other", "dev.dock.greeter", "0.2.0")
        r = self.dock("plugin", "info", "dev.dock.greeter")
        self.assertEqual(r.returncode, 1)
        self.assertIn("is in several catalog folders", r.stderr)


class NoRuntime(CliCase):
    """search, the hint and `plugin info <catalog id>` never call the container runtime (the fake one logs calls)."""

    def test_no_call_for_search_hint_and_info(self):  # AC5
        with self.subTest("a runtime that logs its calls"):
            for args in (("search", "hello"), ("hello", "Ada"), ("plugin", "info", "dev.dock.greeter")):
                r = self.dock(*args)
                self.assertEqual(r.returncode, 1 if args[0] == "hello" else 0, (args, r.stderr))
            self.assertEqual(self.runtime_calls(), "")
        with self.subTest("no runtime at all"):
            self.config('[container]\nruntime = "/nonexistent/docker"\n')
            self.catalog_config(self.cat)
            for args in (("search", "hello"), ("plugin", "info", "dev.dock.greeter")):
                r = self.dock(*args)
                self.assertEqual(r.returncode, 0, (args, r.stderr))
            self.assertIn("dev.dock.greeter has it (not installed)", self.dock("hello").stderr)


class BuiltIn(TmpCase):
    """The <repo>/plugins folder is a catalog source with no config; [catalog] paths come first."""

    def setUp(self):
        super().setUp()
        self.builtin = self.tmp / "builtin"
        make_plugin(self.builtin, "g", "dev.t.g", "0.1.0", commands=(("hi", "Say hi", ["greet"]),))
        patcher = mock.patch.object(catalog, "builtin_dir", return_value=self.builtin)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_load_finds_builtins_with_no_config(self):  # catches: builtin scan not wired into load()
        self.assertEqual([a.manifest.id for a in catalog.load(self.store())], ["dev.t.g"])

    def test_no_builtin_folder_is_skipped_silently(self):  # catches: a packaged install warning on every command
        for value in (None, self.tmp / "gone"):
            with self.subTest(value=value), mock.patch.object(catalog, "builtin_dir", return_value=value):
                warnings = []
                self.assertEqual(catalog.load(self.store(), warnings.append), [])
                self.assertEqual(warnings, [])

    def test_a_catalog_path_copy_wins_over_the_builtin_of_the_same_id(self):  # catches: builtin shadowing paths
        make_plugin(self.cat, "g", "dev.t.g", "0.9.0")
        self.catalog_config(self.cat)
        found = catalog.find(self.store(), "dev.t.g")
        self.assertEqual(found.manifest.version, "0.9.0")

    def test_search_marks_a_builtin_not_installed_with_the_install_hint(self):  # catches: search ignoring builtins
        found = catalog.search(self.store(), ["greet"])
        self.assertEqual([(m.plugin, m.installed, m.install) for m in found],
                         [("dev.t.g", False, "dock plugin install dev.t.g")])

    def test_builtin_dir_is_the_repo_plugins_folder(self):  # catches: a wrong parents[] index
        self.assertEqual(REAL_BUILTIN_DIR(), ROOT.parent.parent / "plugins")

    def test_builtin_dir_needs_the_checkout_marker(self):  # catches: trusting any parents[3]/plugins
        (self.tmp / "other" / "plugins").mkdir(parents=True)
        here = self.tmp / "other" / "apps" / "cli" / "src" / "catalog.py"
        here.parent.mkdir(parents=True)
        here.write_text("")
        self.assertEqual(REAL_BUILTIN_DIR(here), self.tmp / "other" / "plugins")
        lone = self.tmp / "lone" / "a" / "b" / "src" / "catalog.py"
        (self.tmp / "lone" / "plugins").mkdir(parents=True)
        lone.parent.mkdir(parents=True)
        lone.write_text("")
        self.assertIsNone(REAL_BUILTIN_DIR(lone))

    def test_scanning_a_copy_of_the_real_plugins_folder_is_fast(self):  # catches: a slow scan (NFR1)
        copy = self.tmp / "plugins-copy"
        shutil.copytree(ROOT.parent.parent / "plugins" / "dm", copy / "dm",
                        ignore=shutil.ignore_patterns(".venv", "__pycache__", "node_modules", ".git"))
        with mock.patch.object(catalog, "builtin_dir", return_value=copy):
            catalog.load(self.store())  # warm the file cache
            start = time.perf_counter()
            found = catalog.load(self.store())
            self.assertLess(time.perf_counter() - start, 0.5)
        self.assertEqual([a.manifest.id for a in found], ["dev.dock.dm"])


class BrainCatalogShowsImage(TmpCase):
    def test_the_image_plugin_is_in_the_brains_catalog_with_its_summary_and_inputs(self):
        from src import flow_cli
        store = self.store()
        store.install(ROOT.parent.parent / "plugins" / "image")
        work = self.tmp / "work"
        work.mkdir()
        data = json.loads(flow_cli.write_catalog(store, work).read_text(encoding="utf-8"))
        plugin = next(p for p in data["plugins"] if p["id"] == "dev.dock.image")
        command = plugin["commands"][0]
        self.assertEqual(command["name"], "image")
        self.assertIn("picture", command["summary"])
        self.assertIn({"name": "prompt", "type": "string", "required": True}, command["inputs"])
        self.assertIn({"name": "width", "type": "int", "required": False}, command["inputs"])
