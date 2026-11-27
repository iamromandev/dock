"""Unit tests for dm resume: the resume module (keys, records, lock, discard) and HTTP resume in fetch.download."""

import errno
import hashlib
import http.server
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "fixtures" / "dmserver"))

import dmcore
import server as dmserver
from dmcore import fetch, fmt, resume
from dmcore.target import DmError, Target

KEY = "a" * 24


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(os.path.realpath(self.tmp.name))

    def left(self, folder=None):
        return sorted(os.listdir(folder or self.dir))


class Locks(Base):
    def test_symlink_in_place_of_lock_is_replaced_not_followed(self):
        victim = self.dir / "victim"
        os.symlink(victim, self.dir / "l")
        lock = resume.Lock(self.dir / "l")
        lock.acquire()
        lock.release(remove=True)
        self.assertFalse(victim.exists())
        self.assertEqual(self.left(), [])

    def test_unsupported_locking_runs_unlocked(self):
        for code in (errno.ENOLCK, errno.EOPNOTSUPP, errno.EINVAL):
            with (
                self.subTest(code=code),
                mock.patch.object(resume.fcntl, "flock", side_effect=OSError(code, "x")),
            ):
                lock = resume.Lock(self.dir / "l")
                lock.acquire()
                lock.release(remove=True)


# ---- HTTP resume through fetch.download -------------------------------------------------------------------------

SIZE = 400_000
CUT = 150_000
TAIL = 10_000  # the re-fetched tail (fetch.TAIL is 1 MiB; small here so the 400 kB files resume)
START = CUT - TAIL


class ResumeCase(Base):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), dmserver.Handler)
        cls.server.daemon_threads = True
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        super().setUp()
        dmserver.LOG.clear()
        dmserver.SLOW_SEEN.clear()
        dmserver.OVERRIDE.clear()
        self.addCleanup(dmserver.OVERRIDE.clear)
        patch = mock.patch.object(fetch, "TAIL", TAIL)
        patch.start()
        self.addCleanup(patch.stop)
        self.notes = []

    def url(self, name="f.bin", **q):
        q = {"size": SIZE, **q}
        return f"{self.base}/range/{name}?" + "&".join(f"{k}={v}" for k, v in q.items())

    def mode(self, name="f.bin", **flags):
        """Change how the server answers /range/<name> from now on (the URL, and so the key, stays the same)."""
        dmserver.OVERRIDE[f"/range/{name}"] = flags

    def get(self, url=None, name=None, progress=None, cancelled=None, folder=None, **kw):
        t = Target(folder or self.dir, name)
        kw.setdefault("note", self.notes.append)
        return fetch.download(
            url or self.url(), t, progress or (lambda p, m: None), cancelled or threading.Event(), **kw
        )

    def interrupt(self, url=None, **flags):
        """A first run that the server drops at CUT bytes: fails retryably and keeps the partial."""
        self.mode(**{"drop_at": CUT, **flags})
        with self.assertRaises(DmError) as cm:
            self.get(url)
        self.assertTrue(cm.exception.keep, str(cm.exception))
        self.mode(**flags)
        return cm.exception

    def cancel_at(self, percent, url=None, **kw):
        cancelled = threading.Event()
        with mock.patch.object(fetch, "CHUNK", 4096):
            with self.assertRaises(dmcore.Cancelled):
                self.get(
                    url,
                    cancelled=cancelled,
                    throttle=fmt.Throttle(0),
                    progress=lambda p, m: cancelled.set() if p >= percent else None,
                    **kw,
                )

    def hidden(self, suffix):
        return [f for f in self.left() if f.endswith(suffix)]

    def kept(self):
        return len(self.hidden(".dm-part")), len(self.hidden(".dm-resume"))

    def ranges(self):
        return [e["range"] for e in dmserver.LOG if e["path"].startswith("/range/")]

    def assertBody(self, path, version=0, size=SIZE):
        self.assertEqual(
            hashlib.sha256(Path(path).read_bytes()).hexdigest(),
            hashlib.sha256(dmserver.content(size, version)).hexdigest(),
        )


