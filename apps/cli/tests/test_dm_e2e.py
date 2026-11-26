"""dm end to end: dock, the real container runtime, the dm image and a test HTTP server in a second container."""

import hashlib
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import dockenv

DM = ROOT.parent.parent / "plugins" / "dm"
SERVER_DIR = ROOT.parent.parent / "plugins" / "dm" / "tests" / "fixtures" / "dmserver"
RUN_TIMEOUT = 180


def content(size):
    """The bytes /file/<name>?size=N serves (same generator as the server)."""
    out, i, have = [], 0, 0
    while have < size:
        block = hashlib.sha256(str(i).encode()).digest()
        out.append(block)
        have += len(block)
        i += 1
    return b"".join(out)[:size]


class DmE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rt = dockenv.require_runtime()
        dockenv.prebuild_plugins(DM)
        cls.home = dockenv.make_home("dock-dm-", DM)
        cls.addClassCleanup(shutil.rmtree, cls.home, ignore_errors=True)
        cls.base = Path(tempfile.mkdtemp(prefix="dock-dm-work-")).resolve()
        cls.addClassCleanup(shutil.rmtree, cls.base, ignore_errors=True)
        r = subprocess.run([cls.rt, "run", "-d", "--rm", "-v", f"{SERVER_DIR}:/srv:ro", "--label", "dock-test=dmserver",
                            "python:3.15.2-slim", "python3", "-u", "/srv/server.py", "--max-seconds", "900"], capture_output=True, text=True, timeout=600, check=False)
        if r.returncode != 0:
            raise AssertionError(f"cannot start the test server container: {r.stderr}")
        cid = cls.cid = r.stdout.strip().splitlines()[-1]
        cls.addClassCleanup(dockenv.kill_container, cid, cls.rt)
        end = time.monotonic() + 30
        while True:
            logs = subprocess.run([cls.rt, "logs", cid], capture_output=True, text=True, timeout=60, check=False)
            if "ready" in logs.stdout + logs.stderr:
                break
            if time.monotonic() > end:
                raise AssertionError(f"the test server did not print 'ready' within 30 s: {logs.stdout}{logs.stderr}")
            time.sleep(0.3)
        ip = ""
        for fmt in ("{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", "{{.NetworkSettings.IPAddress}}"):
            ip = subprocess.run([cls.rt, "inspect", "-f", fmt, cid], capture_output=True, text=True,
                                timeout=60, check=False).stdout.strip()
            if ip:
                break
        if not ip:
            raise AssertionError("cannot find the IP of the test server container")
        cls.url = f"http://{ip}:8000"
        probe = Path(tempfile.mkdtemp(prefix="probe-", dir=cls.base))
        try:
            r = cls.dock("dm", f"{cls.url}/file/probe?size=1", cwd=probe, timeout=20)
        except subprocess.TimeoutExpired:
            r = None
        if r is None or r.returncode != 0:
            raise AssertionError("the plugin container cannot reach the test server container on the runtime's "
                                 f"default network ({cls.url}): {r.stderr if r else 'timeout'}")

    @classmethod
    def env(cls):
        return {**os.environ, "DOCK_HOME": str(cls.home), "PYTHONPATH": str(ROOT)}

    @classmethod
    def dock(cls, *args, cwd, timeout=RUN_TIMEOUT):
        return subprocess.run([sys.executable, "-m", "src", *args], capture_output=True, text=True, env=cls.env(),
                              cwd=cwd, timeout=timeout, check=False, **dockenv.SIGINT_DEFAULT)

    def setUp(self):
        self.cwd = Path(tempfile.mkdtemp(prefix="w-", dir=self.base))

    def dm(self, route, *extra, cwd=None, dock_args=()):
        url = route if "://" in route or not route.startswith("/") else self.url + route
        return self.dock(*dock_args, "dm", url, *extra, cwd=cwd or self.cwd)

    def server_requests(self):
        """The paths the test server has seen so far."""
        code = "import urllib.request as u;print(u.urlopen('http://127.0.0.1:8000/_requests').read().decode())"
        r = subprocess.run([self.rt, "exec", self.cid, "python3", "-c", code], capture_output=True, text=True,
                           timeout=60, check=False)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.split()

    def files(self, root=None):
        root = root or self.cwd
        found = sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())
        self.assertFalse([f for f in found if f.endswith(".dm-part")], found)
        return found

    def assertSaved(self, r, name, size=None):
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout.strip(), rf"^saved {re.escape(name)} \(.+\)$")
        self.assertEqual(self.files(), [name])
        if size is not None:
            self.assertEqual((self.cwd / name).read_bytes(), content(size))

    def assertFailed(self, r, code=1, text=None):
        self.assertEqual(r.returncode, code, r.stderr)
        self.assertEqual(r.stdout, "")
        lines = r.stderr.strip().splitlines()
        self.assertEqual(len(lines), 1, r.stderr)
        self.assertTrue(lines[0].startswith("[dev.dock.dm] error: dm: "), lines[0])  # the host adds the prefix
        if text:
            self.assertIn(text, lines[0])

    # AC2
    def test_downloads_a_file_with_matching_bytes(self):
        r = self.dm("/file/blob.bin?size=300000")
        self.assertSaved(r, "blob.bin", 300000)
        self.assertIn("300.0 kB", r.stdout)

    # AC3
    def test_filename_star_wins_in_both_orders(self):
        for v in ("attachment; filename=\"plain.txt\"; filename*=UTF-8''r%C3%A9sum%C3%A9.txt",
                  "attachment; filename*=UTF-8''r%C3%A9sum%C3%A9.txt; filename=\"plain.txt\""):
            with self.subTest(v=v):
                cwd = Path(tempfile.mkdtemp(prefix="w-", dir=self.base))
                r = self.dm(f"/cd?v={quote(v)}&size=10", cwd=cwd)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(self.files(cwd), ["résumé.txt"])

    def test_filename_only(self):
        r = self.dm("/cd?v=attachment%3B%20filename%3D%22report.pdf%22&size=10")
        self.assertSaved(r, "report.pdf", 10)

    def test_name_from_url_path_ignores_query(self):
        r = self.dm("/file/a/b.tar.gz?sig=1&size=10")
        self.assertSaved(r, "b.tar.gz", 10)

    def test_trailing_slash_is_named_download(self):
        r = self.dm("/file/?size=10")
        self.assertSaved(r, "download", 10)

    # AC4
    def test_follows_redirects_and_names_from_final_url(self):
        r = self.dm("/redirect/3")
        self.assertSaved(r, "data.csv")
        self.assertEqual((self.cwd / "data.csv").read_text(), "a,b\n1,2\n")

    def test_too_many_redirects(self):
        r = self.dm("/redirect/11")
        self.assertFailed(r, text="too many redirects")
        self.assertEqual(self.files(), [])

    # AC5
    def test_path_in_content_disposition_is_reduced_to_its_basename(self):
        sandbox = Path(tempfile.mkdtemp(prefix="dock-dm-deep-", dir=self.base))
        deep = sandbox / "a" / "b"  # two levels deep, so "../../evil" would land in sandbox
        deep.mkdir(parents=True)
        r = self.dm("/cd?v=attachment%3B%20filename%3D%22..%2F..%2Fevil%22&size=10", cwd=deep)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((deep / "evil").read_bytes(), content(10))
        found = sorted(str(p.relative_to(sandbox)) for p in sandbox.rglob("*") if p.is_file())
        self.assertEqual(found, ["a/b/evil"])
        self.assertFalse((sandbox / "evil").exists())
        self.assertFalse((self.base / "evil").exists())
        self.assertFalse((self.base.parent / "evil").exists())

    # AC6
    def test_never_overwrites(self):
        (self.cwd / "f.txt").write_text("one")
        (self.cwd / "f (1).txt").write_text("two")
        r = self.dm("/file/f.txt?size=10")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("saved f (2).txt", r.stdout)
        self.assertEqual(self.files(), ["f (1).txt", "f (2).txt", "f.txt"])
        self.assertEqual((self.cwd / "f.txt").read_text(), "one")
        self.assertEqual((self.cwd / "f (1).txt").read_text(), "two")

    # AC7
    def test_http_error_leaves_no_file(self):
        r = self.dm("/status/404")
        self.assertFailed(r, text="HTTP 404 Not Found")
        self.assertEqual(self.files(), [])

    def test_truncated_body_leaves_no_file(self):
        r = self.dm("/truncate/t.bin?size=100000")
        self.assertFailed(r)
        self.assertEqual(self.files(), [])

    def test_body_without_length_is_saved(self):
        r = self.dm("/nolength/n.bin?size=5000")
        self.assertSaved(r, "n.bin", 5000)

    # AC8: the host draws progress only on a TTY; here we can only assert that a known-length download completes
    def test_slow_download_with_progress_completes(self):
        r = self.dm("/slow/s.bin?size=200000&seconds=2")
        self.assertSaved(r, "s.bin", 200000)

    # AC9
    def test_ctrl_c_cancels_and_cleans_up(self):
        p = subprocess.Popen([sys.executable, "-m", "src", "dm", f"{self.url}/slow/x?size=2000000&seconds=60"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=self.env(),
                             cwd=self.cwd, **dockenv.SIGINT_DEFAULT)
        self.addCleanup(lambda: (p.poll() is None and p.kill(), p.communicate()))
        end = time.monotonic() + 30  # a warm start of the container takes a moment
        while not list(self.cwd.glob(".*.dm-part")):
            self.assertIsNone(p.poll(), "download ended before Ctrl-C")
            self.assertLess(time.monotonic(), end, "no .dm-part file within 30 s")
            time.sleep(0.1)
        p.send_signal(signal.SIGINT)
        try:
            out, err = p.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            self.fail("dock did not exit within 15 s of Ctrl-C")
        self.assertEqual(p.returncode, 130, err)
        self.assertEqual(list(self.cwd.iterdir()), [])  # no final file and no .dm-part
        self.assertTrue(dockenv.wait_clients_gone(self.home), "a plugin container is still running")

    # AC10
    def test_output_into_existing_subfolder(self):
        (self.cwd / "sub").mkdir()
        r = self.dm("/file/x?size=10", "-o", "sub/name.bin")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("saved sub/name.bin", r.stdout)
        self.assertEqual(self.files(), ["sub/name.bin"])

    def test_output_outside_the_folder_fails(self):
        name = f"dm-out-{uuid.uuid4().hex}"
        for out, outside in ((f"../{name}", self.cwd.parent / name), (f"/tmp/{name}", Path("/tmp") / name)):
            with self.subTest(out=out):
                self.addCleanup(lambda p=outside: p.unlink(missing_ok=True))
                r = self.dm("/file/x?size=10", "-o", out)
                self.assertFailed(r, text="outside the current folder")
                self.assertEqual(self.files(), [])
                self.assertFalse(outside.exists())

    def test_output_in_missing_folder_fails(self):
        r = self.dm("/file/x?size=10", "-o", "missing/x")
        self.assertFailed(r, text="does not exist")
        self.assertEqual(self.files(), [])

    # AC11
    def test_dir_saves_inside(self):
        (self.cwd / "downloads").mkdir()
        r = self.dm("/file/d.bin?size=10", "-d", "downloads")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.files(), ["downloads/d.bin"])

    def test_dir_outside_the_folder_fails(self):
        name = f"up-{uuid.uuid4().hex}"
        r = self.dm("/file/d.bin?size=10", "-d", f"../{name}")
        self.assertFailed(r, text="outside the current folder")
        self.assertEqual(self.files(), [])
        self.assertFalse((self.cwd.parent / name).exists())

    def test_output_and_dir_together_is_a_usage_error(self):
        r = self.dm("/file/d.bin?size=10", "-o", "a", "-d", "b")
        self.assertFailed(r, code=2, text="use -o or -d, not both")

    # AC12
    def test_unsupported_and_invalid_urls_fail_without_a_request(self):
        r = self.dm("ftp://x/y")
        self.assertFailed(r, text="unsupported URL scheme 'ftp'")
        before = self.server_requests()
        r = self.dm("not-a-url")
        self.assertFailed(r, text="invalid URL")
        self.assertEqual(self.files(), [])
        self.assertEqual(self.server_requests(), before)  # nothing reached the server

    def test_redirect_to_a_file_url_is_refused(self):
        r = self.dm("/to-file")
        self.assertFailed(r, text="unsupported URL scheme 'file'")
        self.assertEqual(self.files(), [])

    # AC13
    def test_big_download_stays_in_bounded_memory(self):
        r = self.dm("/big", dock_args=("-v",))
        self.assertEqual(r.returncode, 0, r.stderr)
        m = re.search(r"peak_rss_kb=(\d+)", r.stderr)
        self.assertIsNotNone(m, r.stderr)
        self.assertLess(int(m.group(1)), 150000)
        # only the size is checked: /big repeats one block, so the bytes prove little
        self.assertEqual((self.cwd / "big").stat().st_size, 256 * 1024 * 1024)

    # AC14
    def test_unresolvable_host_is_one_error_line(self):
        r = self.dm("http://does-not-exist.invalid/")
        self.assertFailed(r)
        self.assertEqual(self.files(), [])


class DmCatalog(unittest.TestCase):
    """AC1: the manifest and discovery; no container is started."""

    def test_plugin_info(self):
        r = subprocess.run([sys.executable, "-m", "src", "plugin", "info", str(DM)], capture_output=True, text=True,
                           env={**os.environ, "PYTHONPATH": str(ROOT)}, cwd=ROOT, timeout=60, check=False)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("dev.dock.dm", r.stdout)
        self.assertIn("network, fs:write:cwd", r.stdout)
        self.assertRegex(r.stdout, r"commands:\s+dm")
        self.assertIn("download", r.stdout)

    def test_search_finds_dm_in_the_catalog(self):
        home = Path(tempfile.mkdtemp(prefix="dock-dm-cat-"))
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        (home / "config.toml").write_text(f'[catalog]\npaths = ["{ROOT.parent.parent / "plugins"}"]\n')
        r = subprocess.run([sys.executable, "-m", "src", "search", "download"], capture_output=True, text=True,
                           env={**os.environ, "DOCK_HOME": str(home), "PYTHONPATH": str(ROOT)}, cwd=ROOT,
                           timeout=60, check=False)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout, r"(?m)\bdm\b")


if __name__ == "__main__":
    unittest.main()
