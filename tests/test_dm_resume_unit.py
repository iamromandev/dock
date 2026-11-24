"""Unit tests for dm resume: the resume module (keys, records, lock, discard) and HTTP resume in fetch.download."""

import errno
import hashlib
import http.server
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "plugin" / "dm"))
sys.path.insert(0, str(ROOT / "tests" / "fixtures" / "dmserver"))

import dmcore  # noqa: E402
import server as dmserver  # noqa: E402
from dmcore import fetch, fmt, resume  # noqa: E402
from dmcore.target import DmError, Target  # noqa: E402

KEY = "a" * 24


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(os.path.realpath(self.tmp.name))

    def left(self, folder=None):
        return sorted(os.listdir(folder or self.dir))


class Keys(Base):
    def test_same_download_same_key_and_right_length(self):
        t = Target(self.dir, None)
        key = resume.http_key("http://h/a", t)
        self.assertEqual(key, resume.http_key("http://h/a", t))
        self.assertRegex(key, r"^[0-9a-f]{24}$")

    def test_key_differs_by_url_output_and_folder(self):
        t = Target(self.dir, None)
        keys = {resume.http_key("http://h/a", t), resume.http_key("http://h/b", t),
                resume.http_key("http://h/a?x=1", t), resume.http_key("http://h/a", Target(self.dir, "o")),
                resume.http_key("http://h/a", Target(self.dir / "sub", None))}
        self.assertEqual(len(keys), 5)

    def test_key_ignores_userinfo_and_fragment(self):
        t = Target(self.dir, None)
        self.assertEqual(resume.http_key("http://u:p@h/a?x=1#f", t), resume.http_key("http://h/a?x=1", t))

    def test_torrent_key_differs_by_hash_and_target(self):
        t = Target(self.dir, None)
        self.assertNotEqual(resume.torrent_key(b"1" * 20, t), resume.torrent_key(b"2" * 20, t))
        self.assertNotEqual(resume.torrent_key(b"1" * 20, t), resume.torrent_key(b"1" * 20, Target(self.dir, "x")))

    def test_names_are_hidden_distinct_from_old_random_names_and_fit(self):
        p = resume.paths(self.dir, "a" * 400, KEY)
        for path, suffix in ((p.part, ".dm-part"), (p.record, ".dm-resume"), (p.lock, ".dm-lock")):
            self.assertTrue(path.name.startswith("."))
            self.assertTrue(path.name.endswith(suffix))
            self.assertLessEqual(len((path.name + ".0123456789abcdef.tmp").encode()), 255)
        self.assertRegex(p.part.name, r"^\.a{100}\.a{24}\.dm-part$")
        lock = resume.torrent_lock(self.dir, KEY)  # key only: a magnet does not know its name yet
        self.assertEqual(lock, self.dir / f".dm.{KEY}.dm-lock")
        self.assertNotRegex(resume.paths(self.dir, "x.bin", KEY).part.name, r"\.[0-9a-f]{16}\.dm-part$")


