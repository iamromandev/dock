"""The bundled download plugin (Rust), driven through `dock serve` and the CLI. Run `make test`."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    import fcntl
except ImportError:  # windows: the lock test is skipped
    fcntl = None

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from cli.manifest import platform_key  # noqa: E402
from cli.store import Store  # noqa: E402
from serve_client import ServeCase  # noqa: E402

BINARY = ROOT / "plugin" / "download" / "bin" / f"download-{platform_key()}{'.exe' if 'windows' in platform_key() else ''}"
FAKE = ROOT / "tests" / "fixtures" / "fakeruntime" / "docker"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Origin:
    """A local HTTP server for one body. Knobs: `etag`, `honor_range`, `chunk_delay`; `requests` records headers."""

    def __init__(self, body: bytes, etag: str | None = '"v1"', honor_range=True, chunk_delay=0.0, name="data.bin",
                 last_modified: str | None = None):
        self.body, self.etag, self.honor_range, self.chunk_delay = body, etag, honor_range, chunk_delay
        self.last_modified = last_modified
        validator = etag or last_modified
        self.requests: list[dict] = []
        origin = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                origin.requests.append({k.lower(): v for k, v in self.headers.items()})
                start, status = 0, 200
                rng = self.headers.get("Range")
                if rng and origin.honor_range and self.headers.get("If-Range") == validator and validator:
                    start, status = int(rng.split("=")[1].rstrip("-")), 206
                data = origin.body[start:]
                self.send_response(status)
                if origin.etag:
                    self.send_header("ETag", origin.etag)
                if origin.last_modified:
                    self.send_header("Last-Modified", origin.last_modified)
                if status == 206:
                    self.send_header("Content-Range", f"bytes {start}-{len(origin.body) - 1}/{len(origin.body)}")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                try:
                    for i in range(0, len(data), 65536):
                        self.wfile.write(data[i:i + 65536])
                        self.wfile.flush()
                        if origin.chunk_delay:
                            time.sleep(origin.chunk_delay)
                except OSError:
                    pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/{name}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class PluginCase(ServeCase):
    def setUp(self):
        super().setUp()
        self.cwd = tempfile.mkdtemp(prefix="dock-dl-cwd-")
        self.addCleanup(shutil.rmtree, self.cwd, True)

    def origin(self, *a, **kw) -> Origin:
        o = Origin(*a, **kw)
        self.addCleanup(o.close)
        return o

    def run_call(self, c, *args):
        return c.start("run", {"command": "download", "args": list(args), "cwd": self.cwd})

    def download(self, *args):
        c = self.hello_client()
        call = self.run_call(c, *args)
        return c, call, c.wait_reply(call, timeout=30)

    def path(self, name):
        return Path(self.cwd, name)

    def logs(self, c, call):
        return [n["params"]["message"] for n in c.notes("log", call)]


class Basics(PluginCase):
    def test_cancel_reaches_the_running_invoke(self):
        o = self.origin(os.urandom(4_000_000), chunk_delay=0.2)
        c = self.hello_client()
        call = self.run_call(c, o.url, "-o", "x.bin")
        c.wait_until(lambda: c.notes("progress", call), what="the first progress")
        started = time.monotonic()
        c.notify("cancel", {"id": call})
        self.assertEqual(c.wait_reply(call, timeout=10)["error"]["code"], -32800)
        self.assertLess(time.monotonic() - started, 2)

    def test_a_missing_file_is_a_plugin_error(self):
        c, call, reply = self.download("file:///nonexistent/nope.bin", "-o", "x.bin")
        self.assertEqual(reply["error"]["code"], -32000)

    def test_an_http_error_status_is_a_plugin_error(self):
        o = self.origin(b"")
        o.httpd.RequestHandlerClass.do_GET = lambda h: (h.send_response(404), h.send_header("Content-Length", "0"),
                                                          h.end_headers())
        c, call, reply = self.download(o.url, "-o", "x.bin")
        self.assertIn("404", reply["error"]["message"])
        self.assertFalse(self.path("x.bin").exists())


class ProgressAndPaths(PluginCase):
    def test_progress_carries_bytes_speed_and_name_and_the_result_lists_the_path(self):
        body = os.urandom(4_000_000)
        o = self.origin(body, chunk_delay=0.02, name="movie.bin")
        c, call, reply = self.download(o.url, "-o", "movie.bin")
        self.assertEqual(reply["result"], {"exit_code": 0, "paths": [str(self.path("movie.bin"))]})
        self.assertEqual(sha(self.path("movie.bin").read_bytes()), sha(body))
        notes = [n["params"] for n in c.notes("progress", call)]
        self.assertGreaterEqual(len(notes), 2)
        for p in notes:
            self.assertEqual(p["total"], len(body))
            self.assertIsInstance(p["done"], int)
            self.assertLessEqual(p["done"], len(body))
            self.assertEqual(p["name"], "movie.bin")
            self.assertIsInstance(p["speed"], (int, float))
            self.assertAlmostEqual(p["percent"], p["done"] * 100 / len(body), places=3)
        self.assertGreater(max(p["speed"] for p in notes), 0)
        self.assertEqual([p["done"] for p in notes], sorted(p["done"] for p in notes))

    def test_progress_is_throttled_to_about_ten_a_second(self):
        o = self.origin(os.urandom(6_000_000), chunk_delay=0.005)
        started = time.monotonic()
        c, call, reply = self.download(o.url, "-o", "t.bin")
        elapsed = time.monotonic() - started
        notes = c.notes("progress", call)
        self.assertEqual(reply["result"]["exit_code"], 0)
        self.assertGreaterEqual(len(notes), 2)
        self.assertLessEqual(len(notes), elapsed / 0.1 + 3)  # first + final + one per 100 ms

    def test_unknown_size_gives_null_total_and_percent(self):
        o = self.origin(os.urandom(300_000), chunk_delay=0.05)
        o.httpd.RequestHandlerClass.do_GET = _chunked(o)
        c, call, reply = self.download(o.url, "-o", "u.bin")
        self.assertEqual(reply["result"]["exit_code"], 0)
        notes = [n["params"] for n in c.notes("progress", call)]
        self.assertTrue(notes)
        self.assertTrue(all(p["total"] is None and p["percent"] is None for p in notes))

    def test_cli_stdout_is_unchanged(self):
        src = self.path("src.bin")
        src.write_bytes(os.urandom(12345))
        env = {**os.environ, "DOCK_HOME": str(self.home), "PYTHONPATH": str(ROOT)}
        r = subprocess.run([sys.executable, "-m", "cli", "download", src.as_uri(), "-o", "copy.bin"],
                           capture_output=True, text=True, env=env, cwd=self.cwd, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, f"saved {os.path.realpath(self.path('copy.bin'))} (12345 bytes)\n")


def _chunked(o):
    """No Content-Length (HTTP/1.0: the body ends when the connection closes), so the size is unknown."""
    def handler(h):
        h.send_response(200)
        h.end_headers()
        for i in range(0, len(o.body), 65536):
            h.wfile.write(o.body[i:i + 65536])
            h.wfile.flush()
            time.sleep(o.chunk_delay)
    return handler


class DirMode(PluginCase):
    def test_an_existing_file_leads_to_a_numbered_name(self):
        body = os.urandom(5000)
        o = self.origin(body, name="big.iso")
        out = self.path("out")
        out.mkdir()
        (out / "big.iso").write_bytes(b"old")
        _, _, reply = self.download(o.url, "--dir", "out")
        self.assertEqual(reply["result"]["paths"], [str(out / "big (1).iso")])
        self.assertEqual((out / "big (1).iso").read_bytes(), body)
        self.assertEqual((out / "big.iso").read_bytes(), b"old")
        _, _, reply = self.download(o.url, "--dir", "out")
        self.assertEqual(reply["result"]["paths"], [str(out / "big (2).iso")])

    def test_dir_is_made_absolute_against_cwd_and_created(self):
        o = self.origin(b"abc", name="a.txt")
        _, _, reply = self.download(o.url, "--dir", "new/sub")
        self.assertEqual(reply["result"]["paths"], [str(self.path("new/sub/a.txt"))])

    def test_output_gives_the_name_inside_the_dir_and_a_separator_is_refused(self):
        o = self.origin(b"abc")
        _, _, reply = self.download(o.url, "--dir", "out", "-o", "named.bin")
        self.assertEqual(reply["result"]["paths"], [str(self.path("out/named.bin"))])
        _, _, reply = self.download(o.url, "--dir", "out", "-o", "sub/named.bin")
        self.assertEqual(reply["error"]["code"], -32602)

    def test_the_name_falls_back_to_download_out(self):
        o = self.origin(b"abc", name="")
        _, _, reply = self.download(o.url, "--dir", "out")
        self.assertEqual(reply["result"]["paths"], [str(self.path("out/download.out"))])

    def test_output_without_dir_still_overwrites(self):
        o = self.origin(b"new")
        self.path("x.bin").write_bytes(b"old old old")
        _, _, reply = self.download(o.url, "-o", "x.bin")
        self.assertEqual(reply["result"]["paths"], [str(self.path("x.bin"))])
        self.assertEqual(self.path("x.bin").read_bytes(), b"new")
        self.assertFalse(self.path("x.bin (1)").exists())

    def test_the_cli_uses_dir_mode_too(self):
        src = self.path("src.bin")
        src.write_bytes(b"1234")
        (self.path("d")).mkdir()
        self.path("d/src.bin").write_bytes(b"old")
        env = {**os.environ, "DOCK_HOME": str(self.home), "PYTHONPATH": str(ROOT)}
        r = subprocess.run([sys.executable, "-m", "cli", "download", src.as_uri(), "--dir", "d"],
                           capture_output=True, text=True, env=env, cwd=self.cwd, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, f"saved {os.path.realpath(self.path('d/src (1).bin'))} (4 bytes)\n")

    def test_under_the_fake_container_runtime_the_mount_is_the_cwd_and_the_file_lands_in_dir(self):
        home = Path(tempfile.mkdtemp(prefix="dock-dl-iso-"))
        self.addCleanup(shutil.rmtree, home, True)
        m = Store(home).install(ROOT / "plugin" / "download")
        shutil.copy(BINARY, m.root / "download")  # the fake runtime maps /plugin/download to the context
        log = home / "calls"
        (home / "config.toml").write_text(f'[isolation]\nruntime = "{FAKE}"\nenabled = true\n')
        src = self.path("src.bin")
        src.write_bytes(os.urandom(2000))
        env = {**os.environ, "DOCK_HOME": str(home), "PYTHONPATH": str(ROOT), "FAKE_RUNTIME_LOG": str(log)}
        r = subprocess.run([sys.executable, "-m", "cli", "download", src.as_uri(), "--dir", "out"],
                           capture_output=True, text=True, env=env, cwd=self.cwd, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.path("out/src.bin").read_bytes(), src.read_bytes())
        runs = [json.loads(l) for l in log.read_text().splitlines() if json.loads(l)[0] == "run"]
        self.assertTrue(runs)
        real = os.path.realpath(self.cwd)
        self.assertIn(f"{real}:{real}", runs[0])  # the mount equals the cwd


class Resume(PluginCase):
    BODY = os.urandom(3_000_000)

    def seed(self, name, size, url, etag='"v1"', last_modified=None):
        Path(self.cwd, name + ".part").write_bytes(self.BODY[:size])
        Path(self.cwd, name + ".part.json").write_text(json.dumps(
            {"url": url, "etag": etag, "last_modified": last_modified, "total": len(self.BODY)}))

    def test_a_cancel_keeps_the_part_file_and_sidecar_and_removes_the_lock(self):
        o = self.origin(self.BODY, chunk_delay=0.05)
        c = self.hello_client()
        call = self.run_call(c, o.url, "-o", "r.bin")
        c.wait_until(lambda: len(c.notes("progress", call)) >= 2, what="some progress")
        c.notify("cancel", {"id": call})
        self.assertEqual(c.wait_reply(call, timeout=10)["error"]["code"], -32800)
        part = self.path("r.bin.part")
        self.assertTrue(part.exists() and part.stat().st_size > 0)
        side = json.loads(self.path("r.bin.part.json").read_text())
        self.assertEqual((side["url"], side["etag"], side["total"]), (o.url, '"v1"', len(self.BODY)))
        self.assertFalse(self.path("r.bin.part.lock").exists())
        self.assertFalse(self.path("r.bin").exists())
        # a rerun resumes: it asks only for the rest, and the result is the whole file
        o.chunk_delay = 0
        have = part.stat().st_size
        _, _, reply = self.download(o.url, "-o", "r.bin")
        self.assertEqual(reply["result"]["exit_code"], 0)
        self.assertEqual(o.requests[-1]["range"], f"bytes={have}-")
        self.assertEqual(o.requests[-1]["if-range"], '"v1"')
        self.assertEqual(sha(self.path("r.bin").read_bytes()), sha(self.BODY))
        for leftover in ("r.bin.part", "r.bin.part.json", "r.bin.part.lock"):
            self.assertFalse(self.path(leftover).exists(), leftover)

    def test_a_dir_mode_rerun_resumes_the_same_name_instead_of_numbering(self):
        o = self.origin(self.BODY, name="f.bin")
        (self.path("out")).mkdir()
        Path(self.cwd, "out", "f.bin.part").write_bytes(self.BODY[:1000])
        Path(self.cwd, "out", "f.bin.part.json").write_text(json.dumps(
            {"url": o.url, "etag": '"v1"', "last_modified": None, "total": len(self.BODY)}))
        _, _, reply = self.download(o.url, "--dir", "out")
        self.assertEqual(reply["result"]["paths"], [str(self.path("out/f.bin"))])
        self.assertEqual(o.requests[-1]["range"], "bytes=1000-")
        self.assertEqual(sha(self.path("out/f.bin").read_bytes()), sha(self.BODY))

    def test_a_server_that_ignores_range_restarts_with_a_notice(self):
        o = self.origin(self.BODY, honor_range=False)
        self.seed("r.bin", 1000, o.url)
        c, call, reply = self.download(o.url, "-o", "r.bin")
        self.assertEqual(reply["result"]["exit_code"], 0)
        self.assertIn("restarting: server ignored range", self.logs(c, call))
        self.assertEqual(sha(self.path("r.bin").read_bytes()), sha(self.BODY))

    def test_a_changed_etag_restarts_with_a_notice(self):
        o = self.origin(self.BODY, etag='"v2"')
        self.seed("r.bin", 1000, o.url, etag='"v1"')
        c, call, reply = self.download(o.url, "-o", "r.bin")
        self.assertEqual(reply["result"]["exit_code"], 0)
        self.assertEqual(o.requests[-1]["if-range"], '"v1"')
        self.assertIn("restarting: file changed on server", self.logs(c, call))
        self.assertEqual(sha(self.path("r.bin").read_bytes()), sha(self.BODY))

    def test_a_server_without_a_validator_restarts_from_scratch(self):
        o = self.origin(self.BODY, etag=None)
        self.seed("r.bin", 1000, o.url, etag=None)
        c, call, reply = self.download(o.url, "-o", "r.bin")
        self.assertEqual(reply["result"]["exit_code"], 0)
        self.assertNotIn("range", o.requests[-1])
        self.assertIn("restarting: no validator", self.logs(c, call))
        self.assertEqual(sha(self.path("r.bin").read_bytes()), sha(self.BODY))

    def test_a_part_file_for_another_url_is_not_resumed(self):
        o = self.origin(self.BODY)
        self.seed("r.bin", 1000, "http://elsewhere.invalid/x")
        _, _, reply = self.download(o.url, "-o", "r.bin")
        self.assertEqual(reply["result"]["exit_code"], 0)
        self.assertNotIn("range", o.requests[-1])
        self.assertEqual(sha(self.path("r.bin").read_bytes()), sha(self.BODY))

    @unittest.skipIf(fcntl is None, "flock is posix-only")
    def test_a_busy_lock_is_already_downloading(self):
        o = self.origin(self.BODY)
        lock = open(self.path("r.bin.part.lock"), "w")
        self.addCleanup(lock.close)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _, _, reply = self.download(o.url, "-o", "r.bin")
        self.assertEqual(reply["error"]["code"], -32000)
        self.assertEqual(reply["error"]["message"], "already downloading r.bin")
        self.assertEqual(o.requests, [])
        self.assertTrue(self.path("r.bin.part.lock").exists())  # not ours: left alone

    def test_a_206_with_the_wrong_content_range_start_restarts_with_a_notice(self):
        o = self.origin(self.BODY)

        def handler(h):
            o.requests.append({k.lower(): v for k, v in h.headers.items()})
            ranged = "Range" in h.headers
            h.send_response(206 if ranged else 200)
            h.send_header("ETag", '"v1"')
            if ranged:
                h.send_header("Content-Range", f"bytes 0-{len(self.BODY) - 1}/{len(self.BODY)}")
            h.send_header("Content-Length", str(len(self.BODY)))
            h.end_headers()
            try:
                h.wfile.write(self.BODY)
            except OSError:  # the plugin drops the misplaced 206
                pass
        o.httpd.RequestHandlerClass.do_GET = handler
        self.seed("r.bin", 1000, o.url)
        c, call, reply = self.download(o.url, "-o", "r.bin")
        self.assertEqual(reply["result"]["exit_code"], 0)
        self.assertEqual([("range" in r) for r in o.requests], [True, False])
        self.assertTrue(any(m.startswith("restarting:") for m in self.logs(c, call)), self.logs(c, call))
        self.assertEqual(sha(self.path("r.bin").read_bytes()), sha(self.BODY))

    def test_a_416_restarts_from_scratch(self):
        o = self.origin(self.BODY)

        def handler(h):
            o.requests.append({k.lower(): v for k, v in h.headers.items()})
            if "Range" in h.headers:
                h.send_response(416)
                h.send_header("Content-Length", "0")
                h.end_headers()
                return
            h.send_response(200)
            h.send_header("ETag", '"v1"')
            h.send_header("Content-Length", str(len(self.BODY)))
            h.end_headers()
            h.wfile.write(self.BODY)
        o.httpd.RequestHandlerClass.do_GET = handler
        self.seed("r.bin", 1000, o.url)
        c, call, reply = self.download(o.url, "-o", "r.bin")
        self.assertEqual(reply["result"]["exit_code"], 0)
        self.assertEqual([("range" in r) for r in o.requests], [True, False])
        self.assertIn("restarting: file changed on server", self.logs(c, call))
        self.assertEqual(sha(self.path("r.bin").read_bytes()), sha(self.BODY))

    def test_a_truncated_body_is_an_error_that_keeps_the_part(self):
        o = self.origin(self.BODY)
        half = len(self.BODY) // 2

        def handler(h):
            h.send_response(200)
            h.send_header("ETag", '"v1"')
            h.send_header("Content-Length", str(len(self.BODY)))
            h.end_headers()
            h.wfile.write(self.BODY[:half])
            h.wfile.flush()
        o.httpd.RequestHandlerClass.do_GET = handler
        _, _, reply = self.download(o.url, "-o", "r.bin")
        self.assertEqual(reply["error"]["code"], -32000)
        self.assertIn(f"connection closed after {half} of {len(self.BODY)} bytes", reply["error"]["message"])
        self.assertEqual(self.path("r.bin.part").stat().st_size, half)
        self.assertTrue(self.path("r.bin.part.json").exists())
        self.assertFalse(self.path("r.bin").exists())
        self.assertFalse(self.path("r.bin.part.lock").exists())

    def test_a_last_modified_only_server_resumes_with_it_as_the_validator(self):
        lm = "Wed, 01 Jan 2025 00:00:00 GMT"
        o = self.origin(self.BODY, etag=None, last_modified=lm)
        self.seed("r.bin", 1000, o.url, etag=None, last_modified=lm)
        _, _, reply = self.download(o.url, "-o", "r.bin")
        self.assertEqual(reply["result"]["exit_code"], 0)
        self.assertEqual(o.requests[-1]["range"], "bytes=1000-")
        self.assertEqual(o.requests[-1]["if-range"], lm)
        self.assertEqual(sha(self.path("r.bin").read_bytes()), sha(self.BODY))


@unittest.skipIf(os.name == "nt", "symlinks need privileges on windows")
class Symlinks(PluginCase):
    def setUp(self):
        super().setUp()
        self.victim = self.path("victim")

    def test_a_dangling_symlink_as_the_target_is_taken_in_dir_mode(self):
        o = self.origin(b"abc", name="a.iso")
        self.path("out").mkdir()
        os.symlink(self.victim, self.path("out/a.iso"))
        _, _, reply = self.download(o.url, "--dir", "out")
        self.assertEqual(reply["result"]["paths"], [str(self.path("out/a (1).iso"))])
        self.assertFalse(self.victim.exists())

    def test_a_symlinked_part_is_never_written_through(self):
        o = self.origin(b"abc")
        os.symlink(self.victim, self.path("x.bin.part"))
        _, _, reply = self.download(o.url, "-o", "x.bin")
        self.assertEqual(reply["error"]["code"], -32000)
        self.assertFalse(self.victim.exists())

    def test_a_symlinked_part_with_a_matching_sidecar_is_not_appended_to(self):
        o = self.origin(b"abc" * 1000, name="a.iso")
        self.victim.write_bytes(b"precious")
        self.path("out").mkdir()
        os.symlink(self.victim, self.path("out/a.iso.part"))
        self.path("out/a.iso.part.json").write_text(json.dumps({"url": o.url, "etag": '"v1"', "total": 3000}))
        _, _, reply = self.download(o.url, "--dir", "out")
        self.assertEqual(reply["result"]["paths"], [str(self.path("out/a (1).iso"))])
        self.assertEqual(self.victim.read_bytes(), b"precious")

    def test_a_symlinked_lock_file_is_refused(self):
        o = self.origin(b"abc")
        os.symlink(self.victim, self.path("x.bin.part.lock"))
        _, _, reply = self.download(o.url, "-o", "x.bin")
        self.assertEqual(reply["error"]["code"], -32000)
        self.assertFalse(self.victim.exists())


class Safety(PluginCase):
    def test_a_windows_unsafe_output_name_in_dir_mode_is_invalid_params(self):
        o = self.origin(b"abc")
        for bad in ("C:foo", "a:b", "NUL", "com1.txt", "x.", "x ", "a\\b"):
            _, _, reply = self.download(o.url, "--dir", "out", "-o", bad)
            self.assertEqual(reply["error"]["code"], -32602, bad)
        self.assertEqual(list(self.path("out").iterdir()) if self.path("out").exists() else [], [])

    def test_a_windows_unsafe_url_name_falls_back_to_download_out(self):
        for name in ("a%3Ab", "NUL.txt", "C%3Afoo"):
            o = self.origin(b"abc", name=name)
            _, _, reply = self.download(o.url, "--dir", "out")
            self.assertEqual(reply["result"]["paths"], [str(self.path("out/download.out"))], name)
            self.path("out/download.out").unlink()

    def test_url_names_match_the_old_go_plugin(self):
        for name, want in (("dir/", "dir"), ("a%2Fb", "b")):
            o = self.origin(b"abc", name=name)
            _, _, reply = self.download(o.url, "--dir", "out")
            self.assertEqual(reply["result"]["paths"], [str(self.path(f"out/{want}"))], name)

    def test_a_traversal_in_the_url_cannot_leave_the_dir(self):
        for name in ("..%2f", "%2e%2e", "..%2f..%2fescape", "%2e%2e%2f"):
            o = self.origin(b"abc", name=name)
            _, _, reply = self.download(o.url, "--dir", "out/in")
            paths = reply["result"]["paths"]
            self.assertEqual(len(paths), 1, name)
            self.assertEqual(Path(paths[0]).parent, self.path("out/in"), name)
        self.assertEqual(sorted(p.name for p in self.path("out").iterdir()), ["in"])


class Concurrency(PluginCase):
    def test_two_downloads_of_different_urls_to_one_name_get_numbered_names(self):
        b1, b2 = os.urandom(2_000_000), os.urandom(2_000_000)
        o1 = self.origin(b1, chunk_delay=0.05, name="a.iso")
        o2 = self.origin(b2, chunk_delay=0.05, name="a.iso")
        c = self.hello_client()
        c1 = self.run_call(c, o1.url, "--dir", "out")
        c2 = self.run_call(c, o2.url, "--dir", "out")
        r1, r2 = c.wait_reply(c1, timeout=60), c.wait_reply(c2, timeout=60)
        got = {r["result"]["paths"][0] for r in (r1, r2)}
        self.assertEqual(got, {str(self.path("out/a.iso")), str(self.path("out/a (1).iso"))})
        by_url = {o1.url: b1, o2.url: b2}
        for r, o in ((r1, o1), (r2, o2)):
            self.assertEqual(Path(r["result"]["paths"][0]).read_bytes(), by_url[o.url])


class Big(PluginCase):
    def test_a_50_mb_download_reports_progress_and_arrives_intact(self):
        body = os.urandom(50 * 1024 * 1024)
        o = self.origin(body, name="big.bin")
        c, call, reply = self.download(o.url, "-o", "big.bin")
        self.assertEqual(reply["result"], {"exit_code": 0, "paths": [str(self.path("big.bin"))]})
        self.assertEqual(self.path("big.bin").stat().st_size, len(body))
        self.assertEqual(sha(self.path("big.bin").read_bytes()), sha(body))
        notes = [n["params"] for n in c.notes("progress", call)]
        self.assertTrue(notes)
        self.assertEqual(notes[-1]["done"], len(body))
        self.assertEqual({p["total"] for p in notes}, {len(body)})
        self.assertGreater(max(p["speed"] for p in notes), 0)


if __name__ == "__main__":
    unittest.main()
