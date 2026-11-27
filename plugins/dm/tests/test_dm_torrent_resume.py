"""Resume for torrents (AC8-AC10, AC12, AC14, AC17, NFR1, NFR2): re-check, reuse, keep and delete.

In one process: the fixture tracker and seeders of test_dm_torrent_local on 127.0.0.1, no container needed.
"""

import os
import shutil
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import torrentkit
from dmcore import Cancelled
from dmcore.target import DmError, Target
from dmcore.torrent import layout, metainfo, session
from dmcore.torrent.handle import TorrentHandle
from test_dm_torrent_local import FAST, PL, Harness


class Resume(Harness):
    """AC8-AC10, AC12, AC17, NFR1, NFR2: run_torrent keeps, reuses and deletes partials."""

    def part_path(self, t, fixed_name=None):
        from dmcore import resume as r

        target = Target(self.cwd, fixed_name)
        meta = metainfo.load(t.write_torrent(self.dir / "k.torrent"))
        base = fixed_name or layout.item_name(meta)
        return r.paths(self.cwd, base, r.torrent_key(meta.info_hash, target)).part

    def go(self, t, fixed_name=None, handle=None, resume=True, opts=FAST, reset=True):
        """run_torrent with the resume arguments; the notes and progress it sent are kept in self."""
        path = t.write_torrent(self.dir / "x.torrent")
        if reset:
            self.notes, self.progress = [], []
        return session.run_torrent(
            path,
            Target(self.cwd, fixed_name),
            lambda pct, text: self.progress.append((pct, text)),
            handle or TorrentHandle(),
            opts,
            None,
            resume=resume,
            note=self.notes.append,
        )

    def interrupt(self, t, seeder, at=40, fixed_name=None):
        """Run until `at` percent are verified, then abort. Returns the exception the run ended with."""
        handle, result = TorrentHandle(), {}
        self.notes, self.progress = [], []

        def work():
            try:
                result["ok"] = self.go(t, fixed_name, handle, reset=False)
            except BaseException as e:
                result["err"] = e

        thread = threading.Thread(target=work)
        thread.start()
        self.assertTrue(
            self.wait_for(lambda: any(p >= at and "peer" in x for p, x in list(self.progress)), 20)
        )
        start = time.monotonic()
        self.assertTrue(handle.abort())
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertLess(time.monotonic() - start, 2)
        self.assertIsInstance(result.get("err"), Cancelled, result)
        return result["err"]

    def on_disk(self, t, part):
        """The piece indexes a partial holds that match the torrent (computed here, not by dm)."""
        data = (
            Path(part).read_bytes()
            if not t.multi
            else b"".join((Path(part) / rel).read_bytes() for rel, _ in t.files)
        )
        return {
            i
            for i in range(-(-len(data) // t.piece_length))
            if data[i * t.piece_length : (i + 1) * t.piece_length]
            == t.blob[i * t.piece_length : (i + 1) * t.piece_length]
            and any(data[i * t.piece_length : (i + 1) * t.piece_length])
        }

    def slow_swarm(self, tr, t, delay=0.01):
        seeder = self.seeder(t, delay=delay)
        self.swarm(tr, seeder)
        return seeder

    def test_a_folder_partial_with_an_extra_file_or_a_symlinked_file_starts_over(self):
        for hostile in ("extra", "link"):
            with self.subTest(hostile):
                tr = self.serve_tracker()
                files = [("a", torrentkit.content(4 * PL, "a")), ("b", torrentkit.content(4 * PL, "b"))]
                t = self.torrent(files, name="set", tr=tr, piece_length=PL)
                part = self.part_path(t)
                part.mkdir()
                outside = self.dir / f"outside-{hostile}"
                outside.write_bytes(b"precious")
                (part / "a").write_bytes(files[0][1])
                if hostile == "extra":
                    (part / "b").write_bytes(files[1][1])
                    (part / "extra").write_bytes(b"x")
                else:
                    os.symlink(outside, part / "b")
                self.swarm(tr, self.seeder(t))
                final, *_ = self.go(t)
                self.assertEqual(torrentkit.tree(final), dict(files))
                self.assertEqual(outside.read_bytes(), b"precious")
                self.assertEqual(self.notes, ["cannot resume: starting over"])
                shutil.rmtree(final)

    def test_a_retryable_failure_after_a_piece_was_verified_in_this_run_keeps_the_partial(self):
        tr = self.serve_tracker()
        t = self.single(size=8 * PL, tr=tr, piece_length=PL)
        self.swarm(tr, self.seeder(t))
        real, calls = session.Storage.write_piece, []

        def flaky(st, index, data):
            calls.append(index)
            if len(calls) > 3:
                time.sleep(0.5)  # let the first pieces be counted as verified
                raise OSError(28, "No space left on device")
            return real(st, index, data)

        with mock.patch.object(session.Storage, "write_piece", flaky):
            with self.assertRaises(DmError) as cm:
                self.go(t)
        self.assertTrue(cm.exception.keep)
        self.assertEqual(self.listing(), [self.part_path(t).name])

    def test_a_resume_that_cannot_read_the_partial_keeps_it_with_a_user_safe_message(self):
        import errno

        tr = self.serve_tracker()
        t = self.single(size=4 * PL, tr=tr, piece_length=PL)
        part = self.part_path(t)
        part.write_bytes(t.blob)
        for where, patch in (
            (
                "open",
                mock.patch.object(
                    session.Storage, "open_existing", side_effect=OSError(errno.EMFILE, "Too many open files")
                ),
            ),
            (
                "read",
                mock.patch.object(
                    session.Storage, "read_block", side_effect=OSError(errno.EIO, "Input/output error")
                ),
            ),
        ):
            with self.subTest(where):
                with patch:
                    with self.assertRaises(DmError) as cm:
                        self.go(t)
                self.assertTrue(cm.exception.keep)
                self.assertNotIn(str(self.cwd), str(cm.exception))
                self.assertEqual(self.listing(), [part.name])
                self.assertEqual(part.read_bytes(), t.blob)

    def test_a_failed_cleanup_never_replaces_the_original_error(self):
        tr = self.serve_tracker(mode="empty")
        t = self.single(size=4 * PL, tr=tr, piece_length=PL)
        with mock.patch.object(session.resume_mod.os, "unlink", side_effect=OSError(13, "Permission denied")):
            with self.assertRaises(DmError) as cm:
                self.go(t, opts=session.Options(dht=False, stall_s=1))
        self.assertEqual(str(cm.exception), "no peers found")


def dmfmt(n):
    from dmcore.fmt import human_size

    return human_size(n)


if __name__ == "__main__":
    unittest.main()
