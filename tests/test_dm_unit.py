"""Unit tests for the bundled dm plugin (plugin/dm/dmcore). No container runtime needed."""

import errno
import http.server
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "plugin" / "dm"))

import dmcore  # noqa: E402
from dmcore import fetch, fmt, names, target  # noqa: E402


class Fmt(unittest.TestCase):
    def test_human_size(self):
        cases = {0: "0 B", 512: "512 B", 999: "999 B", 1500: "1.5 kB", 10_000_000: "10.0 MB",
                 1_200_000_000: "1.2 GB", 999_999: "1.0 MB"}
        for n, text in cases.items():
            with self.subTest(n=n):
                self.assertEqual(fmt.human_size(n), text)

    def test_display_url_drops_credentials_query_fragment(self):
        self.assertEqual(fmt.display_url("https://u:p@example.com:8443/a/b.txt?sig=1#frag"),
                         "https://example.com:8443/a/b.txt")
        self.assertEqual(fmt.display_url("http://example.com"), "http://example.com")
        self.assertEqual(fmt.display_url("http://[::1]:80/x"), "http://[::1]:80/x")

    def test_throttle_allows_ten_per_second_and_final(self):
        now = [0.0]
        th = fmt.Throttle(0.1, clock=lambda: now[0])
        fired = []
        for i in range(1000):  # a call every simulated millisecond for one second
            now[0] = i / 1000
            if th.ready():
                fired.append(now[0])
        self.assertLessEqual(len(fired), 10)
        self.assertGreaterEqual(len(fired), 9)
        self.assertTrue(th.ready(final=True))

    def test_throttle_first_call_is_ready(self):
        th = fmt.Throttle(0.1, clock=lambda: 5.0)
        self.assertTrue(th.ready())
        self.assertFalse(th.ready())


class Names(unittest.TestCase):
    def test_safe_name(self):
        cases = {
            "../../x": "x", "/etc/passwd": "passwd", "..": "download", ".": "download", "": "download",
            "a\\b\\c.txt": "c.txt", "a\0b": "ab", "a\x1fb\x7fc": "abc", ".bashrc": "bashrc",
            "  spaced.txt  ": "spaced.txt", "dir/": "download", "evil\u202egpj.exe": "evilgpj.exe",
            "r\u00e9sum\u00e9.txt": "r\u00e9sum\u00e9.txt",
        }
        for raw, want in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(names.safe_name(raw), want)

    def test_long_name_is_cut_to_255_bytes_keeping_extension(self):
        got = names.safe_name("a" * 1000 + ".txt")
        self.assertEqual(len(got.encode()), 255)
        self.assertTrue(got.endswith(".txt"))
        got = names.safe_name("\u00e9" * 400)
        self.assertLessEqual(len(got.encode()), 255)
        self.assertEqual(got.strip("\u00e9"), "")
        self.assertEqual(len(names.safe_name("b" * 1000)), 255)

    def test_name_from_prefers_rfc5987_then_filename_then_url(self):
        cd = "attachment; filename*=UTF-8''r%C3%A9sum%C3%A9.txt; filename=\"resume.txt\""
        self.assertEqual(names.name_from({"Content-Disposition": cd}, "http://h/x/y.bin"), "r\u00e9sum\u00e9.txt")
        self.assertEqual(names.name_from({"Content-Disposition": 'attachment; filename="x.bin"'}, "http://h/y"), "x.bin")
        self.assertEqual(names.name_from({}, "http://h/a/b.tar.gz?sig=1#f"), "b.tar.gz")
        self.assertEqual(names.name_from({}, "http://h/a/r%C3%A9.txt"), "r\u00e9.txt")
        self.assertEqual(names.name_from({}, "http://h/a/"), "download")
        self.assertEqual(names.name_from({}, "http://h"), "download")

    def test_name_from_filename_star_beats_filename_in_either_order(self):
        for cd in ["attachment; filename=\"a.txt\"; filename*=UTF-8''b.txt",
                   "attachment; filename*=UTF-8''b.txt; filename=\"a.txt\""]:
            with self.subTest(cd=cd):
                self.assertEqual(names.name_from({"Content-Disposition": cd}, "http://h/y"), "b.txt")

    def test_name_from_filename_star_alone_is_decoded(self):
        cd = "attachment; filename*=iso-8859-1'en'r%E9sum%E9.txt"
        self.assertEqual(names.name_from({"Content-Disposition": cd}, "http://h/y"), "r\u00e9sum\u00e9.txt")

    def test_name_from_invalid_charset_falls_back(self):
        cd = "attachment; filename*=bogus''r%C3%A9.txt; filename=\"x.txt\""
        self.assertEqual(names.name_from({"Content-Disposition": cd}, "http://h/y"), "x.txt")
        cd = "attachment; filename*=bogus''r%C3%A9.txt"
        self.assertEqual(names.name_from({"Content-Disposition": cd}, "http://h/y"), "r\u00e9.txt")

    def test_name_from_sanitizes_header_names(self):
        self.assertEqual(names.name_from({"Content-Disposition": 'attachment; filename="../../etc/x"'}, "http://h/y"), "x")
        self.assertEqual(names.name_from({"Content-Disposition": "garbage;;;"}, "http://h/y.txt"), "y.txt")

    def test_candidates(self):
        got = list(names.candidates("b.tar.gz"))
        self.assertEqual(got[:3], ["b.tar.gz", "b (1).tar.gz", "b (2).tar.gz"])
        self.assertEqual(len(got), 1000)
        self.assertEqual(got[-1], "b (999).tar.gz")
        self.assertEqual(list(names.candidates("a.txt"))[1], "a (1).txt")
        self.assertEqual(list(names.candidates("noext"))[1], "noext (1)")
        self.assertEqual(list(names.candidates(".x"))[1], ".x (1)")

    def test_candidates_stay_within_255_bytes(self):
        base = names.safe_name("a" * 300 + ".txt")
        got = list(names.candidates(base))
        self.assertTrue(all(len(c.encode()) <= 255 for c in got))
        self.assertTrue(got[-1].endswith(" (999).txt"))


