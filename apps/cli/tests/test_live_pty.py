"""LiveUI in a real pty, fed by tests/fixtures/uidriver.py: AC3, AC5, AC8, AC9, AC13, AC16, AC17 and NFR1, NFR3.

No container is needed. The pty is read through tests/screenkit.py, which also lists every escape sequence seen.
"""

import json
import os
import re
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

import ptykit

DRIVER = str(ROOT / "tests" / "fixtures" / "uidriver.py")

# Everything dock itself may write to the terminal: carriage return, newline, cursor up, clear down, hide/show
# the cursor, and bold around a panel title (the editor adds cursor forward, the screen clear on Ctrl-L).
WHITELIST = re.compile(r"\x1b\[(\d*A|J|K|\d*C|\?25[hl]|1m|0m|H|2J)")
FIRST_FINAL = "first  [####################]  100.0%  100 of 100"


def host_env(**extra):
    env = {k: v for k, v in os.environ.items() if k not in ("NO_COLOR", "DOCK_PLAIN")}
    env.update(PYTHONPATH=str(ROOT), TERM="xterm-256color", **extra)
    return env


@unittest.skipUnless(ptykit.AVAILABLE, "needs pty and termios")
class LivePty(unittest.TestCase):
    def run_driver(self, script, cols=80, rows=24, **env):
        p = ptykit.Pty([sys.executable, DRIVER, script], cols=cols, rows=rows, env=host_env(**env), cwd=ROOT)
        self.addCleanup(p.close)
        return p

    def assert_restored(self, p):
        self.assertEqual(p.termios(), p.initial_termios, "termios is not what it was before")
        self.assertTrue(p.screen.cursor_visible, "the cursor is still hidden")

    def assert_only_host_sequences(self, p):
        with p._cv:
            seqs, controls = list(p.screen.seqs), list(p.screen.controls)
            data = p.data
        for s in seqs:
            self.assertTrue(WHITELIST.fullmatch(s), f"a sequence dock does not send itself: {s!r}")
        self.assertEqual(controls, [], "control characters reached the terminal")
        self.assertIsNone(re.search(rb"[\x80-\x9f]", data.decode("utf-8", "replace").encode("latin-1", "ignore")),
                          "a C1 byte reached the terminal")
        for bad in (b"\x07", b"\x00", b"\x08", b"\x7f", b"\xc2\x9b"):
            self.assertNotIn(bad, data)

    # AC3 ------------------------------------------------------------------------------
    def test_areas_update_in_place_and_scrollback_keeps_one_final_copy(self):
        p = self.run_driver("basic")
        seen_counts = set()
        deadline = time.monotonic() + 30
        while p.events == [] or not any(e.startswith("exit") for e in p.events):
            text = p.text()
            seen_counts.add(len([ln for ln in text.splitlines() if ln.startswith("first ") or ln.startswith("second ")]))
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.005)
        self.assertEqual(p.wait_exit(), 0)
        self.assertLessEqual(max(seen_counts), 2, f"a bar was drawn twice at once: {seen_counts}")
        text = p.text()
        self.assertEqual(text.count(FIRST_FINAL), 1)
        self.assertEqual(text.count("second  [##########..........]  50.0%"), 1)
        for once in ("peers\n", "peer     down", "1.2.3.4  100", "5.6.7.8  200", "stats\nspeed  100 MB/s", "basic done"):
            self.assertEqual(text.count(once), 1, once)
        self.assertNotIn("┌", text, "the live panel was left on screen")
        self.assertEqual(text.count("output line 100"), 1)
        self.assertEqual(text.count("log line 100"), 1)
        self.assert_restored(p)
        self.assert_only_host_sequences(p)

    def test_the_final_lines_come_in_creation_order_after_the_output(self):
        p = self.run_driver("basic")
        p.wait_exit()
        lines = p.text().splitlines()
        self.assertLess(lines.index("basic done"), lines.index(FIRST_FINAL))
        self.assertLess(lines.index(FIRST_FINAL), lines.index("peers"))
        self.assertLess(lines.index("peers"), lines.index("stats"))

    # AC5, NFR1 ---------------------------------------------------------------------------
    def test_hostile_text_in_every_message_reaches_the_terminal_only_as_replacement_characters(self):
        for seed in range(8):
            with self.subTest(seed=seed), tempfile.TemporaryDirectory() as tmp:
                result = os.path.join(tmp, "ok")
                p = self.run_driver("hostile", DOCK_DRIVER_SEED=str(seed), DOCK_DRIVER_RESULT=result)
                deadline = time.monotonic() + 60
                while not any(e.startswith("exit ") for e in p.events):  # Enter answers the hostile questions
                    self.assertLess(time.monotonic(), deadline)
                    p.send("\r")
                    time.sleep(0.05)
                self.assertEqual(p.wait_exit(), 0)
                self.assert_only_host_sequences(p)
                self.assert_restored(p)
                self.assertIn("�", p.text())
                self.assertIn("hostile done", p.text())
                self.assertEqual(json.loads(Path(result).read_text()), {"ok": True},
                                 "the plugin did not get its own choice and default strings back")

    # AC8 ---------------------------------------------------------------------------------
    def test_a_resize_redraws_the_region_at_the_new_width_within_100_ms(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = os.path.join(tmp, "latency")
            p = self.run_driver("hold", cols=100, rows=24, DOCK_DRIVER_RESULT=result)
            p.wait_text("┌ stats")
            p.quiet_for(0.2)
            with p._cv:
                self.assertGreater(max(len(ln) for ln in p.screen.lines()), 90, "the region is not wide to begin with")
            start = time.monotonic()
            p.resize(40, 24)
            p.wait_for(lambda: all(len(ln) <= 40 for ln in p.screen.lines()), what="the region at the new width")
            wall = time.monotonic() - start
            self.assertIn("┌ stats", p.text())
            p.quiet_for(0.2)
            p.resize(70, 24)  # a second measurement: one descheduled moment on a loaded host must not fail the test
            p.wait_for(lambda: max(len(ln) for ln in p.screen.lines()) > 45, what="the region at the wider width")
            p.quiet_for(0.2)
            p.send("q")
            self.assertEqual(p.wait_exit(), 0)
            latencies = json.loads(Path(result).read_text())["resize_latencies"]
        shown = ", ".join(f"{v * 1000:.0f}" for v in latencies)
        print(f"\n  resize: redrawn {shown} ms after SIGWINCH ({wall * 1000:.0f} ms seen from the test)", file=sys.stderr)
        self.assertEqual(len(latencies), 2, latencies)
        self.assertLess(min(latencies), 0.1, f"the resizes took {shown} ms to redraw")  # NFR3: <= 100 ms
        self.assertLess(wall, 2.0)

    # AC7, NFR4 ---------------------------------------------------------------------------
    def test_32_areas_and_a_500_row_table_fit_every_terminal_size(self):
        for cols, rows in ((20, 10), (80, 24), (200, 50)):
            with self.subTest(cols=cols, rows=rows):
                p = self.run_driver("many", cols=cols, rows=rows)
                p.wait_for(lambda: "more rows" in p.text() or "more" in p.text(), what="the cut table")
                p.quiet_for(0.3)
                with p._cv:
                    shown = [line for line in p.screen.lines() if line]
                    scrollback = list(p.screen.scrollback)
                    widest = max(len(line) for line in p.screen.lines())
                self.assertLessEqual(len(shown), rows - 2, shown)
                self.assertLessEqual(widest, cols)
                self.assertEqual([line for line in scrollback if line], [], "the region scrolled the screen")
                self.assertTrue(any("more" in line for line in shown))
                self.assertTrue(p.screen.cursor_visible is False)
                p.send("q")
                self.assertEqual(p.wait_exit(), 0)
                self.assert_restored(p)

    # AC9 live ----------------------------------------------------------------------------
    def test_form_keys_give_the_values_and_the_secret_is_not_echoed(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = os.path.join(tmp, "values")
            p = self.run_driver("form", DOCK_DRIVER_RESULT=result)
            p.wait_text("Your name [Ada]")
            p.send("Bob")
            p.wait_text("Your name [Ada] Bob")
            p.send("\r")
            p.wait_text("Token")
            p.send("s3cr3t")
            p.quiet_for(0.2)
            p.send("\r")
            p.wait_text("Overwrite? (y/N)")
            p.send("y")
            p.wait_text("Mirror")
            p.wait_text("› eu")
            p.send("\x1b[B")
            p.wait_text("› us")
            p.send("\x1b[B\x1b[B")  # asia, and the last option: a third Down stays there
            p.wait_text("› asia")
            p.send("\r")
            self.assertEqual(p.wait_exit(), 0)
            self.assertEqual(json.loads(Path(result).read_text()),
                             {"values": {"name": "Bob", "token": "s3cr3t", "ok": True, "mirror": "asia"}})
        self.assertNotIn(b"s3cr3t", p.data, "the secret was echoed")
        self.assertNotIn(b"s3", p.data)
        self.assert_restored(p)

    def test_enter_takes_the_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = os.path.join(tmp, "values")
            p = self.run_driver("form", DOCK_DRIVER_RESULT=result)
            for label in ("Your name", "Token", "Overwrite?", "Mirror"):
                p.wait_text(label)
                p.send("\r")
            self.assertEqual(p.wait_exit(), 0)
            self.assertEqual(json.loads(Path(result).read_text()),
                             {"values": {"name": "Ada", "token": "", "ok": False, "mirror": "eu"}})

    def test_keys_typed_before_a_form_opens_do_not_answer_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = os.path.join(tmp, "values")
            p = self.run_driver("late_form", DOCK_DRIVER_RESULT=result)
            p.wait_text("READY")
            p.send("zzz\r")
            p.wait_text("Name?")
            p.quiet_for(0.3)
            self.assertNotIn("zzz", p.text(), "keys typed earlier were taken as the answer")
            p.send("Bob\r")
            self.assertEqual(p.wait_exit(), 0)
            self.assertEqual(json.loads(Path(result).read_text()), {"values": {"name": "Bob"}})

    def test_keys_typed_before_a_prompt_opens_do_not_answer_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = os.path.join(tmp, "values")
            p = self.run_driver("late_prompt", DOCK_DRIVER_RESULT=result)
            p.wait_text("READY")
            p.send("zzz\r")
            p.wait_text("Answer:")
            p.quiet_for(0.3)
            self.assertNotIn("zzz", p.text(), "keys typed earlier were taken as the answer")
            p.send("Bob\r")
            self.assertEqual(p.wait_exit(), 0)
            self.assertEqual(json.loads(Path(result).read_text()), {"text": "Bob"})

    def test_ctrl_c_in_an_open_form_cancels_it(self):
        p = self.run_driver("form")
        p.wait_text("Your name")
        p.send("\x03")
        self.assertEqual(p.wait_exit(), 130)
        self.assert_restored(p)

    def test_ctrl_d_in_an_open_form_ends_the_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = os.path.join(tmp, "values")
            p = self.run_driver("form", DOCK_DRIVER_RESULT=result)
            p.wait_text("Your name")
            p.send("\x04")
            self.assertEqual(p.wait_exit(), 0)
            self.assertEqual(json.loads(Path(result).read_text()), {"eof": True})

    # AC17 --------------------------------------------------------------------------------
    def test_the_panel_title_is_bold_unless_no_color_is_set(self):
        p = self.run_driver("hold")
        p.wait_text("┌ stats")
        self.assertIn(b"\x1b[1m", p.data)
        p.send("q")
        p.wait_exit()
        p = self.run_driver("hold", NO_COLOR="1")
        p.wait_text("┌ stats")
        self.assertNotIn(b"\x1b[1m", p.data)
        self.assertNotIn(b"\x1b[0m", p.data)
        p.send("q")
        p.wait_exit()

    # AC13 --------------------------------------------------------------------------------
    def test_sigterm_with_a_region_drawn_erases_it_and_restores_the_terminal(self):
        p = self.run_driver("hold")
        p.wait_text("┌ stats")
        p.signal(signal.SIGTERM)
        self.assertEqual(p.wait_exit(), -signal.SIGTERM)
        self.assert_restored(p)
        self.assertNotIn("┌ stats", p.text())

    def test_sighup_with_a_region_drawn_restores_the_terminal(self):
        p = self.run_driver("hold")
        p.wait_text("┌ stats")
        p.signal(signal.SIGHUP)
        self.assertEqual(p.wait_exit(), -signal.SIGHUP)
        self.assert_restored(p)

    def test_an_exception_in_the_main_thread_erases_the_region_before_the_traceback(self):
        p = self.run_driver("raise")
        self.assertEqual(p.wait_exit(), 1)
        self.assert_restored(p)
        text = p.text()
        self.assertIn("RuntimeError: boom in the main thread", text)
        self.assertNotIn("working", text)

    def test_a_renderer_that_raises_leaves_the_run_and_the_terminal_intact(self):
        p = self.run_driver("broken")
        self.assertEqual(p.wait_exit(), 0)
        self.assert_restored(p)
        self.assertIn("run went on", p.text())
        self.assertIn("a  [####", p.text())  # the final line is still written, by plain_lines

    def test_a_form_after_a_renderer_failure_shows_what_is_typed(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = os.path.join(tmp, "values")
            p = self.run_driver("broken_form", DOCK_DRIVER_RESULT=result)
            p.wait_text("Name?")
            p.send("Bob")
            p.wait_text("Name? Bob")  # echoed: the user does not type blind
            p.send("\r")
            self.assertEqual(p.wait_exit(), 0)
            self.assertEqual(json.loads(Path(result).read_text()), {"values": {"name": "Bob"}})
            self.assert_restored(p)

    def test_ctrl_z_and_fg_draw_the_region_again(self):
        p = self.run_driver("hold")
        p.wait_text("┌ stats")
        p.quiet_for(0.3)  # on macOS a ^Z written just after the mode change can be dropped by the pty
        p.send("\x1a")  # quiet mode keeps ISIG: the pty turns it into SIGTSTP
        p.wait_stopped()
        self.assert_restored(p)
        self.assertNotIn("┌ stats", p.text(), "the region was left on screen while stopped")
        p.cont()
        p.wait_text("┌ stats")
        p.send("q")
        self.assertEqual(p.wait_exit(), 0)

    # AC16, NFR3 --------------------------------------------------------------------------
    def flood(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = os.path.join(tmp, "numbers")
            p = self.run_driver("flood", DOCK_DRIVER_RESULT=result, DOCK_DRIVER_SECONDS="5")
            self.assertEqual(p.wait_exit(timeout=60), 0)
            return p, json.loads(Path(result).read_text())

    def test_10000_updates_a_second_cost_at_most_30_redraws_a_second_and_a_quarter_of_a_core(self):
        # The feeder is part of the test, not of dock: on a loaded host it may fall short of 10,000 updates a second,
        # and then nothing is proved. That one measurement is made again; the limits themselves are never retried.
        for attempt in (1, 2):
            p, n = self.flood()
            sent_rate, rate, cpu = n["sent"] / n["seconds"], n["frames"] / n["seconds"], n["cpu"] / n["seconds"]
            print(f"\n  flood (try {attempt}): {sent_rate:.0f} updates/s, {rate:.1f} frames/s, {cpu * 100:.1f}% of a core "
                  f"(feeder included), {n['cpu'] / n['sent'] * 1e6:.1f} us of CPU per update", file=sys.stderr)
            if sent_rate > 9000:
                break
        self.assertGreater(sent_rate, 9000, "the feeder could not send 10,000 updates a second, twice")
        self.assertLessEqual(rate, 30.5)  # NFR3: at most 30 redraws a second
        self.assertLessEqual(cpu, 0.25)   # NFR3: at most a quarter of a core
        self.assert_only_host_sequences(p)


if __name__ == "__main__":
    unittest.main()
