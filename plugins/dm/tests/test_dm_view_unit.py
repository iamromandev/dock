"""dm 0.5.0 live view (phase C, step 17): the messages a torrent run sends, throttled to 5 a second."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dmcore import view


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def stats(files, peers=(), down=0.0, up=0.0, known=None, eta=None, ratio=0.0):
    return {
        "files": [(i, *f) for i, f in enumerate(files)],
        "down_rate": down,
        "up_rate": up,
        "peers_connected": len(peers),
        "peers_known": known if known is not None else len(peers),
        "eta_s": eta,
        "ratio": ratio,
        "peers": list(peers),
    }


class Harness(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.events = []
        self.view = view.TorrentView(lambda method, **p: self.events.append((method, p)), self.clock)

    def sent(self, method=None, id=None):
        return [
            (m, p)
            for m, p in self.events
            if (method is None or m == method) and (id is None or p.get("id") == id)
        ]

    def tick(self, seconds=0.25):
        self.clock.now += seconds


FILES = [("d/a", 0, 40000), ("d/b", 0, 10000), ("d/c", 0, 30000)]


class Labels(Harness):
    LONG = "Some Show Season 1/Some.Show.S01.1080p.WEB-DL.DDP5.1.H.264-GROUP.E0{}.mkv"

    def labels(self, files):
        self.view.stats(lambda: stats(files))
        return {p["id"]: p["label"] for m, p in self.events if m == "bar"}

    def test_the_done_bar_has_the_same_label(self):
        files = [(self.LONG.format(1), 0, 500), ("d/b", 0, 400)]
        first = self.labels(files)["f0"]
        self.tick()
        files[0] = (files[0][0], 500, 500)
        self.view.stats(lambda: stats(files))
        done = [p for m, p in self.events if m == "bar" and p.get("done")]
        self.assertEqual(done[0]["label"], first)


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