class Records(Base):
    def rec(self, **kw):
        return resume.Record(**{"total": 100, "etag": '"e1"', "last_modified": None, **kw})

    def test_round_trip_leaves_no_temp_files(self):
        path = self.dir / "r"
        resume.write_record(path, KEY, self.rec(last_modified="Tue, 14 Nov 2023 22:13:20 GMT"))
        self.assertEqual(self.left(), ["r"])
        self.assertEqual(resume.read_record(path, KEY), self.rec(last_modified="Tue, 14 Nov 2023 22:13:20 GMT"))

    def test_record_holds_no_url(self):
        path = self.dir / "r"
        resume.write_record(path, KEY, self.rec())
        self.assertEqual(set(json.loads(path.read_text())), {"v", "key", "total", "etag", "lm"})

    def test_validator_prefers_etag(self):
        self.assertEqual(self.rec(last_modified="d").validator(), '"e1"')
        self.assertEqual(self.rec(etag=None, last_modified="d").validator(), "d")

    def write(self, data, name="r"):
        path = self.dir / name
        path.write_bytes(data if isinstance(data, bytes) else json.dumps(data).encode())
        return path

    def good(self, **kw):
        return {"v": 1, "key": KEY, "total": 100, "etag": '"e1"', "lm": None, **kw}

    def test_hostile_records_are_ignored(self):
        cases = {
            "garbage": b"\xff\x00not json",
            "not an object": b"[1,2]",
            "deep nesting": b"[" * 3000,
            "oversize": json.dumps(self.good(etag='"' + "x" * 300 + '"')).encode() + b" " * 5000,
            "wrong version": self.good(v=2),
            "other key": self.good(key="b" * 24),
            "zero total": self.good(total=0),
            "negative total": self.good(total=-5),
            "bool total": self.good(total=True),
            "float total": self.good(total=1.5),
            "string total": self.good(total="100"),
            "weak etag": self.good(etag='W/"e"'),
            "no validator": self.good(etag=None, lm=None),
            "empty etag": self.good(etag=""),
            "long etag": self.good(etag='"' + "x" * 300 + '"'),
            "newline in etag": self.good(etag='"a\nb"'),
            "non ascii lm": self.good(etag=None, lm="Tue é"),
            "etag not string": self.good(etag=5),
        }
        for name, data in cases.items():
            with self.subTest(name):
                self.assertIsNone(resume.read_record(self.write(data), KEY))

    def test_missing_directory_and_fifo_are_ignored(self):
        self.assertIsNone(resume.read_record(self.dir / "none", KEY))
        (self.dir / "d").mkdir()
        self.assertIsNone(resume.read_record(self.dir / "d", KEY))
        os.mkfifo(self.dir / "fifo")
        self.assertIsNone(resume.read_record(self.dir / "fifo", KEY))

    def test_symlink_record_is_not_followed(self):
        outside = self.dir / "outside"
        outside.write_text(json.dumps(self.good()))
        os.symlink(outside, self.dir / "link")
        self.assertIsNone(resume.read_record(self.dir / "link", KEY))

    def test_write_record_never_writes_through_a_symlink(self):
        victim = self.dir / "victim"
        os.symlink(victim, self.dir / "r")
        resume.write_record(self.dir / "r", KEY, self.rec())  # replaces the link itself
        self.assertFalse(victim.exists())
        self.assertFalse((self.dir / "r").is_symlink())

    def test_record_from_headers(self):
        h = {"ETag": '"abc"', "Last-Modified": "Tue, 14 Nov 2023 22:13:20 GMT"}
        self.assertEqual(resume.record_from_headers(h, 10), resume.Record(10, '"abc"', h["Last-Modified"]))
        self.assertEqual(resume.record_from_headers({"ETag": 'W/"abc"'}, 10), None)
        self.assertEqual(resume.record_from_headers({"ETag": 'W/"abc"', "Last-Modified": "d"}, 10),
                         resume.Record(10, None, "d"))
        self.assertIsNone(resume.record_from_headers(h, None))
        self.assertIsNone(resume.record_from_headers(h, 0))
        self.assertIsNone(resume.record_from_headers({}, 10))
        self.assertIsNone(resume.record_from_headers({"ETag": '"a\nb"'}, 10))


