"""`dock serve` search: discovery of catalog plugins over the wire. No test here needs a runtime or a running plugin."""

import json
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from cli import SERVE_VERSION  # noqa: E402
from cli.store import Store  # noqa: E402
from serve_client import ServeClient  # noqa: E402
from test_catalog import make_plugin  # noqa: E402

BAD_WORDS = "words must be a non-empty list of non-empty strings"


class SearchCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dock-serve-search-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.cat = self.tmp / "cat"
        self.cat.mkdir()

    def catalog_config(self, *folders):
        (self.home / "config.toml").write_text(
            "[catalog]\npaths = [" + ", ".join(json.dumps(str(f)) for f in folders) + "]\n")

    def client(self, hello=True) -> ServeClient:
        c = ServeClient(self.home)
        self.addCleanup(c.cleanup)
        if hello:
            c.hello()
        return c

    def assert_bad_words(self, params):
        reply = self.client().request("search", params)
        self.assertEqual(reply["error"], {"code": -32602, "message": BAD_WORDS}, params)


class Search(SearchCase):
    def test_serve_version_is_still_1(self):
        self.assertEqual(SERVE_VERSION, "1")

    def test_catalog_only_match_has_every_field(self):  # AC20
        root = make_plugin(self.cat, "hello", "dev.t.hello")
        self.catalog_config(self.cat)
        reply = self.client().request("search", {"words": ["hello", "greet"]})
        self.assertEqual(reply["result"], {"matches": [{
            "name": "hello", "plugin": "dev.t.hello", "version": "0.1.0", "summary": "Greet someone",
            "keywords": ["greet"], "inputs": [], "installed": False, "path": str(root),
        }]})

    def test_inputs_have_the_shape_of_commands(self):
        root = make_plugin(self.cat, "hello", "dev.t.hello")
        with open(root / "plugin.toml", "a") as f:
            f.write('\n[[commands.inputs]]\nname = "who"\ntype = "string"\nrequired = true\nflag = "--who"\n')
        self.catalog_config(self.cat)
        match = self.client().request("search", {"words": ["hello"]})["result"]["matches"][0]
        self.assertEqual(match["inputs"], [{"name": "who", "type": "string", "required": True, "flag": "--who"}])

    def test_installed_match_has_catalog_version_only_when_it_differs_and_no_path(self):
        Store(self.home).install(make_plugin(self.tmp, "i", "dev.t.a", "0.1.0"))
        with self.subTest("catalog has another version"):
            make_plugin(self.cat, "a", "dev.t.a", "0.2.0")
            self.catalog_config(self.cat)
            matches = self.client().request("search", {"words": ["hello"]})["result"]["matches"]
            self.assertEqual(matches, [{
                "name": "hello", "plugin": "dev.t.a", "version": "0.1.0", "summary": "Greet someone",
                "keywords": ["greet"], "inputs": [], "installed": True, "catalog_version": "0.2.0",
            }])
        with self.subTest("no other catalog version"):
            (self.home / "config.toml").unlink(missing_ok=True)  # the original had no catalog at all
            matches = self.client().request("search", {"words": ["hello"]})["result"]["matches"]
            self.assertEqual(len(matches), 1)
            self.assertTrue(matches[0]["installed"])
            self.assertNotIn("catalog_version", matches[0])
            self.assertNotIn("installed_version", matches[0])
            self.assertNotIn("path", matches[0])

    def test_catalog_command_of_an_installed_plugin_has_installed_version(self):
        Store(self.home).install(make_plugin(self.tmp, "i", "dev.t.a", "0.1.0", commands=(("other", "Other", []),)))
        make_plugin(self.cat, "a", "dev.t.a", "0.2.0")
        self.catalog_config(self.cat)
        matches = self.client().request("search", {"words": ["hello"]})["result"]["matches"]
        self.assertEqual(len(matches), 1)
        self.assertFalse(matches[0]["installed"])
        self.assertEqual(matches[0]["installed_version"], "0.1.0")
        self.assertIn("path", matches[0])

    def test_no_match_is_an_empty_list(self):
        make_plugin(self.cat, "hello", "dev.t.hello")
        self.catalog_config(self.cat)
        reply = self.client().request("search", {"words": ["nonexistentword"]})
        self.assertEqual(reply["result"], {"matches": []})

    def test_bad_catalog_config_is_an_error_reply_and_serve_survives(self):
        (self.home / "config.toml").write_text('[catalog]\npaths = "nope"\n')
        c = self.client()
        reply = c.request("search", {"words": ["hello"]})
        self.assertEqual(reply["error"]["code"], -32000)
        self.assertIn("[catalog] paths", reply["error"]["message"])
        self.assertEqual(c.request("commands")["result"], {"commands": []})

    def test_catalog_warnings_go_to_stderr_not_stdout(self):
        make_plugin(self.cat, "hello", "dev.t.hello")
        missing = self.tmp / "gone"
        self.catalog_config(missing, self.cat)
        c = self.client()
        reply = c.request("search", {"words": ["hello"]})
        self.assertEqual([m["plugin"] for m in reply["result"]["matches"]], ["dev.t.hello"])
        self.assertEqual(c.bad_lines, [])  # stdout is ordered: a stray line would have come before the reply
        deadline = time.monotonic() + 5  # stderr is a separate pipe, read on its own thread
        while b"not found, skipped" not in c.stderr and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertIn(f"catalog folder {missing} not found, skipped".encode(), c.stderr)


class SearchParams(SearchCase):
    def test_bad_words_are_refused(self):  # AC21
        for label, params in (("missing words", {}), ("empty words", {"words": []}),
                              ("words not a list", {"words": "hello"}),
                              ("non-string word", {"words": ["hello", 3]}),
                              ("blank word", {"words": ["hello", "  "]})):
            with self.subTest(label):
                self.assert_bad_words(params)
        with self.subTest("params not an object"):
            c = self.client()
            c.send({"id": 99, "method": "search", "params": ["hello"]})
            self.assertEqual(c.wait_reply(99)["error"], {"code": -32602, "message": BAD_WORDS})

    def test_search_before_hello(self):
        reply = self.client(hello=False).request("search", {"words": ["hello"]})
        self.assertEqual(reply["error"]["code"], -32600)
        self.assertEqual(reply["error"]["message"], "hello first")

    def test_notification_gets_no_reply(self):
        make_plugin(self.cat, "hello", "dev.t.hello")
        self.catalog_config(self.cat)
        c = self.client()
        c.notify("search", {"words": ["hello"]})
        reply = c.request("commands")  # handled in order: a search reply would have arrived by now
        self.assertEqual(reply["result"], {"commands": []})
        with c.cond:
            self.assertEqual(set(c.replies), {1, 2})  # hello and commands only
            self.assertEqual(c.notifications, [])


class RunHint(SearchCase):
    def test_run_of_a_catalog_only_command_names_the_plugin_to_install(self):  # AC22
        make_plugin(self.cat, "hello", "dev.t.hello")
        self.catalog_config(self.cat)
        reply = self.client().request("run", {"command": "hello", "args": [], "cwd": str(self.tmp)})
        self.assertEqual(reply["error"]["code"], -32601)
        self.assertIn("dev.t.hello has it (not installed): dock plugin install dev.t.hello",
                      reply["error"]["message"])


if __name__ == "__main__":
    unittest.main()
