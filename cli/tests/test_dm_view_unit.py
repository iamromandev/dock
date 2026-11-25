"""dm 0.5.0 live view (phase C, step 17): the messages a torrent run sends, throttled to 5 a second."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "plugins" / "dm"))

from dmcore import view


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def stats(files, peers=(), down=0.0, up=0.0, known=None, eta=None, ratio=0.0):
    return {"files": [(i, *f) for i, f in enumerate(files)], "down_rate": down, "up_rate": up, "peers_connected": len(peers),
            "peers_known": known if known is not None else len(peers), "eta_s": eta, "ratio": ratio,
            "peers": list(peers)}


class Harness(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.events = []
        self.view = view.TorrentView(lambda method, **p: self.events.append((method, p)), self.clock)

    def sent(self, method=None, id=None):
        return [(m, p) for m, p in self.events if (method is None or m == method) and (id is None or p.get("id") == id)]

    def tick(self, seconds=0.25):
        self.clock.now += seconds


FILES = [("d/a", 0, 40000), ("d/b", 0, 10000), ("d/c", 0, 30000)]


class Messages(Harness):
    def test_one_bar_per_file_a_panel_and_a_peers_table(self):
        peers = [(("10.0.0.1", 6881), "qBittorrent 4.6.5", 1500.0, 20.0)]
        self.view.stats(lambda: stats([("d/a", 20000, 40000), *FILES[1:]], peers, 1500.0, 20.0, known=4, eta=95,
                                      ratio=0.5))
        bars = {p["id"]: p for m, p in self.events if m == "bar"}
        self.assertEqual(sorted(bars), ["f0", "f1", "f2"])
        self.assertEqual((bars["f0"]["label"], bars["f0"]["percent"], bars["f0"]["message"]),
                         ("d/a", 50.0, "20.0 kB of 40.0 kB"))
        panel = self.sent("panel")[0][1]
        self.assertEqual(panel["id"], "stats")
        self.assertEqual(dict(map(tuple, panel["items"])),
                         {"down": "1.5 kB/s", "up": "20 B/s", "peers": "1 of 4", "eta": "1m 35s", "ratio": "0.50"})
        table = self.sent("table")[0][1]
        self.assertEqual((table["id"], table["columns"]), ("peers", ["peer", "client", "down", "up"]))
        self.assertEqual(table["rows"], [["10.0.0.1:6881", "qBittorrent 4.6.5", "1.5 kB/s", "20 B/s"]])

    def test_the_total_bar_follows_progress_and_a_magnet_starts_with_an_unknown_percent(self):
        self.view.progress(None, "fetching metadata · 2 peers")
        self.assertEqual(self.sent("bar", "total")[0][1],
                         {"id": "total", "label": "total", "percent": None, "message": "fetching metadata · 2 peers"})

    def test_a_single_file_torrent_has_only_the_total_bar_and_the_panel(self):
        self.view.stats(lambda: stats([]))  # the session reports no files for one: the total bar says it all
        self.assertEqual(self.sent("bar"), [])
        self.assertEqual(len(self.sent("panel")), 1)

    def test_only_the_eight_largest_unfinished_files_get_a_bar(self):
        files = [(f"d/{i}", 0, 100 + i) for i in range(12)]
        self.view.stats(lambda: stats(files))
        ids = sorted(p["id"] for m, p in self.events if m == "bar")
        self.assertEqual(ids, sorted(f"f{i}" for i in range(4, 12)))

    def test_a_file_that_finishes_gets_one_done_bar_and_its_place_goes_to_the_next(self):
        files = [(f"d/{i}", 0, 100 + i) for i in range(10)]
        self.view.stats(lambda: stats(files))
        self.tick()
        files[9] = ("d/9", 109, 109)
        self.view.stats(lambda: stats(files))
        self.tick()
        self.view.stats(lambda: stats(files))
        done = [p for m, p in self.events if m == "bar" and p.get("done")]
        self.assertEqual([p["id"] for p in done], ["f9"])
        self.assertEqual(done[0]["percent"], 100.0)
        self.assertIn("f1", {p["id"] for m, p in self.events if m == "bar"})  # the ninth largest took the slot

    def test_a_file_that_was_never_shown_gets_no_bar_when_it_finishes(self):
        files = [(f"d/{i}", 0, 100 + i) for i in range(12)]
        self.view.stats(lambda: stats(files))
        self.tick()
        files[0] = ("d/0", 100, 100)
        self.view.stats(lambda: stats(files))
        self.assertEqual(self.sent("bar", "f0"), [])

    def test_no_more_than_28_file_bars_are_ever_introduced(self):
        files = [(f"d/{i}", 0, 1000 - i) for i in range(100)]
        for _ in range(100):
            self.tick()
            self.view.stats(lambda: stats(files))
            for i, (path, done, size) in enumerate(files):
                if size > 0 and done == 0 and f"f{i}" in {p["id"] for _, p in self.sent("bar")}:
                    files[i] = (path, size, size)
        self.assertEqual(len({p["id"] for _, p in self.sent("bar")}), 28)

    def test_an_unchanged_state_sends_nothing_again(self):
        self.view.stats(lambda: stats(FILES))
        before = len(self.events)
        self.tick()
        self.view.stats(lambda: stats(FILES))
        self.assertEqual(len(self.events), before)

    def test_at_most_peers_ten_rows_sorted_as_given(self):
        peers = [((f"10.0.0.{i}", 1), "?", float(100 - i), 0.0) for i in range(15)]
        self.view.stats(lambda: stats(FILES, peers))
        rows = self.sent("table")[0][1]["rows"]
        self.assertEqual(len(rows), 10)
        self.assertEqual(rows[0][0], "10.0.0.0:1")

    def test_an_ipv6_peer_is_bracketed(self):
        self.view.stats(lambda: stats(FILES, [(("::1", 80), "?", 1.0, 0.0)]))
        self.assertEqual(self.sent("table")[0][1]["rows"][0][0], "[::1]:80")


class Throttling(Harness):
    def test_at_most_five_updates_a_second_for_each_id(self):
        times = {}
        self.view._notify = lambda method, **p: times.setdefault((method, p["id"]), []).append(self.clock.now)
        for step in range(300):  # 30 s in 0.1 s steps, a changed state every time
            self.tick(0.1)
            self.view.progress(step % 99, f"{step} bytes")
            self.view.stats(lambda s=step: stats([("d/a", s, 400), ("d/b", 0, 100)], [(("1.1.1.1", 1), "?", float(s), 0.0)],
                                                 down=float(s)))
        self.assertTrue(times)
        for key, at in times.items():
            gaps = [round(b - a, 6) for a, b in zip(at, at[1:])]
            self.assertGreaterEqual(min(gaps, default=1), 0.2, key)  # a 0.15 s throttle would fail here
        self.assertGreater(len(times[("panel", "stats")]), 100)

    def test_the_stats_are_not_even_computed_between_updates(self):
        calls = []
        self.view.stats(lambda: calls.append(1) or stats(FILES))
        self.tick(0.05)
        self.view.stats(lambda: calls.append(1) or stats(FILES))
        self.assertEqual(len(calls), 1)

    def test_a_final_update_always_passes(self):
        """R22's exemption: the final update of a run is not held back by the 5 a second cap."""
        self.view.progress(10, "a")
        self.view.stats(lambda: stats(FILES))
        self.view.stats(lambda: stats(FILES, [(("1.1.1.1", 1), "?", 5.0, 0.0)]), final=True)
        self.assertEqual(len(self.sent("table")), 1)