class Locks(Base):
    def test_second_holder_fails_at_once(self):
        path = self.dir / "l"
        first = resume.Lock(path)
        first.acquire()
        with self.assertRaises(DmError) as cm:
            resume.Lock(path).acquire()
        self.assertEqual(str(cm.exception), "already being downloaded in this folder")
        self.assertEqual(cm.exception.code, 1)
        first.release(remove=True)
        self.assertEqual(self.left(), [])
        again = resume.Lock(path)
        again.acquire()
        again.release(remove=False)
        self.assertEqual(self.left(), ["l"])

    def test_context_manager(self):
        with resume.Lock(self.dir / "l"):
            with self.assertRaises(DmError):
                resume.Lock(self.dir / "l").acquire()

    def test_swapped_lock_file_is_retried(self):
        path = self.dir / "l"
        real = resume.fcntl.flock
        calls = []

        def swapping(fd, op):
            if not calls:  # the file is replaced between our open and our check
                calls.append(1)
                os.unlink(path)
                path.touch()
            return real(fd, op)

        lock = resume.Lock(path)
        with mock.patch.object(resume.fcntl, "flock", swapping):
            lock.acquire()
        self.assertEqual(os.fstat(lock.fd).st_ino, os.stat(path).st_ino)
        lock.release(remove=True)

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
            with self.subTest(code=code), mock.patch.object(resume.fcntl, "flock", side_effect=OSError(code, "x")):
                lock = resume.Lock(self.dir / "l")
                lock.acquire()
                lock.release(remove=True)

    def test_unwritable_folder_is_a_write_error(self):
        with self.assertRaises(DmError) as cm:
            resume.Lock(self.dir / "gone" / "l").acquire()
        self.assertEqual(str(cm.exception), f"cannot write to {self.dir / 'gone'}: No such file or directory")


class Discard(Base):
    def test_removes_files_and_trees_and_ignores_missing(self):
        (self.dir / "f").write_text("x")
        (self.dir / "d" / "e").mkdir(parents=True)
        (self.dir / "d" / "e" / "g").write_text("x")
        resume.discard(self.dir / "f", self.dir / "d", self.dir / "missing")
        self.assertEqual(self.left(), [])

    def test_a_failed_delete_is_not_an_error(self):
        (self.dir / "f").write_text("x")
        with mock.patch.object(resume.os, "unlink", side_effect=OSError(errno.EACCES, "Permission denied")):
            resume.discard(self.dir / "f")
        self.assertEqual(self.left(), ["f"])

    def test_never_follows_symlinks(self):
        outside = self.dir / "outside"
        outside.mkdir()
        (outside / "keep").write_text("x")
        os.symlink(outside, self.dir / "link")
        os.symlink(outside / "keep", self.dir / "flink")
        resume.discard(self.dir / "link", self.dir / "flink")
        self.assertEqual(self.left(), ["outside"])
        self.assertEqual(self.left(outside), ["keep"])


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
        return fetch.download(url or self.url(), t, progress or (lambda p, m: None), cancelled or threading.Event(), **kw)

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
                self.get(url, cancelled=cancelled, throttle=fmt.Throttle(0),
                         progress=lambda p, m: cancelled.set() if p >= percent else None, **kw)

    def hidden(self, suffix):
        return [f for f in self.left() if f.endswith(suffix)]

    def kept(self):
        return len(self.hidden(".dm-part")), len(self.hidden(".dm-resume"))

    def ranges(self):
        return [e["range"] for e in dmserver.LOG if e["path"].startswith("/range/")]

    def assertBody(self, path, version=0, size=SIZE):
        self.assertEqual(hashlib.sha256(Path(path).read_bytes()).hexdigest(),
                         hashlib.sha256(dmserver.content(size, version)).hexdigest())


