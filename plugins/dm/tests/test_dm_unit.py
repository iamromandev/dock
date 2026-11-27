"""Unit tests for the bundled dm plugin (plugins/dm/dmcore). No container runtime needed."""

import errno
import http.server
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import dmcore
from dmcore import fetch, names, target


class Names(unittest.TestCase):
    def test_long_name_is_cut_to_255_bytes_keeping_extension(self):
        got = names.safe_name("a" * 1000 + ".txt")
        self.assertEqual(len(got.encode()), 255)
        self.assertTrue(got.endswith(".txt"))
        got = names.safe_name("\u00e9" * 400)
        self.assertLessEqual(len(got.encode()), 255)
        self.assertEqual(got.strip("\u00e9"), "")
        self.assertEqual(len(names.safe_name("b" * 1000)), 255)

    def test_name_from_invalid_charset_falls_back(self):
        cd = "attachment; filename*=bogus''r%C3%A9.txt; filename=\"x.txt\""
        self.assertEqual(names.name_from({"Content-Disposition": cd}, "http://h/y"), "x.txt")
        cd = "attachment; filename*=bogus''r%C3%A9.txt"
        self.assertEqual(names.name_from({"Content-Disposition": cd}, "http://h/y"), "r\u00e9.txt")


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

    def test_symlink_pointing_outside_is_rejected(self):
        os.symlink(self.outside, os.path.join(self.cwd, "link"))
        self.assertEqual(self.fails(dir="link"), "link is outside the current folder")
        self.assertEqual(self.fails(output="link/x"), "link/x is outside the current folder")
        self.assertEqual(self.fails(output="link"), "link is outside the current folder")
        open(os.path.join(self.outside, "f"), "w").close()
        os.symlink(os.path.join(self.outside, "f"), os.path.join(self.cwd, "flink"))
        self.assertEqual(self.fails(output="flink"), "flink is outside the current folder")

    def test_output_rejects_bad_names(self):
        for name in ("", "..", ".", "a\x00b", "a\nb", "x" * 256, "\u00e9" * 128):
            with self.subTest(name=name):
                self.assertEqual(self.fails(output=name), f"invalid file name '{name}'")
        self.assertEqual(target.resolve(self.cwd, "x" * 255, None).fixed_name, "x" * 255)


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
        return fetch.download(
            self.base + path, t, progress or (lambda p, m: None), cancelled or threading.Event(), **kw
        )

    def fails(self, path, **kw):
        with self.assertRaises(target.DmError) as cm:
            self.get(path, **kw)
        self.assertEqual(self.left(), [])
        return str(cm.exception)

    def test_no_content_length(self):
        _, size = self.get("/nolen")
        self.assertEqual(size, len(BODY))

    def fails_keep(self, path):
        with self.assertRaises(target.DmError) as cm:
            self.get(path)
        return str(cm.exception)

    def test_disk_full_leaves_nothing(self):
        with mock.patch.object(
            fetch.os, "write", side_effect=OSError(errno.ENOSPC, "No space left on device")
        ):
            with self.assertRaises(target.DmError) as cm:
                self.get("/file/data.bin")
        self.assertIn("No space left on device", str(cm.exception))
        self.assertEqual(self.left(), [])

    def test_dangling_symlink_is_skipped_without_hard_links_too(self):
        os.symlink(os.path.join(self.cwd, "victim"), os.path.join(self.cwd, "data.bin"))
        with mock.patch.object(fetch.os, "link", side_effect=OSError(errno.EPERM, "no links")):
            path, _ = self.get("/file/data.bin")
        self.assertEqual(path.name, "data (1).bin")
        self.assertFalse(os.path.lexists(os.path.join(self.cwd, "victim")))

    def test_failed_replace_removes_the_placeholder(self):
        with (
            mock.patch.object(fetch.os, "link", side_effect=OSError(errno.EPERM, "no links")),
            mock.patch.object(fetch.os, "replace", side_effect=OSError(errno.EIO, "Input/output error")),
        ):
            with self.assertRaises(target.DmError) as cm:
                self.get("/file/data.bin")
        self.assertEqual(str(cm.exception), f"cannot write to {self.cwd}: Input/output error")
        self.assertEqual(self.left(), [])


class Run(ServerCase):
    def run_dm(self, inputs, cancelled=None):
        calls = []
        code = dmcore.run(
            inputs, self.cwd, lambda m, **p: calls.append((m, p)), cancelled or threading.Event()
        )
        return code, calls

    def errors(self, calls):
        return [p["message"] for m, p in calls if m == "log" and p["level"] == "error"]

    def assert_no_secrets(self, calls):
        text = repr(calls)
        for secret in ("SECRET", "user", "pw"):
            self.assertNotIn(secret, text)

    def test_url_with_space_and_query_hides_the_query(self):
        host = self.base.split("//")[1]
        code, calls = self.run_dm({"url": f"http://user:pw@{host}/a b.txt?token=SECRET"})
        self.assertEqual(code, 1)
        self.assertEqual(self.errors(calls), [f"dm: {self.base}/a b.txt: invalid URL"])
        self.assert_no_secrets(calls)

    def test_unexpected_exception_shows_only_its_class(self):
        with mock.patch.object(fetch, "download", side_effect=RuntimeError("http://h/x?token=SECRET")):
            code, calls = self.run_dm({"url": self.base + "/file/x"})
        self.assertEqual((code, self.errors(calls)), (1, ["dm: RuntimeError"]))

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
