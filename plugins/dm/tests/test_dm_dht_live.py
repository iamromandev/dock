"""Opt-in live check (AC17, NFR6): the metadata of a public magnet arrives through the real DHT within 2 minutes.

Off by default: it talks to the public Internet. Run it with `DOCK_LIVE_DHT=1 python3 -m unittest tests.test_dm_dht_live`.
It stops as soon as the metadata is known; nothing is downloaded.

The magnet has only an info-hash, so only the DHT can find peers for it. INFO_HASH is a long-lived public
torrent (Big Buck Bunny, the one the README example uses). Swap in the info-hash of another well-seeded public
torrent, a Linux ISO for instance, if this one ever goes quiet.
"""

import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dmcore.target import Target
from dmcore.torrent import magnet, session
from dmcore.torrent.handle import TorrentHandle

INFO_HASH = "c9e15763f722f23e98a29decdfae341b98d53056"
WAIT = 120


@unittest.skipUnless(os.environ.get("DOCK_LIVE_DHT") == "1", "set DOCK_LIVE_DHT=1 to use the real DHT")
class Live(unittest.TestCase):
    def test_the_metadata_of_a_trackerless_magnet_arrives_within_2_minutes(self):
        folder = Path(tempfile.mkdtemp(prefix="dm-live-")).resolve()
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        got, handle, result, logs = threading.Event(), TorrentHandle(), {}, []
        real = session.Session.metadata_complete

        def seen(s, info_bytes):
            got.set()
            return real(s, info_bytes)

        def work():
            try:
                session.run_magnet(magnet.parse(f"magnet:?xt=urn:btih:{INFO_HASH}"), Target(folder, None),
                                   lambda p, x: None, handle, session.Options(stall_s=WAIT), logs.append)
            except BaseException as e:
                result["err"] = e

        with mock.patch.object(session.Session, "metadata_complete", seen):
            thread = threading.Thread(target=work)
            start = time.monotonic()
            thread.start()
            try:
                self.assertTrue(got.wait(WAIT), f"no metadata within {WAIT} s: {logs[-5:]}")
            finally:
                handle.abort()
                thread.join(10)
        print(f"\n[live] metadata after {time.monotonic() - start:.1f} s", file=sys.stderr)


if __name__ == "__main__":
    unittest.main()
