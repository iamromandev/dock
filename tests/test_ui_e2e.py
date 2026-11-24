"""v2 UI messages through a real host and a real container, with output that is not a terminal (plain mode).

Docker or podman is needed; without one the class is skipped, as the other e2e tests are. Run `make test`.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

import dockenv  # noqa: E402

UIPROBE = ROOT / "tests" / "fixtures" / "uiprobe"
PLUGIN_ID = "dev.dock.uiprobe"
FFFD = "�"

FIRST = "first  [####################]  100.0%  100 of 100"
BASIC_STDERR = [
    FIRST,
    "second  [##########..........]  50.0%",
    "peers", "peer     down", "1.2.3.4  100", "5.6.7.8  200",
    "stats", "speed  100 MB/s", "peers  2",
]


@unittest.skipUnless(dockenv.runtime(), "needs Docker or podman")
class UiE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        dockenv.prebuild_plugins(UIPROBE)
        cls.home = dockenv.make_home("dock-ui-e2e-", UIPROBE)
        cls.addClassCleanup(shutil.rmtree, cls.home, ignore_errors=True)
        cls.env = {k: v for k, v in os.environ.items() if k not in ("DOCK_PLAIN", "TERM", "NO_COLOR")}
        cls.env.update(DOCK_HOME=str(cls.home), PYTHONPATH=str(ROOT))

    def dock(self, *args, stdin="", env=None, stderr_to_file=False):
        """Run dock with piped stdin and stdout; stderr is a pipe, or a file when `stderr_to_file`."""
        with tempfile.TemporaryFile("w+") as err_file:
            r = subprocess.run(
                [sys.executable, "-m", "cli", *args], input=stdin, stdout=subprocess.PIPE,
                stderr=err_file if stderr_to_file else subprocess.PIPE, text=True, check=False,
                env={**self.env, **(env or {})}, cwd=ROOT, timeout=120)
            if stderr_to_file:
                err_file.seek(0)
                r.stderr = err_file.read()
        return r

    def plugin_log(self):
        return (Path(self.home) / "logs" / f"{PLUGIN_ID}.log").read_text()

    # AC1, AC6
    def test_v2_plugin_runs_and_prints_each_area_once_without_escapes(self):
        variants = {"stderr is a pipe": {}, "stderr is a file": {"stderr_to_file": True},
                    "DOCK_PLAIN": {"env": {"DOCK_PLAIN": "1"}}, "TERM=dumb": {"env": {"TERM": "dumb"}}}
        for name, kw in variants.items():
            with self.subTest(name):
                r = self.dock("show", "basic", **kw)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(r.stdout, "basic done\n")
                self.assertNotIn("\x1b", r.stderr)
                self.assertEqual(r.stderr.splitlines(), BASIC_STDERR)

    # AC1
    def test_a_v1_manifest_is_refused_at_install(self):
        tmp = Path(tempfile.mkdtemp(prefix="dock-ui-v1-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        old = dockenv.plugin_copy(UIPROBE, tmp, "dev.dock.uiprobe-v1")
        manifest = old / "plugin.toml"
        manifest.write_text(manifest.read_text().replace('api_version = "2"', 'api_version = "1"'))
        r = self.dock("plugin", "install", str(old))
        self.assertEqual(r.returncode, 1)
        self.assertIn("dev.dock.uiprobe-v1 needs protocol v1, this dock speaks v2", r.stderr)

    # AC2, AC4
    def test_invalid_and_unknown_messages_are_dropped_and_logged_and_the_run_succeeds(self):
        r = self.dock("show", "invalid")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "form error -32602\ninvalid done\n")
        self.assertEqual(r.stderr.splitlines(), ["still works  [####################]  100.0%"])
        lines = self.plugin_log().splitlines()
        self.assertIn("[unknown notification] progress", lines)
        self.assertIn("[unknown notification] mystery", lines)
        invalid = [ln for ln in lines if ln.startswith("[invalid ")]
        self.assertEqual(len(invalid), 8, invalid)
        self.assertTrue(any("id is longer than 64" in ln for ln in invalid))
        self.assertTrue(any("more than 32 live areas" in ln for ln in invalid))
        self.assertTrue(any("more than 10000" in ln for ln in invalid))

    # AC5 (plain half)
    def test_hostile_text_shows_as_replacement_characters(self):
        r = self.dock("show", "hostile")
        self.assertEqual(r.returncode, 0, r.stderr)
        for name, text in (("stdout", r.stdout), ("stderr", r.stderr)):
            with self.subTest(name):
                bad = [c for c in text if (ord(c) < 0x20 and c != "\n") or ord(c) == 0x7F or 0x80 <= ord(c) <= 0x9F]
                self.assertEqual(bad, [])
                self.assertIn(FFFD, text)
        self.assertIn(f"out {FFFD}[31mred{FFFD}[0m\n", r.stdout)
        self.assertIn(f"[{PLUGIN_ID}] info: log {FFFD}]0;pwned{FFFD}", r.stderr)
        self.assertIn(f"panel {FFFD}bell", r.stderr)

    # AC9 (plain)
    def test_form_answers_come_from_stdin_lines(self):
        r = self.dock("ask", stdin="\ntok\ny\n2\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), {"values": {"name": "Ada", "token": "tok", "ok": True, "mirror": "us"}})
        for part in ("Settings", "Your name [Ada]", "Token", "Overwrite? (y/N)", "1) eu", "3) asia"):
            self.assertIn(part, r.stderr)

    def test_a_choice_by_text_and_an_invalid_answer_asked_again(self):
        r = self.dock("ask", stdin="Bob\ntok\nmaybe\nyes\nasia\n")
        self.assertEqual(json.loads(r.stdout)["values"], {"name": "Bob", "token": "tok", "ok": True, "mirror": "asia"})
        self.assertEqual(r.stderr.count("Overwrite?"), 2)

    def test_three_invalid_answers_give_32602(self):
        r = self.dock("ask", stdin="\n\nx\nx\nx\n")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stdout, "error -32602\n")

    def test_end_of_input_gives_eof(self):
        r = self.dock("ask", stdin="")
        self.assertEqual(json.loads(r.stdout), {"eof": True})


if __name__ == "__main__":
    unittest.main()