class Target(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = os.path.realpath(self.tmp.name)
        self.cwd = os.path.join(base, "cwd")
        self.outside = os.path.join(base, "outside")
        os.mkdir(self.cwd)
        os.mkdir(self.outside)
        os.mkdir(os.path.join(self.cwd, "sub"))

    def fails(self, output=None, dir=None, code=1):
        with self.assertRaises(target.DmError) as cm:
            target.resolve(self.cwd, output, dir)
        self.assertEqual(cm.exception.code, code)
        return str(cm.exception)

    def test_default_is_cwd(self):
        t = target.resolve(self.cwd, None, None)
        self.assertEqual((str(t.folder), t.fixed_name), (self.cwd, None))

    def test_output_relative_and_absolute(self):
        t = target.resolve(self.cwd, "sub/f.bin", None)
        self.assertEqual((str(t.folder), t.fixed_name), (os.path.join(self.cwd, "sub"), "f.bin"))
        t = target.resolve(self.cwd, os.path.join(self.cwd, "g.bin"), None)
        self.assertEqual((str(t.folder), t.fixed_name), (self.cwd, "g.bin"))

    def test_output_naming_existing_folder_is_dir(self):
        t = target.resolve(self.cwd, "sub", None)
        self.assertEqual((str(t.folder), t.fixed_name), (os.path.join(self.cwd, "sub"), None))

    def test_dir(self):
        t = target.resolve(self.cwd, None, "sub")
        self.assertEqual((str(t.folder), t.fixed_name), (os.path.join(self.cwd, "sub"), None))

    def test_output_outside_cwd(self):
        self.assertEqual(self.fails(output="../outside/x"), "../outside/x is outside the current folder")
        self.assertEqual(self.fails(output=os.path.join(self.outside, "x")),
                         f"{os.path.join(self.outside, 'x')} is outside the current folder")

    def test_dir_outside_cwd(self):
        self.assertEqual(self.fails(dir=".."), ".. is outside the current folder")

    def test_symlink_pointing_outside_is_rejected(self):
        os.symlink(self.outside, os.path.join(self.cwd, "link"))
        self.assertEqual(self.fails(dir="link"), "link is outside the current folder")
        self.assertEqual(self.fails(output="link/x"), "link/x is outside the current folder")
        self.assertEqual(self.fails(output="link"), "link is outside the current folder")
        open(os.path.join(self.outside, "f"), "w").close()
        os.symlink(os.path.join(self.outside, "f"), os.path.join(self.cwd, "flink"))
        self.assertEqual(self.fails(output="flink"), "flink is outside the current folder")

    def test_missing_folder(self):
        self.assertEqual(self.fails(dir="nope"), "folder nope does not exist")
        self.assertEqual(self.fails(output="nope/x"), "folder nope does not exist")

    def test_output_keeps_leading_dot_names(self):
        for name in (".env", ".gitignore"):
            with self.subTest(name=name):
                t = target.resolve(self.cwd, name, None)
                self.assertEqual(t.fixed_name, name)

    def test_output_rejects_bad_names(self):
        for name in ("", "..", ".", "a\x00b", "a\nb", "x" * 256, "\u00e9" * 128):
            with self.subTest(name=name):
                self.assertEqual(self.fails(output=name), f"invalid file name '{name}'")
        self.assertEqual(target.resolve(self.cwd, "x" * 255, None).fixed_name, "x" * 255)

    def test_empty_dir_is_invalid_folder(self):
        self.assertEqual(self.fails(dir=""), "invalid folder ''")

    def test_empty_values_count_as_given_for_both_flags(self):
        self.assertEqual(self.fails(output="", dir="sub", code=2), "use -o or -d, not both")

    def test_both_flags_is_usage_error(self):
        self.assertEqual(self.fails(output="a", dir="sub", code=2), "use -o or -d, not both")


BODY = b"0123456789" * 20_000  # 200 kB


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def reply(self, status, headers=(), body=b"", length=True):
        self.send_response(status)
        for k, v in headers:
            self.send_header(k, v)
        if length:
            self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def do_GET(self):
        path = self.path.split("?")[0]
        if path.startswith("/file/"):
            self.reply(200, body=BODY)
        elif path == "/nolen":
            self.reply(200, body=BODY, length=False)
        elif path == "/cd":
            cd = "attachment; filename*=UTF-8''r%C3%A9sum%C3%A9.txt; filename=\"resume.txt\""
            self.reply(200, [("Content-Disposition", cd)], b"cv")
        elif path.startswith("/redir/"):
            n = int(path.rsplit("/", 1)[1])
            if n:
                self.reply(302, [("Location", f"/redir/{n - 1}")])
            else:
                self.reply(200, body=b"end")
        elif path == "/toftp":
            self.reply(302, [("Location", "ftp://h/x?t=SECRET#f")])
        elif path == "/hangup":
            self.close_connection = True
        elif path == "/loop":
            self.reply(302, [("Location", "/loop")])
        elif path == "/tofile":
            self.reply(302, [("Location", "file:///etc/passwd")])
        elif path.startswith("/status/"):
            self.reply(int(path.rsplit("/", 1)[1]))
        elif path == "/trunc":
            self.send_response(200)
            self.send_header("Content-Length", "1000")
            self.end_headers()
            self.wfile.write(b"x" * 100)
            self.wfile.flush()
            self.close_connection = True
        elif path == "/slow":
            self.send_response(200)
            self.send_header("Content-Length", "1000000")
            self.end_headers()
            self.wfile.write(b"x" * 1000)
            self.wfile.flush()
            self.server.stop.wait(5)
            self.close_connection = True
        else:
            self.reply(404)


class ServerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.server.daemon_threads = True
        cls.server.stop = threading.Event()
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.stop.set()
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cwd = os.path.realpath(self.tmp.name)

    def left(self):
        return sorted(os.listdir(self.cwd))


class Fetch(ServerCase):
    def get(self, path, name=None, progress=None, cancelled=None, **kw):
        t = target.Target(Path(self.cwd), name)
        return fetch.download(self.base + path, t, progress or (lambda p, m: None), cancelled or threading.Event(), **kw)

    def fails(self, path, **kw):
        with self.assertRaises(target.DmError) as cm:
            self.get(path, **kw)
        self.assertEqual(self.left(), [])
        return str(cm.exception)

    def test_saves_body_under_name_from_url(self):
        path, size = self.get("/file/data.bin")
        self.assertEqual((path, size), (Path(self.cwd, "data.bin"), len(BODY)))
        self.assertEqual(path.read_bytes(), BODY)
        self.assertEqual(self.left(), ["data.bin"])

    def test_no_content_length(self):
        _, size = self.get("/nolen")
        self.assertEqual(size, len(BODY))

    def test_content_disposition_name(self):
        path, _ = self.get("/cd")
        self.assertEqual(path.name, "r\u00e9sum\u00e9.txt")

    def test_fixed_name_wins(self):
        path, _ = self.get("/cd", name="mine.txt")
        self.assertEqual(path.name, "mine.txt")

    def test_existing_file_is_never_overwritten(self):
        Path(self.cwd, "data.bin").write_text("old")
        path, _ = self.get("/file/data.bin")
        self.assertEqual(path.name, "data (1).bin")
        self.assertEqual(Path(self.cwd, "data.bin").read_text(), "old")

    def test_no_free_name(self):
        for n in names.candidates("data.bin"):
            Path(self.cwd, n).touch()
        self.assertEqual(self.fails_keep("/file/data.bin"), "no free name for 'data.bin'")
        self.assertFalse([f for f in self.left() if f.endswith(".dm-part")])

    def fails_keep(self, path):
        with self.assertRaises(target.DmError) as cm:
            self.get(path)
        return str(cm.exception)

    def test_file_created_during_download_is_not_replaced(self):
        real_link = os.link
        calls = []

        def racing_link(src, dst, *a, **k):
            if not calls:
                calls.append(1)
                Path(dst).write_text("raced")
            return real_link(src, dst, *a, **k)

        with mock.patch.object(fetch.os, "link", racing_link):
            path, _ = self.get("/file/data.bin")
        self.assertEqual(path.name, "data (1).bin")
        self.assertEqual(Path(self.cwd, "data.bin").read_text(), "raced")
        self.assertEqual(self.left(), ["data (1).bin", "data.bin"])

    def test_filesystem_without_hard_links_still_never_overwrites(self):
        Path(self.cwd, "data.bin").write_text("old")
        with mock.patch.object(fetch.os, "link", side_effect=OSError(errno.EPERM, "no links")):
            path, _ = self.get("/file/data.bin")
        self.assertEqual(path.name, "data (1).bin")
        self.assertEqual(Path(self.cwd, "data.bin").read_text(), "old")
        self.assertEqual(self.left(), ["data (1).bin", "data.bin"])

    def test_disk_full_leaves_nothing(self):
        with mock.patch.object(fetch.os, "write", side_effect=OSError(errno.ENOSPC, "No space left on device")):
            with self.assertRaises(target.DmError) as cm:
                self.get("/file/data.bin")
        self.assertIn("No space left on device", str(cm.exception))
        self.assertEqual(self.left(), [])

    def test_http_error(self):
        self.assertEqual(self.fails("/status/404"), f"{self.base}/status/404: HTTP 404 Not Found")
        self.assertEqual(self.fails("/status/503"), f"{self.base}/status/503: HTTP 503 Service Unavailable")

    def test_error_messages_hide_credentials_query_and_fragment(self):
        host = self.base.split("//")[1]
        t = target.Target(Path(self.cwd), None)
        with self.assertRaises(target.DmError) as cm:
            fetch.download(f"http://bob:hunter2@{host}/status/404?token=abc#frag", t, lambda p, m: None, threading.Event())
        self.assertEqual(str(cm.exception), f"{self.base}/status/404: HTTP 404 Not Found")

    def test_connection_refused(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        t = target.Target(Path(self.cwd), None)
        with self.assertRaises(target.DmError) as cm:
            fetch.download(f"http://127.0.0.1:{port}/x", t, lambda p, m: None, threading.Event())
        self.assertTrue(str(cm.exception).startswith(f"http://127.0.0.1:{port}/x: "), str(cm.exception))
        self.assertEqual(self.left(), [])

    def test_truncated_body(self):
        self.assertEqual(self.fails("/trunc"), f"{self.base}/trunc: connection closed early")

    def test_redirect_chain_within_limit(self):
        path, _ = self.get("/redir/10")
        self.assertEqual(path.name, "0")

    def test_too_many_redirects(self):
        self.assertEqual(self.fails("/redir/11"), f"{self.base}/redir/11: too many redirects")
        self.assertEqual(self.fails("/loop"), f"{self.base}/loop: too many redirects")

    def test_redirect_to_non_http_is_refused(self):
        self.assertEqual(self.fails("/tofile"), "unsupported URL scheme 'file' (only http and https)")

    def test_check_url(self):
        for url in ["ftp://h/x", "file:///etc/passwd", "h/x", "", "http:///x", "javascript:alert(1)"]:
            with self.subTest(url=url):
                with self.assertRaises(target.DmError) as cm:
                    fetch.check_url(url)
                self.assertIn(str(cm.exception), (f"unsupported URL scheme '{urlsplit(url).scheme}' (only http and https)",
                                                  "invalid URL"))
        with self.assertRaises(target.DmError) as cm:
            fetch.check_url("ftp://bob:pw@h/x?t=SECRET#f")
        self.assertEqual(str(cm.exception), "unsupported URL scheme 'ftp' (only http and https)")
        fetch.check_url("https://example.com/x")

    def test_progress_is_throttled_with_final_100(self):
        ticks = [0.0]

        def clock():
            ticks[0] += 0.01  # every chunk is 10 ms of simulated time
            return ticks[0]

        seen = []
        with mock.patch.object(fetch, "CHUNK", 400):
            self.get("/file/data.bin", progress=lambda p, m: seen.append((p, m)), throttle=fmt.Throttle(0.1, clock))
        self.assertEqual(seen[-1], (100, "200.0 kB of 200.0 kB"))
        simulated = ticks[0]
        self.assertGreater(simulated, 4)
        self.assertLessEqual(len(seen), 10 * simulated + 2)
        self.assertGreater(len(seen), 5)
        self.assertTrue(all(isinstance(p, int) and 0 <= p <= 100 for p, _ in seen))

    def test_progress_without_content_length_has_null_percent(self):
        seen = []
        self.get("/nolen", progress=lambda p, m: seen.append((p, m)))
        self.assertEqual(seen[-1], (None, "200.0 kB"))
        self.assertTrue(all(p is None for p, _ in seen))
        self.assertEqual(len(seen), len(set(seen)))

    def test_small_file_sends_one_final_update(self):
        seen = []
        self.get("/cd", progress=lambda p, m: seen.append((p, m)))
        self.assertEqual(seen, [(100, "2 B of 2 B")])

    def test_progress_burst_is_limited_plus_one_final(self):
        ticks = [0.0]
        seen = []

        def clock():
            ticks[0] += 0.001
            return ticks[0]

        with mock.patch.object(fetch, "CHUNK", 400):
            self.get("/file/data.bin", progress=lambda p, m: seen.append((p, m)), throttle=fmt.Throttle(0.1, clock))
        self.assertLessEqual(len(seen), 10 * ticks[0] + 2)
        self.assertEqual(seen[-1][0], 100)
        self.assertEqual([p for p, _ in seen].count(100), 1)

    def test_progress_is_dropped_after_abort(self):
        handle = fetch.Handle()
        seen = []
        handle.emit(lambda p, m: seen.append((p, m)), 5, "a")
        self.assertTrue(handle.abort())
        handle.emit(lambda p, m: seen.append((p, m)), 100, "b")
        self.assertEqual(seen, [(5, "a")])

    def test_dangling_symlink_as_fixed_name_is_never_written_through(self):
        os.symlink(os.path.join(self.cwd, "victim"), os.path.join(self.cwd, "link"))
        path, _ = self.get("/file/x", name="link")
        self.assertEqual(path.name, "link (1)")
        self.assertFalse(os.path.lexists(os.path.join(self.cwd, "victim")))
        self.assertTrue(os.path.islink(os.path.join(self.cwd, "link")))

    def test_dangling_symlink_at_candidate_name_is_skipped(self):
        os.symlink(os.path.join(self.cwd, "victim"), os.path.join(self.cwd, "data.bin"))
        path, _ = self.get("/file/data.bin")
        self.assertEqual(path.name, "data (1).bin")
        self.assertFalse(os.path.lexists(os.path.join(self.cwd, "victim")))

    def test_dangling_symlink_is_skipped_without_hard_links_too(self):
        os.symlink(os.path.join(self.cwd, "victim"), os.path.join(self.cwd, "data.bin"))
        with mock.patch.object(fetch.os, "link", side_effect=OSError(errno.EPERM, "no links")):
            path, _ = self.get("/file/data.bin")
        self.assertEqual(path.name, "data (1).bin")
        self.assertFalse(os.path.lexists(os.path.join(self.cwd, "victim")))

    def test_failed_replace_removes_the_placeholder(self):
        with mock.patch.object(fetch.os, "link", side_effect=OSError(errno.EPERM, "no links")), \
                mock.patch.object(fetch.os, "replace", side_effect=OSError(errno.EIO, "Input/output error")):
            with self.assertRaises(target.DmError) as cm:
                self.get("/file/data.bin")
        self.assertEqual(str(cm.exception), f"cannot write to {self.cwd}: Input/output error")
        self.assertEqual(self.left(), [])

    def test_failed_replace_keeps_a_placeholder_someone_else_replaced(self):
        def replace_and_fail(src, dst):
            os.unlink(dst)
            Path(dst).write_text("theirs")
            raise OSError(errno.EIO, "Input/output error")

        with mock.patch.object(fetch.os, "link", side_effect=OSError(errno.EPERM, "no links")), \
                mock.patch.object(fetch.os, "replace", replace_and_fail):
            with self.assertRaises(target.DmError):
                self.get("/file/data.bin")
        self.assertEqual(Path(self.cwd, "data.bin").read_text(), "theirs")

    def test_place_error_is_wrapped_without_temp_path(self):
        with mock.patch.object(fetch, "_place", side_effect=OSError(errno.ENOSPC, "No space left on device", "/x/.tmp.dm-part")):
            with self.assertRaises(target.DmError) as cm:
                self.get("/file/data.bin")
        self.assertEqual(str(cm.exception), f"cannot write to {self.cwd}: No space left on device")
        self.assertEqual(self.left(), [])

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores permissions")
    def test_unwritable_folder_is_wrapped_without_temp_path(self):
        sub = Path(self.cwd, "ro")
        sub.mkdir()
        sub.chmod(0o500)
        self.addCleanup(sub.chmod, 0o700)
        t = target.Target(sub, None)
        with self.assertRaises(target.DmError) as cm:
            fetch.download(self.base + "/file/data.bin", t, lambda p, m: None, threading.Event())
        self.assertEqual(str(cm.exception), f"cannot write to {sub}: Permission denied")

    def test_missing_folder_is_a_write_error_not_a_cancel(self):
        t = target.Target(Path(self.cwd, "gone"), None)
        with self.assertRaises(target.DmError) as cm:
            fetch.download(self.base + "/file/data.bin", t, lambda p, m: None, threading.Event())
        self.assertEqual(str(cm.exception), f"cannot write to {self.cwd}/gone: No such file or directory")

    def test_cancelled_before_start_creates_nothing(self):
        ev = threading.Event()
        ev.set()
        with self.assertRaises(dmcore.Cancelled):
            self.get("/file/data.bin", cancelled=ev)
        self.assertEqual(self.left(), [])


class Run(ServerCase):
    def run_dm(self, inputs, cancelled=None):
        calls = []
        code = dmcore.run(inputs, self.cwd, lambda m, **p: calls.append((m, p)), cancelled or threading.Event())
        return code, calls

    def test_success_reports_saved_path_and_peak_rss(self):
        code, calls = self.run_dm({"url": self.base + "/file/data.bin"})
        self.assertEqual(code, 0)
        outputs = [p for m, p in calls if m == "output"]
        self.assertEqual(outputs, [{"text": "saved data.bin (200.0 kB)\n"}])
        debug = [p for m, p in calls if m == "log" and p["level"] == "debug"]
        self.assertEqual(len(debug), 1)
        self.assertRegex(debug[0]["message"], r"^peak_rss_kb=\d+$")
        self.assertEqual(calls[-1][0], "log")
        progress = [p for m, p in calls if m == "progress"]
        self.assertEqual(progress[-1]["percent"], 100)

    def test_dir_and_output(self):
        os.mkdir(os.path.join(self.cwd, "sub"))
        _, calls = self.run_dm({"url": self.base + "/file/data.bin", "dir": "sub"})
        self.assertIn(("output", {"text": "saved sub/data.bin (200.0 kB)\n"}), calls)
        _, calls = self.run_dm({"url": self.base + "/file/data.bin", "output": "mine.bin"})
        self.assertIn(("output", {"text": "saved mine.bin (200.0 kB)\n"}), calls)
        code, calls = self.run_dm({"url": self.base + "/file/data.bin", "output": "mine.bin"})
        self.assertIn(("output", {"text": "saved mine (1).bin (200.0 kB)\n"}), calls)

    def errors(self, calls):
        return [p["message"] for m, p in calls if m == "log" and p["level"] == "error"]

    def test_http_error_is_one_error_line_exit_1(self):
        code, calls = self.run_dm({"url": self.base + "/status/404?x=1"})
        self.assertEqual(code, 1)
        self.assertEqual(self.errors(calls), [f"dm: {self.base}/status/404: HTTP 404 Not Found"])
        self.assertEqual(self.left(), [])

    def test_unsupported_url_exit_1(self):
        code, calls = self.run_dm({"url": "ftp://example.com/x"})
        self.assertEqual(code, 1)
        self.assertEqual(self.errors(calls), ["dm: unsupported URL scheme 'ftp' (only http and https)"])

    def assert_no_secrets(self, calls):
        text = repr(calls)
        for secret in ("SECRET", "user", "pw"):
            self.assertNotIn(secret, text)

    def test_redirect_to_other_scheme_hides_query_and_fragment(self):
        _, calls = self.run_dm({"url": self.base + "/toftp?t=SECRET"})
        self.assertEqual(self.errors(calls), ["dm: unsupported URL scheme 'ftp' (only http and https)"])
        self.assert_no_secrets(calls)

    def test_malformed_url_with_credentials_is_invalid_url(self):
        code, calls = self.run_dm({"url": "http://user:pw@[::1/?q=SECRET"})
        self.assertEqual(code, 1)
        self.assertEqual(self.errors(calls), ["dm: invalid URL"])
        self.assert_no_secrets(calls)

    def test_url_with_space_and_query_hides_the_query(self):
        host = self.base.split("//")[1]
        code, calls = self.run_dm({"url": f"http://user:pw@{host}/a b.txt?token=SECRET"})
        self.assertEqual(code, 1)
        self.assertEqual(self.errors(calls), [f"dm: {self.base}/a b.txt: invalid URL"])
        self.assert_no_secrets(calls)

    def test_server_hanging_up_is_reported_plainly(self):
        code, calls = self.run_dm({"url": self.base + "/hangup?token=SECRET"})
        self.assertEqual(code, 1)
        self.assertEqual(self.errors(calls), [f"dm: {self.base}/hangup: connection closed by server"])

    def test_unexpected_exception_shows_only_its_class(self):
        with mock.patch.object(fetch, "download", side_effect=RuntimeError("http://h/x?token=SECRET")):
            code, calls = self.run_dm({"url": self.base + "/file/x"})
        self.assertEqual((code, self.errors(calls)), (1, ["dm: RuntimeError"]))

    def test_both_flags_exit_2(self):
        code, calls = self.run_dm({"url": self.base + "/x", "output": "a", "dir": "."})
        self.assertEqual(code, 2)
        self.assertEqual(self.errors(calls), ["dm: use -o or -d, not both"])

    def test_output_outside_cwd_exit_1(self):
        code, calls = self.run_dm({"url": self.base + "/file/x", "output": "../x"})
        self.assertEqual(code, 1)
        self.assertEqual(self.errors(calls), ["dm: ../x is outside the current folder"])

    def test_cancel_mid_download_raises_within_2s_and_leaves_nothing(self):
        ev = threading.Event()
        fired = []

        def cancel_later():
            time.sleep(0.5)
            fired.append(time.monotonic())
            ev.set()

        threading.Thread(target=cancel_later, daemon=True).start()
        with self.assertRaises(dmcore.Cancelled):
            self.run_dm({"url": self.base + "/slow"}, ev)
        self.assertLess(time.monotonic() - fired[0], 2)
        self.assertEqual(self.left(), [])
        time.sleep(0.3)  # an abandoned worker must not recreate anything
        self.assertEqual(self.left(), [])


if __name__ == "__main__":
    unittest.main()