class ResumeHttp(ResumeCase):
    # AC1, AC2, AC12
    def test_dropped_connection_keeps_partial_and_next_run_resumes(self):
        err = self.interrupt()
        self.assertIn("connection closed early", str(err))
        self.assertEqual(self.kept(), (1, 1))
        self.assertEqual((self.dir / self.hidden(".dm-part")[0]).stat().st_size, CUT)
        self.assertEqual(self.hidden(".dm-lock"), [])
        path, size = self.get()
        self.assertEqual(size, SIZE)
        self.assertBody(path)
        self.assertEqual(self.ranges(), [None, f"bytes={START}-"])
        self.assertEqual(self.notes, ["resuming from 140.0 kB of 400.0 kB"])
        self.assertEqual(self.left(), ["f.bin"])

    def test_progress_starts_at_the_resumed_size_and_never_goes_back(self):
        self.interrupt()
        seen = []
        with mock.patch.object(fetch, "CHUNK", 8192):
            self.get(progress=lambda p, m: seen.append(p), throttle=fmt.Throttle(0))
        self.assertEqual(seen[0], START * 100 // SIZE)
        self.assertEqual(seen, sorted(seen))
        self.assertEqual(seen[-1], 100)

    # NFR1: two interruptions, then the digest of the source
    def test_two_interruptions_still_give_the_right_bytes(self):
        self.cancel_at(25)
        self.cancel_at(60)
        self.assertEqual(self.kept(), (1, 1))
        path, _ = self.get()
        self.assertBody(path)
        self.assertEqual(self.left(), ["f.bin"])
        self.assertEqual(len(self.notes), 2)
        self.assertTrue(all(n.startswith("resuming from ") for n in self.notes))
        self.assertEqual(len([r for r in self.ranges() if r]), 2)

    def test_cancel_keeps_partial_and_record_and_releases_the_lock(self):
        self.cancel_at(30)
        self.assertEqual(self.kept(), (1, 1))
        self.assertEqual(self.hidden(".dm-lock"), [])

    def test_cancel_without_validator_deletes_the_partial(self):
        self.mode(etag="none")
        self.cancel_at(30)
        self.assertEqual(self.left(), [])

    def test_abort_wakes_a_blocked_read_and_keeps_the_partial(self):
        handle = fetch.Handle()
        self.mode(slow=20)
        result = []

        def work():
            try:
                self.get(handle=handle)
            except BaseException as e:  # noqa: BLE001
                result.append(e)

        th = threading.Thread(target=work, daemon=True)
        th.start()
        for _ in range(100):
            if self.hidden(".dm-resume") and self.hidden(".dm-part"):
                break
            threading.Event().wait(0.05)
        self.assertTrue(handle.abort())
        th.join(5)
        self.assertFalse(th.is_alive())
        self.assertIsInstance(result[0], dmcore.Cancelled)
        self.assertEqual(self.kept(), (1, 1))

    def test_503_on_the_resume_request_keeps_the_partial(self):
        self.interrupt()
        self.mode(fail_resume=503)
        with self.assertRaises(DmError) as cm:
            self.get()
        self.assertIn("HTTP 503", str(cm.exception))
        self.assertTrue(cm.exception.keep)
        self.assertEqual(self.kept(), (1, 1))
        self.mode()
        self.assertBody(self.get()[0])

    def test_404_on_a_fresh_run_leaves_nothing(self):
        with self.assertRaises(DmError) as cm:
            self.get(f"{self.base}/status/404")
        self.assertFalse(cm.exception.keep)
        self.assertEqual(self.left(), [])

    def test_status_classification(self):
        for code, keep in ((400, False), (403, False), (404, False), (408, True), (429, True), (500, True),
                           (503, True)):
            with self.subTest(code=code):
                with self.assertRaises(DmError) as cm:
                    self.get(f"{self.base}/status/{code}")
                self.assertEqual(cm.exception.keep, keep)

    def test_connection_errors_are_retryable(self):
        err = ConnectionRefusedError(errno.ECONNREFUSED, "refused")
        with mock.patch("urllib.request.OpenerDirector.open", side_effect=err):
            with self.assertRaises(DmError) as cm:
                self.get()
        self.assertTrue(cm.exception.keep)

    def test_invalid_url_and_redirect_loops_are_not_retryable(self):
        for url in ("ftp://h/x", "http://[::1/x", f"{self.base}/redirect/20"):
            with self.subTest(url=url), self.assertRaises(DmError) as cm:
                self.get(url)
            self.assertFalse(cm.exception.keep)

    def test_disk_full_while_writing_is_retryable_other_write_errors_are_not(self):
        for code, keep in ((errno.ENOSPC, True), (errno.EDQUOT, True), (errno.EIO, False), (errno.EACCES, False)):
            with self.subTest(code=code):
                real = os.write
                calls = []

                def failing(fd, data, code=code, real=real, calls=calls):
                    if len(calls) >= 2:  # the record and the first chunk get through
                        raise OSError(code, os.strerror(code))
                    calls.append(1)
                    return real(fd, data)

                with mock.patch.object(fetch.os, "write", failing), mock.patch.object(fetch, "CHUNK", 4096):
                    with self.assertRaises(DmError) as cm:
                        self.get()
                self.assertEqual(cm.exception.keep, keep)
                self.assertEqual(self.kept(), (1, 1) if keep else (0, 0))
                resume.discard(*[self.dir / f for f in self.left()])

    # AC3
    def test_changed_server_file_starts_over_with_the_new_content(self):
        self.interrupt()
        part = self.dir / self.hidden(".dm-part")[0]
        self.mode(version=1)
        path, _ = self.get()
        self.assertBody(path, version=1)
        self.assertEqual(self.notes, ["cannot resume: starting over"])
        self.assertEqual(self.ranges(), [None, f"bytes={START}-"])  # the If-Range made the server answer 200
        self.assertFalse(part.exists())
        self.assertEqual(self.left(), ["f.bin"])

    def test_206_from_a_changed_file_is_not_trusted(self):
        """A server that ignores If-Range but sends the new validator in its 206: restart without a range."""
        self.interrupt()
        self.mode(version=1)
        real = fetch._open

        def ignoring_if_range(url, handle, headers=None):
            return real(url, handle, {k: v for k, v in (headers or {}).items() if k != "If-Range"})

        with mock.patch.object(fetch, "_open", ignoring_if_range):
            path, _ = self.get()
        self.assertBody(path, version=1)
        self.assertEqual(self.left(), ["f.bin"])

    def test_206_without_validators_from_a_changed_file_is_not_trusted(self):
        """S1: the server ignores If-Range and sends neither ETag nor Last-Modified: restart, never splice."""
        self.interrupt()
        self.mode(version=1, etag="none")
        real = fetch._open

        def ignoring_if_range(url, handle, headers=None):
            return real(url, handle, {k: v for k, v in (headers or {}).items() if k != "If-Range"})

        with mock.patch.object(fetch, "_open", ignoring_if_range):
            path, _ = self.get()
        self.assertBody(path, version=1)
        self.assertEqual(self.ranges(), [None, f"bytes={START}-", None])
        self.assertEqual(self.left(), ["f.bin"])

    def test_weak_etag_with_last_modified_resumes_on_the_date(self):
        """S2: only the validator the record holds is compared; the 206's weak ETag is ignored."""
        self.interrupt(etag="weak", lm=1)
        path, _ = self.get()
        self.assertBody(path)
        self.assertEqual(self.ranges(), [None, f"bytes={START}-"])
        self.assertEqual(self.notes, ["resuming from 140.0 kB of 400.0 kB"])

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
                with self.lie_on_range(header, value) if header != "Content-Encoding" else self.add_header(header, value):
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

    def test_a_resumed_body_longer_than_expected_is_an_error_and_is_discarded(self):
        """N2: a chunked 206 with extra bytes."""
        self.interrupt()
        self.mode(extra=100)
        with self.assertRaises(DmError) as cm:
            self.get()
        self.assertFalse(cm.exception.keep)
        self.assertEqual(self.left(), [])

    def test_partial_smaller_than_the_tail_is_fetched_again_from_the_start(self):
        """N1: below fetch.TAIL the partial is not worth resuming: a plain GET, no record needed."""
        self.interrupt()
        with mock.patch.object(fetch, "TAIL", CUT + 1):
            path, _ = self.get()
        self.assertBody(path)
        self.assertEqual(self.ranges(), [None, None])
        self.assertEqual(self.left(), ["f.bin"])

    def test_resume_truncates_the_torn_tail_before_the_range(self):
        """N1: whatever the last TAIL bytes hold (an OS crash may leave zeros), they are fetched again."""
        self.interrupt()
        part = self.dir / self.hidden(".dm-part")[0]
        data = bytearray(part.read_bytes())
        data[-TAIL:] = b"\0" * TAIL
        part.write_bytes(bytes(data))
        path, _ = self.get()
        self.assertBody(path)
        self.assertEqual(self.ranges()[-1], f"bytes={START}-")

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
            except BaseException as e:  # noqa: BLE001
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
    def test_server_ignoring_range_starts_over(self):
        self.interrupt()
        self.mode(ignore_range=1)
        path, _ = self.get()
        self.assertBody(path)
        self.assertEqual(self.notes, ["cannot resume: starting over"])
        self.assertEqual(self.left(), ["f.bin"])

    def test_weak_etag_alone_leaves_no_record_so_nothing_is_kept(self):
        self.mode(etag="weak", drop_at=CUT)
        with self.assertRaises(DmError):
            self.get()
        self.assertEqual(self.left(), [])

    def test_weak_etag_in_a_record_is_not_used(self):
        self.interrupt()
        record = self.dir / self.hidden(".dm-resume")[0]
        rec = json.loads(record.read_text())
        rec["etag"] = 'W/"x"'
        record.write_text(json.dumps(rec))
        path, _ = self.get()
        self.assertBody(path)
        self.assertEqual(self.notes, ["cannot resume: starting over"])
        self.assertEqual(self.ranges(), [None, None])

    def test_last_modified_alone_is_a_validator(self):
        self.interrupt(etag="none", lm=1)
        path, _ = self.get()
        self.assertBody(path)
        self.assertEqual(self.ranges()[-1], f"bytes={START}-")
        self.assertEqual(dmserver.LOG[-1]["if_range"], "Tue, 14 Nov 2023 22:13:20 GMT")

    def test_range_request_carries_if_range_with_the_etag(self):
        self.interrupt()
        self.get()
        self.assertEqual(dmserver.LOG[-1]["if_range"], f'"v0-{SIZE}"')

    def test_wrong_content_range_restarts_once_without_range(self):
        self.interrupt()
        real = fetch._open
        seen = []

        def lying(url, handle, headers=None):
            resp = real(url, handle, headers)
            if headers:
                seen.append(1)
                resp.headers.replace_header("Content-Range", f"bytes 1-{SIZE - 1}/{SIZE}")
            return resp

        with mock.patch.object(fetch, "_open", lying):
            path, _ = self.get()
        self.assertBody(path)
        self.assertEqual(len(seen), 1)
        self.assertEqual(self.ranges()[-1], None)
        self.assertEqual(self.left(), ["f.bin"])

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

    def test_a_complete_download_that_cannot_be_placed_keeps_the_partial_and_the_record(self):
        with mock.patch.object(fetch, "_place", side_effect=DmError("no free name for 'f.bin'")):
            with self.assertRaises(DmError) as cm:
                self.get()
        self.assertEqual(str(cm.exception), "no free name for 'f.bin'")
        self.assertEqual(self.kept(), (1, 1))
        path, _ = self.get()
        self.assertBody(path)
        self.assertEqual(self.left(), ["f.bin"])

    def test_complete_partial_is_finished_after_refetching_one_byte(self):
        self.interrupt_complete()
        path, size = self.get()
        self.assertEqual(size, SIZE)
        self.assertBody(path)
        self.assertEqual(self.ranges()[-1], f"bytes={SIZE - TAIL}-")
        self.assertEqual(self.left(), ["f.bin"])

    def test_complete_partial_of_a_changed_file_starts_over(self):
        self.interrupt_complete()
        self.mode(version=1)
        path, _ = self.get()
        self.assertBody(path, version=1)
        self.assertEqual(self.notes, ["cannot resume: starting over"])

    # AC5
    def test_other_url_or_output_name_or_folder_never_uses_the_partial(self):
        self.interrupt()
        before = self.left()
        self.assertBody(self.get(self.url("other.bin"))[0])
        self.assertBody(self.get(name="mine.bin")[0])
        sub = self.dir / "sub"
        sub.mkdir()
        self.assertBody(self.get(folder=sub)[0])
        self.assertEqual(self.notes, [])
        self.assertEqual(sorted(set(self.left()) - {"other.bin", "mine.bin", "sub"}), before)

    # AC6
    def test_names_and_records_hold_no_credentials_query_or_fragment(self):
        host = self.base.split("//")[1]
        self.mode(drop_at=CUT)
        url = f"http://bob:hunter2@{host}/range/f.bin?size={SIZE}&token=SECRETTOKEN#FRAG"
        with self.assertRaises(DmError):
            self.get(url)
        self.assertEqual(self.kept(), (1, 1))
        blob = " ".join(self.left()) + " ".join((self.dir / f).read_text() for f in self.hidden(".dm-resume"))
        for secret in ("bob", "hunter2", "SECRETTOKEN", "FRAG", "token", "127.0.0.1"):
            self.assertNotIn(secret, blob)

    # AC10
    def test_no_resume_discards_the_partial_and_starts_at_zero(self):
        self.interrupt()
        path, _ = self.get(resume=False)
        self.assertBody(path)
        self.assertEqual(self.ranges()[-1], None)
        self.assertEqual(self.notes, [])
        self.assertEqual(self.left(), ["f.bin"])

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
    def test_old_random_named_partials_are_left_alone(self):
        old = self.dir / ".f.bin.0123456789abcdef.dm-part"
        old.write_bytes(b"old data")
        path, _ = self.get()
        self.assertBody(path)
        self.assertEqual(old.read_bytes(), b"old data")
        self.assertEqual(self.notes, [])
        self.assertEqual(self.left(), [".f.bin.0123456789abcdef.dm-part", "f.bin"])

    def test_lock_file_is_gone_after_success_and_failure(self):
        self.get()
        self.assertEqual(self.left(), ["f.bin"])
        with self.assertRaises(DmError):
            self.get(f"{self.base}/status/404")
        self.assertEqual(self.left(), ["f.bin"])


class ResumeRun(ResumeCase):
    def run_dm(self, **inputs):
        calls = []
        code = dmcore.run({"url": self.url(), **inputs}, str(self.dir), lambda m, **p: calls.append((m, p)),
                          threading.Event())
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

    def test_no_resume_input_discards(self):
        self.mode(drop_at=CUT)
        self.run_dm()
        self.mode()
        code, calls = self.run_dm(no_resume=True)
        self.assertEqual(code, 0)
        self.assertEqual([p["text"] for m, p in calls if m == "output"], ["saved f.bin (400.0 kB)\n"])
        self.assertEqual(self.ranges()[-1], None)

    def test_cancel_replies_within_2s_and_keeps_the_partial(self):
        import time

        self.mode(slow=60)
        ev = threading.Event()
        fired = []

        def cancel_when_partial_exists():
            for _ in range(200):
                if self.hidden(".dm-resume"):
                    break
                time.sleep(0.05)
            time.sleep(0.2)
            fired.append(time.monotonic())
            ev.set()

        threading.Thread(target=cancel_when_partial_exists, daemon=True).start()
        with self.assertRaises(dmcore.Cancelled):
            dmcore.run({"url": self.url()}, str(self.dir), lambda m, **p: None, ev)
        self.assertLess(time.monotonic() - fired[0], 2)
        self.assertEqual(self.kept(), (1, 1))


if __name__ == "__main__":
    unittest.main()