class ResumeHttp(ResumeCase):
    # AC1, AC2, AC12

    # NFR1: two interruptions, then the digest of the source

    def test_invalid_url_and_redirect_loops_are_not_retryable(self):
        for url in ("ftp://h/x", "http://[::1/x", f"{self.base}/redirect/20"):
            with self.subTest(url=url), self.assertRaises(DmError) as cm:
                self.get(url)
            self.assertFalse(cm.exception.keep)

    # AC3

    def lie_on_range(self, header, value):
        real = fetch._open

        def lying(url, handle, headers=None):
            resp = real(url, handle, headers)
            if headers:
                del resp.headers[header]
                resp.headers[header] = value
            return resp

        return mock.patch.object(fetch, "_open", lying)

    def test_a_206_that_does_not_fit_the_record_restarts(self):
        """N7: wrong total in Content-Range, a content encoding, a wrong Content-Length."""
        cases = {
            "total": ("Content-Range", f"bytes {START}-{SIZE - 1}/{SIZE + 1}"),
            "encoding": ("Content-Encoding", "gzip"),
            "length": ("Content-Length", str(SIZE - START - 1)),
        }
        for name, (header, value) in cases.items():
            with self.subTest(name):
                self.interrupt()
                dmserver.LOG.clear()
                self.notes.clear()
                with (
                    self.lie_on_range(header, value)
                    if header != "Content-Encoding"
                    else self.add_header(header, value)
                ):
                    path, _ = self.get()
                self.assertBody(path)
                self.assertEqual(self.ranges(), [f"bytes={START}-", None])
                self.assertEqual(self.notes, ["cannot resume: starting over"])
                self.assertEqual(self.left(), ["f.bin"])
                path.unlink()

    def add_header(self, header, value):
        real = fetch._open

        def lying(url, handle, headers=None):
            resp = real(url, handle, headers)
            if headers:
                resp.headers[header] = value
            return resp

        return mock.patch.object(fetch, "_open", lying)

    def test_partial_smaller_than_the_tail_is_fetched_again_from_the_start(self):
        """N1: below fetch.TAIL the partial is not worth resuming: a plain GET, no record needed."""
        self.interrupt()
        with mock.patch.object(fetch, "TAIL", CUT + 1):
            path, _ = self.get()
        self.assertBody(path)
        self.assertEqual(self.ranges(), [None, None])
        self.assertEqual(self.left(), ["f.bin"])

    def test_empty_partial_without_a_record_starts_quietly(self):
        """N5."""
        p = resume.paths(self.dir, "f.bin", resume.http_key(self.url(), Target(self.dir, None)))
        p.part.write_bytes(b"")
        path, _ = self.get()
        self.assertBody(path)
        self.assertEqual(self.notes, [])

    def test_stale_record_temp_files_of_this_download_are_removed(self):
        """N4: not those of other downloads, and a symlink is removed, not followed."""
        key = resume.http_key(self.url(), Target(self.dir, None))
        p = resume.paths(self.dir, "f.bin", key)
        outside = self.dir.parent / f"outside-{os.getpid()}"
        outside.write_text("precious")
        self.addCleanup(outside.unlink, missing_ok=True)
        mine = [Path(f"{p.record}.0123456789abcdef.tmp"), Path(f"{p.record}.fedcba9876543210.tmp")]
        mine[0].write_bytes(b"{}")
        os.symlink(outside, mine[1])
        other = self.dir / ".g.bin.bbbbbbbbbbbbbbbbbbbbbbbb.dm-resume.0123456789abcdef.tmp"
        other.write_bytes(b"{}")
        self.get()
        self.assertEqual(self.left(), [other.name, "f.bin"])
        self.assertEqual(outside.read_text(), "precious")

    def test_abort_wakes_a_request_the_server_never_answers(self):
        """N3: the worker lets go of the lock soon after the abort, so a rerun is not 'already being downloaded'."""
        import time

        self.interrupt()
        self.mode(stall=30)
        handle = fetch.Handle()
        result = []

        def worker():
            try:
                self.get(handle=handle)
            except BaseException as e:
                result.append(e)

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        for _ in range(200):
            if len(self.ranges()) >= 2:
                break
            time.sleep(0.05)
        self.assertEqual(len(self.ranges()), 2)
        time.sleep(0.3)
        started = time.monotonic()
        handle.abort()
        t.join(5)
        self.assertFalse(t.is_alive(), "the worker is still waiting for the server")
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(len(result), 1)
        self.assertEqual(self.kept(), (1, 1))
        self.mode()
        self.assertBody(self.get()[0])

    # AC4

    def test_4xx_on_the_range_request_retries_fresh(self):
        self.interrupt()
        self.mode(fail_resume=416)
        path, _ = self.get()
        self.assertBody(path)
        self.assertEqual(self.notes, ["cannot resume: starting over"])
        self.assertEqual(self.ranges(), [None, f"bytes={START}-", None])

    # AC7
    def interrupt_complete(self):
        """A partial that holds the whole file (as if killed right before the final move)."""
        with mock.patch.object(fetch, "_place", side_effect=OSError(errno.ENOSPC, "No space left on device")):
            with self.assertRaises(DmError):
                self.get()
        self.assertEqual((self.dir / self.hidden(".dm-part")[0]).stat().st_size, SIZE)

    # AC5

    # AC6

    # AC10

    # AC13
    def test_hostile_partial_and_record_start_over(self):
        self.interrupt()
        part = self.dir / self.hidden(".dm-part")[0]
        record = self.dir / self.hidden(".dm-resume")[0]
        outside = self.dir.parent / f"outside-{os.getpid()}"
        outside.write_text("precious")
        self.addCleanup(outside.unlink, missing_ok=True)
        good = record.read_text()

        def reset():
            resume.discard(part, record)
            part.write_bytes(dmserver.content(SIZE)[:CUT])
            record.write_text(good)

        cases = {
            "size larger than total": lambda: part.write_bytes(b"x" * (SIZE + 1)),
            "empty partial": lambda: part.write_bytes(b""),
            "record garbage": lambda: record.write_bytes(b"\x00garbage"),
            "record oversize": lambda: record.write_bytes(good.encode() + b" " * 5000),
            "record symlink": lambda: (record.unlink(), os.symlink(outside, record)),
            "partial symlink": lambda: (part.unlink(), os.symlink(outside, part)),
            "partial is a directory": lambda: (part.unlink(), part.mkdir()),
            "record missing": lambda: record.unlink(),
            "partial missing": lambda: part.unlink(),
        }
        for name, break_it in cases.items():
            with self.subTest(name):
                reset()
                break_it()
                self.notes.clear()
                path, _ = self.get()
                self.assertBody(path)
                self.assertEqual(self.notes, ["cannot resume: starting over"])
                self.assertEqual(outside.read_text(), "precious")
                self.assertEqual(self.left(), ["f.bin"])
                path.unlink()

    # AC11
    def test_second_run_in_the_same_folder_fails_at_once(self):
        self.interrupt()
        p = resume.paths(self.dir, "f.bin", resume.http_key(self.url(), Target(self.dir, None)))
        with resume.Lock(p.lock):
            with self.assertRaises(DmError) as cm:
                self.get()
            self.assertEqual(str(cm.exception), "already being downloaded in this folder")
            self.assertEqual(self.kept(), (1, 1))  # the holder's data is untouched
            self.assertEqual(self.ranges(), [None])  # and nothing was requested
        self.assertBody(self.get()[0])

    # AC17


class ResumeRun(ResumeCase):
    def run_dm(self, **inputs):
        calls = []
        code = dmcore.run(
            {"url": self.url(), **inputs},
            str(self.dir),
            lambda m, **p: calls.append((m, p)),
            threading.Event(),
        )
        return code, calls

    def test_notes_go_out_as_output_lines(self):
        self.mode(drop_at=CUT)
        self.assertEqual(self.run_dm()[0], 1)
        self.mode()
        code, calls = self.run_dm()
        self.assertEqual(code, 0)
        outputs = [p["text"] for m, p in calls if m == "output"]
        self.assertEqual(outputs, ["resuming from 140.0 kB of 400.0 kB\n", "saved f.bin (400.0 kB)\n"])
        self.assertEqual(self.ranges()[-1], f"bytes={START}-")


if __name__ == "__main__":
    unittest.main()
