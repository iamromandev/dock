"""Live rendering through a real host and a real container, in a pty: AC3, AC5, AC9, AC11, AC12 and exit codes (R17).

Docker or podman is needed; without one the classes are skipped, as the other e2e tests are. Run `make test`.
"""

import json
import os
import re
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import dockenv  # noqa: E402
import ptykit  # noqa: E402
import test_dm_e2e  # noqa: E402
import test_dm_torrent_e2e  # noqa: E402  (the module, not its class: its tests are not ours to run again)

UIPROBE = ROOT / "tests" / "fixtures" / "uiprobe"
SHOWN = "dock>"
WHITELIST = re.compile(r"\x1b\[(\d*A|J|K|\d*C|\?25[hl]|1m|0m|H|2J)")
FIRST_FINAL = "first  [####################]  100.0%  100 of 100"
NEEDS = unittest.skipUnless(dockenv.runtime() and ptykit.AVAILABLE, "needs Docker or podman, and a pty")


def host_env(home, **extra):
    env = {k: v for k, v in os.environ.items() if k not in ("DOCK_PLAIN", "NO_COLOR", "TERM")}
    env.update(DOCK_HOME=str(home), PYTHONPATH=str(ROOT), TERM="xterm-256color", **extra)
    return env


class LiveAsserts:
    def assert_restored(self, p):
        self.assertEqual(p.termios(), p.initial_termios, "termios is not what it was before")
        self.assertTrue(p.screen.cursor_visible, "the cursor is still hidden")

    def assert_only_host_sequences(self, p):
        with p._cv:
            seqs, controls, data = list(p.screen.seqs), list(p.screen.controls), p.data
        for s in seqs:
            self.assertTrue(WHITELIST.fullmatch(s), f"a sequence dock does not send itself: {s!r}")
        self.assertEqual(controls, [])
        for bad in (b"\x07", b"\x00", b"\x08", b"\x7f", b"\xc2\x9b"):
            self.assertNotIn(bad, data)

    def prompts(self, p):
        return sum(1 for line in p.text().splitlines() if line.startswith(SHOWN))