class Labels(Harness):
    LONG = "Some Show Season 1/Some.Show.S01.1080p.WEB-DL.DDP5.1.H.264-GROUP.E0{}.mkv"

    def labels(self, files):
        self.view.stats(lambda: stats(files))
        return {p["id"]: p["label"] for m, p in self.events if m == "bar"}

    def test_a_long_name_keeps_its_tail_so_names_that_differ_at_the_end_differ(self):
        labels = self.labels([(self.LONG.format(1), 0, 500), (self.LONG.format(2), 0, 400)])
        self.assertNotEqual(labels["f0"], labels["f1"])
        for i, label in enumerate(labels.values(), 1):
            self.assertTrue(label.startswith("…"), label)
            self.assertTrue(self.LONG.format(i).endswith(label[1:]), label)
            self.assertLessEqual(len(label), 29)
        self.assertIn("E01", labels["f0"])

    def test_a_short_name_is_sent_as_it_is(self):
        self.assertEqual(self.labels([("d/short.bin", 0, 500), ("d/b", 0, 400)])["f0"], "d/short.bin")

    def test_the_tail_is_measured_in_display_width(self):
        name = "dir/" + "日本語" * 12 + "終.mkv"  # wide characters take two cells each
        label = self.labels([(name, 0, 500), ("x", 0, 1)])["f0"]
        self.assertEqual(label[0], "…")
        self.assertTrue(name.endswith(label[1:]))
        self.assertLessEqual(view.display_width(label), 29)
        self.assertGreater(view.display_width(label), 25)

    def test_the_done_bar_has_the_same_label(self):
        files = [(self.LONG.format(1), 0, 500), ("d/b", 0, 400)]
        first = self.labels(files)["f0"]
        self.tick()
        files[0] = (files[0][0], 500, 500)
        self.view.stats(lambda: stats(files))
        done = [p for m, p in self.events if m == "bar" and p.get("done")]
        self.assertEqual(done[0]["label"], first)


class Totals(Harness):
    def test_a_total_the_throttle_dropped_is_sent_at_the_next_stats_update(self):
        self.view.progress(10, "a")
        self.view.stats(lambda: stats(FILES))
        self.tick(0.1)
        self.view.progress(20, "b")  # inside the 0.2 s gate of the total bar: dropped
        self.assertEqual([p["message"] for _, p in self.sent("bar", "total")], ["a"])
        self.tick(0.15)
        self.view.stats(lambda: stats(FILES))
        self.assertEqual([p["message"] for _, p in self.sent("bar", "total")], ["a", "b"])
        self.tick()
        self.view.stats(lambda: stats(FILES))
        self.assertEqual(len(self.sent("bar", "total")), 2)  # nothing new: nothing sent again