@NEEDS
class UiProbeLive(LiveAsserts, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        dockenv.prebuild_plugins(UIPROBE)
        cls.home = dockenv.make_home("dock-live-e2e-", UIPROBE)
        cls.addClassCleanup(shutil.rmtree, cls.home, ignore_errors=True)

    def dock(self, *args, cols=80, rows=24):
        p = ptykit.Pty([sys.executable, "-m", "cli", *args], cols=cols, rows=rows, env=host_env(self.home), cwd=ROOT)
        self.addCleanup(p.close)
        return p

    # AC3, R17 ------------------------------------------------------------------------
    def test_a_one_shot_run_draws_live_areas_and_leaves_one_final_copy(self):
        p = self.dock("show", "basic")
        self.assertEqual(p.wait_exit(timeout=120), 0)
        text = p.text()
        self.assertEqual(text.count(FIRST_FINAL), 1)
        for once in ("second  [##########..........]  50.0%", "peer     down", "1.2.3.4  100", "stats\nspeed  100 MB/s",
                     "basic done"):
            self.assertEqual(text.count(once), 1, once)
        self.assertNotIn("┌", text)
        self.assert_restored(p)
        self.assert_only_host_sequences(p)
        with p._cv:
            self.assertTrue(any(s.endswith("A") for s in p.screen.seqs), "nothing was redrawn in place")

    def test_the_exit_code_of_a_plugin_command_is_unchanged(self):
        p = self.dock("show", "nosuch")
        self.assertEqual(p.wait_exit(timeout=120), 2)
        p.wait_text("unknown script nosuch")
        self.assert_restored(p)

    def test_a_usage_error_exits_2_without_starting_anything(self):
        p = self.dock("show")
        self.assertEqual(p.wait_exit(timeout=60), 2)
        self.assert_restored(p)

    # AC4 -----------------------------------------------------------------------------
    def test_invalid_messages_are_dropped_and_the_run_ends_normally(self):
        p = self.dock("show", "invalid")
        self.assertEqual(p.wait_exit(timeout=120), 0)
        p.wait_text("form error -32602")
        p.wait_text("invalid done")
        self.assertIn("still works  [####################]  100.0%", p.text())
        self.assert_restored(p)

    # AC5 -----------------------------------------------------------------------------
    def test_hostile_text_from_a_real_plugin_reaches_the_terminal_only_as_replacement_characters(self):
        p = self.dock("show", "hostile")
        self.assertEqual(p.wait_exit(timeout=120), 0)
        p.wait_text("hostile done")
        self.assertIn("�", p.text())
        self.assert_only_host_sequences(p)
        self.assert_restored(p)

    # AC9 -----------------------------------------------------------------------------
    def test_a_live_form_takes_keys_and_the_values_reach_the_plugin(self):
        p = self.dock("ask")
        p.wait_text("Your name [Ada]")
        p.send("Bob\r")
        p.wait_text("Token")
        p.send("s3cr3t\r")
        p.wait_text("Overwrite? (y/N)")
        p.send("y")
        p.wait_text("Mirror")
        p.send("\x1b[B\x1b[B")
        p.wait_text("› asia")
        p.send("\r")
        self.assertEqual(p.wait_exit(timeout=60), 0)
        p.wait_text('"mirror": "asia"')
        values = json.loads(next(line for line in p.text().splitlines() if line.startswith("{")))
        self.assertEqual(values, {"values": {"mirror": "asia", "name": "Bob", "ok": True, "token": "s3cr3t"}})
        self.assertEqual(p.data.count(b"s3cr3t"), 1, "the secret was echoed (once is the plugin printing its values)")
        self.assert_restored(p)

    def test_ctrl_c_in_an_open_form_cancels_the_run_with_exit_130(self):
        p = self.dock("ask")
        p.wait_text("Your name")
        start = time.monotonic()
        p.send("\x03")
        self.assertEqual(p.wait_exit(timeout=60), 130)
        self.assertLess(time.monotonic() - start, 10, "cancel took longer than the grace period")
        self.assert_restored(p)

    # Ctrl-C while running -----------------------------------------------------------------
    def test_ctrl_c_while_a_command_runs_cancels_it_with_exit_130_within_the_grace_period(self):
        p = self.dock("show", "slow")
        p.wait_text("slow started")
        p.wait_text("slow  [")
        start = time.monotonic()
        p.send("\x03")
        self.assertEqual(p.wait_exit(timeout=60), 130)
        self.assertLess(time.monotonic() - start, 6, "cancel took longer than the grace period")
        p.wait_text("interrupted, cancelling")
        self.assertNotIn("┌", p.text())
        self.assert_restored(p)

    # AC11 ------------------------------------------------------------------------------
    def test_in_the_shell_the_prompt_is_hidden_while_a_command_runs_and_other_keys_are_ignored(self):
        p = self.dock("shell")
        p.wait_text(SHOWN)
        p.send("show slow\r")
        p.wait_text("slow started")
        p.wait_text("slow  [")
        self.assertEqual(self.prompts(p), 1, "a prompt was shown while the command runs")
        p.send("zzz\r\x1b[A")
        time.sleep(0.5)  # the bar keeps drawing, so the output never goes quiet
        self.assertEqual(self.prompts(p), 1)
        self.assertNotIn(b"zzz", p.data, "a key typed while the command runs was echoed")
        p.send("\x03")
        p.wait_for(lambda: self.prompts(p) == 2, timeout=60, what="the prompt after Ctrl-C")
        p.wait_text("slow cancelled")
        self.assertNotIn("zzz", p.text(), "the keys typed meanwhile were kept")
        p.send("version\r")
        p.wait_text("dock 0.2.0")
        p.send("\x04")
        self.assertEqual(p.wait_exit(timeout=60), 0)
        self.assert_restored(p)

    def test_in_the_shell_a_run_leaves_its_areas_in_scrollback_and_the_prompt_comes_back(self):
        p = self.dock("shell")
        p.wait_text(SHOWN)
        p.send("show basic\r")
        p.wait_text("basic done", timeout=120)
        p.wait_for(lambda: self.prompts(p) == 2, what="the prompt after the run")
        self.assertEqual(p.text().count(FIRST_FINAL), 1)
        self.assertNotIn("┌", p.text())
        p.send("\x04")
        self.assertEqual(p.wait_exit(timeout=60), 0)
        self.assert_restored(p)
        self.assert_only_host_sequences(p)

    def test_in_the_shell_a_second_run_of_the_same_plugin_shows_its_own_areas(self):
        p = self.dock("shell")
        p.wait_text(SHOWN)
        for n in (1, 2):
            p.send("show basic\r")
            p.wait_for(lambda n=n: p.text().count("basic done") == n, timeout=120, what=f"run {n} to finish")
            p.wait_for(lambda n=n: self.prompts(p) == n + 1, what=f"the prompt after run {n}")
        self.assertEqual(p.text().count(FIRST_FINAL), 2, "the second run showed nothing of its own")
        p.send("\x04")
        self.assertEqual(p.wait_exit(timeout=60), 0)
        self.assert_restored(p)

    def test_in_the_shell_ctrl_z_during_a_run_stops_it_clean_and_fg_draws_the_region_again(self):
        p = self.dock("shell")
        p.wait_text(SHOWN)
        p.send("show slow\r")
        p.wait_text("slow  [")
        p.send("\x1a")
        p.wait_stopped()
        self.assert_restored(p)
        self.assertNotIn("slow  [", p.text(), "the region was left on screen while stopped")
        mark = len(p.data)
        p.cont()
        p.wait_data(b"slow", start=mark)
        p.wait_for(lambda: "slow  [" in p.text(), what="the region again")
        p.send("\x03")
        p.wait_for(lambda: self.prompts(p) >= 2, timeout=60, what="the prompt after Ctrl-C")
        p.send("\x04")
        self.assertEqual(p.wait_exit(timeout=60), 0)


@NEEDS
class DmLive(LiveAsserts, unittest.TestCase):
    """AC12: `dock dm <url>` in a pty shows a live bar, replaced by `saved ...`, and exits 0."""

    env = test_dm_e2e.DmE2E.__dict__["env"]
    dock = test_dm_e2e.DmE2E.__dict__["dock"]

    @classmethod
    def setUpClass(cls):
        test_dm_e2e.DmE2E.setUpClass.__func__(cls)

    def test_dm_shows_a_live_bar_then_the_saved_line_and_exits_zero(self):
        work = Path(tempfile.mkdtemp(prefix="w-", dir=self.base))
        p = ptykit.Pty([sys.executable, "-m", "cli", "dm", f"{self.url}/slow/live.bin?size=3000000&seconds=6"],
                       env=host_env(self.home), cwd=work)
        self.addCleanup(p.close)
        p.wait_for(lambda: re.search(r"\[#*\.*\]\s+\d+\.\d%", p.text()) is not None, timeout=60, what="a live bar")
        self.assertEqual(p.wait_exit(timeout=120), 0)
        text = p.text()
        self.assertRegex(text, r"saved live\.bin \(")
        self.assertEqual(sum(1 for line in text.splitlines() if re.search(r"\[#*\.*\]", line)), 1,
                         "the bar is in scrollback once, in its final state")
        self.assertTrue((work / "live.bin").exists())
        self.assert_restored(p)
        self.assert_only_host_sequences(p)


@NEEDS
class DmTorrentLive(LiveAsserts, unittest.TestCase):
    """AC15 (R21, R22): `dock dm <3-file torrent>` in a pty shows the file bars, the total, the panel and the peers table."""

    PL = 16384
    env = test_dm_torrent_e2e.E2E.__dict__["env"]
    dock = test_dm_torrent_e2e.E2E.__dict__["dock"]
    torrent = test_dm_torrent_e2e.E2E.__dict__["torrent"]
    seed = test_dm_torrent_e2e.E2E.__dict__["seed"]

    @classmethod
    def setUpClass(cls):
        test_dm_torrent_e2e.E2E.setUpClass.__func__(cls)
        test_dm_torrent_e2e.E2E.seed_no = getattr(test_dm_torrent_e2e.E2E, "seed_no", 0)  # the borrowed seed() counts there

    def test_a_three_file_torrent_is_shown_live_and_ends_with_the_unchanged_saved_line(self):
        work = Path(tempfile.mkdtemp(prefix="w-", dir=self.base))
        files = [(f"{n}.bin", test_dm_torrent_e2e.torrentkit.content(250_000, n)) for n in "abc"]
        t = self.torrent(files, name="pack", piece_length=self.PL)
        self.seed(t, "--delay", "0.05")
        t.write_torrent(work / "data.torrent")
        p = ptykit.Pty([sys.executable, "-m", "cli", "dm", "data.torrent"], cols=100, rows=40,
                       env=host_env(self.home), cwd=work)
        self.addCleanup(p.close)

        def everything_is_shown():
            text = p.text()
            return (all(re.search(rf"{n}\.bin\s+\[#*\.*\]\s+\d+\.\d%", text) for n in "abc")
                    and re.search(r"total\s+\[#*\.*\]", text) and re.search(r"peer\s+client\s+down\s+up", text)
                    and re.search(r"\d+\.\d+\.\d+\.\d+:\d+", text) and "ratio" in text and "eta" in text)

        p.wait_for(everything_is_shown, timeout=90, what="3 file bars, a total, a panel and a peers table")
        self.assertEqual(p.wait_exit(timeout=180), 0)
        text = p.text()
        self.assertRegex(text, r"saved pack \(750\.0 kB, 3 files\)")
        for n in "abc":
            self.assertEqual(sum(1 for line in text.splitlines() if re.match(rf"{n}\.bin\s+\[#{{20}}\]\s+100\.0%", line)), 1, n)
        self.assertEqual(sum(1 for line in text.splitlines() if re.match(r"total\s+\[#{20}\]\s+100\.0%", line)), 1)
        # the panel and the table are frozen into scrollback in their final state, once: the host writes the live
        # areas when the invoke ends, so they come after the `saved` line the plugin printed during it
        self.assertEqual(len(re.findall(r"peer\s+client\s+down\s+up", text)), 1, "the final peers table")
        self.assertEqual(len(re.findall(r"^ratio\b", text, re.M)), 1, "the final panel")
        self.assertEqual((work / "pack" / "a.bin").stat().st_size, 250_000)
        self.assert_restored(p)
        self.assert_only_host_sequences(p)


if __name__ == "__main__":
    unittest.main()