class Caps(Harness):
    def rows(self, *files):
        return {**stats([]), "files": list(files)}

    def test_the_cap_on_file_bars_does_not_stop_the_update_of_one_already_shown(self):
        for i in range(27):  # with the small file, 28 bars in all: the cap
            self.tick()
            self.view.stats(lambda i=i: self.rows((0, "d/small", 0, 10), (i + 1, f"d/f{i}", 0, 100 + i)))
        self.tick()
        # the huge file sorts first and may not get a bar; the small one, already shown, still moves
        self.view.stats(lambda: self.rows((100, "d/huge", 0, 9999), (0, "d/small", 5, 10)))
        self.assertEqual(self.sent("bar", "f100"), [])
        self.assertEqual(self.sent("bar", "f0")[-1][1]["percent"], 50.0)


class Ratio(Harness):
    def test_nothing_downloaded_this_run_shows_a_dash_not_0_00(self):
        self.view.stats(lambda: stats(FILES, ratio=None))
        self.assertEqual(dict(map(tuple, self.sent("panel")[0][1]["items"]))["ratio"], "-")


class Ending(Harness):
    def run_to_the_end(self):
        self.view.progress(40, "16.0 kB of 80.0 kB · 1 peer")
        self.view.stats(lambda: stats([("d/a", 20000, 40000), *FILES[1:]], [(("10.0.0.1", 1), "?", 1.0, 0.0)]))
        self.tick()
        self.view.progress(100, "80.0 kB of 80.0 kB · 1 peer")
        self.view.stats(lambda: stats([("d/a", 40000, 40000), ("d/b", 10000, 10000), ("d/c", 30000, 30000)],
                                      [(("10.0.0.1", 1), "?", 1.0, 0.0)]), final=True)

    def test_on_success_every_shown_bar_is_done_and_the_panel_and_table_stay_for_the_host_to_freeze(self):
        self.run_to_the_end()
        self.view.finish()
        done = sorted(p["id"] for m, p in self.events if m == "bar" and p.get("done"))
        self.assertEqual(done, ["f0", "f1", "f2", "total"])
        total = [p for m, p in self.events if m == "bar" and p["id"] == "total"][-1]
        self.assertEqual((total["percent"], total["message"]), (100, "80.0 kB of 80.0 kB · 1 peer"))
        self.assertEqual([m for m, p in self.events if m == "clear"], [])  # the host writes them at the invoke's end
        self.assertEqual(self.sent("panel")[-1][1]["items"][0], ["down", "0 B/s"])  # the final update is the last
        self.assertEqual(self.sent("table")[-1][1]["rows"][0][0], "10.0.0.1:1")

    def test_on_failure_everything_shown_is_cleared(self):
        self.view.progress(40, "x")
        self.view.stats(lambda: stats(FILES, [(("10.0.0.1", 1), "?", 1.0, 0.0)]))
        self.view.drop()
        cleared = sorted((p["kind"], p["id"]) for m, p in self.events if m == "clear")
        self.assertEqual(cleared, [("bar", "f0"), ("bar", "f1"), ("bar", "f2"), ("bar", "total"),
                                   ("panel", "stats"), ("table", "peers")])
        self.assertFalse([1 for m, p in self.events if m == "bar" and p.get("done")])

    def test_on_failure_the_panel_and_table_are_emptied_first_so_a_piped_run_prints_nothing_of_them(self):
        self.view.stats(lambda: stats(FILES, [(("10.0.0.1", 1), "?", 1.0, 0.0)]))
        self.view.drop()
        names = [(m, p["id"]) for m, p in self.events]
        for kind, id in (("panel", "stats"), ("table", "peers")):
            before = [p for m, p in self.events[:names.index(("clear", id))] if m == kind][-1]
            self.assertEqual((before.get("items"), before.get("columns"), before.get("rows")),
                             ([], None, None) if kind == "panel" else (None, [], []), kind)

    def test_dropping_a_view_that_showed_nothing_sends_nothing(self):
        self.view.drop()
        self.assertEqual(self.events, [])


class Formats(unittest.TestCase):
    def test_rates_and_etas(self):
        self.assertEqual(view.human_rate(0), "0 B/s")
        self.assertEqual(view.human_rate(999), "999 B/s")
        self.assertEqual(view.human_rate(2_500_000), "2.5 MB/s")
        self.assertEqual(view.human_eta(None), "-")
        self.assertEqual(view.human_eta(7), "7s")
        self.assertEqual(view.human_eta(95), "1m 35s")
        self.assertEqual(view.human_eta(3 * 3600 + 5 * 60 + 9), "3h 05m")
        self.assertEqual(view.human_eta(10 * 86400), "> 99h")


if __name__ == "__main__":
    unittest.main()
