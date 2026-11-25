"""dm torrent end to end: dock, the real container runtime, the dm image, and tracker and seeder containers.

The fixtures are the ones the in-process tests use (tests/fixtures/torrent), mounted read-only into
python:3.15.2-slim containers together with dmcore. They are reached by container IP on the runtime's default
network, like the dmserver of test_dm_e2e.
"""

import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import dockenv  # noqa: E402
import torrentkit  # noqa: E402

DM = ROOT / "plugin" / "dm"
FIXTURES = ROOT / "tests" / "fixtures" / "torrent"
ARIA2_DIR = ROOT / "tests" / "fixtures" / "aria2"
ARIA2_IMAGE = "dock-test/aria2:1"
RUN_TIMEOUT = 180
PL = 16384


def container_ip(rt, cid):
    for fmt in ("{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", "{{.NetworkSettings.IPAddress}}"):
        ip = subprocess.run([rt, "inspect", "-f", fmt, cid], capture_output=True, text=True, timeout=60,
                            check=False).stdout.strip()
        if ip:
            return ip
    raise AssertionError("cannot find the IP of a test container")


def start_container(rt, image, args, mounts=(), ready="ready", wait=30, env=None):
    """Start a detached container; wait for `ready` in its log. Returns (cid, ip). The caller cleans up."""
    cmd = [rt, "run", "-d", "--rm", "--label", "dock-test=dmtorrent"]
    for host, inner, mode in mounts:
        cmd += ["-v", f"{host}:{inner}:{mode}"]
    for k, v in (env or {}).items():
        cmd += ["-e", f"{k}={v}"]
    r = subprocess.run([*cmd, image, *args], capture_output=True, text=True, timeout=600, check=False)
    if r.returncode != 0:
        raise AssertionError(f"cannot start the {image} container: {r.stderr}")
    cid = r.stdout.strip().splitlines()[-1]
    end = time.monotonic() + wait
    while True:
        logs = subprocess.run([rt, "logs", cid], capture_output=True, text=True, timeout=60, check=False)
        if ready in logs.stdout + logs.stderr:
            break
        if time.monotonic() > end:
            dockenv.kill_container(cid, rt)
            raise AssertionError(
                f"no '{ready}' from the {image} container within {wait} s: {logs.stdout}{logs.stderr}")
        time.sleep(0.3)
    return cid, container_ip(rt, cid)


PY_ENV = {"PYTHONPATH": "/dm", "PYTHONDONTWRITEBYTECODE": "1"}
PY_MOUNTS = ((FIXTURES, "/fx", "ro"), (DM, "/dm", "ro"))


class E2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rt = dockenv.require_runtime()
        dockenv.prebuild_plugins(DM)
        cls.home = dockenv.make_home("dock-dmt-", DM)
        cls.addClassCleanup(shutil.rmtree, cls.home, ignore_errors=True)
        cls.base = Path(tempfile.mkdtemp(prefix="dock-dmt-work-")).resolve()
        cls.addClassCleanup(shutil.rmtree, cls.base, ignore_errors=True)
        cid, cls.tracker_ip = start_container(cls.rt, "python:3.15.2-slim", ["python3", "-u", "/fx/tracker.py"],
                                              PY_MOUNTS, env=PY_ENV)
        cls.addClassCleanup(dockenv.kill_container, cid, cls.rt)
        cls.announce = f"http://{cls.tracker_ip}:8000/announce"
        cls.seed_no = 0

    @classmethod
    def env(cls):
        return {**os.environ, "DOCK_HOME": str(cls.home), "PYTHONPATH": str(ROOT)}

    @classmethod
    def dock(cls, *args, cwd, timeout=RUN_TIMEOUT):
        args = list(args)
        # hermetic: the DHT tests are in test_dm_dht_e2e. The command is the first argument that is not a flag.
        command = next((i for i, a in enumerate(args) if not a.startswith("-")), None)
        if command is not None and args[command] == "dm" and not getattr(cls, "dht", False):
            args.insert(command + 1, "--no-dht")
        return subprocess.run([sys.executable, "-m", "cli", *args], capture_output=True, text=True,
                              env=cls.env(), cwd=cwd, timeout=timeout, check=False,
                              **dockenv.SIGINT_DEFAULT)

    def setUp(self):
        self.cwd = Path(tempfile.mkdtemp(prefix="w-", dir=self.base))

    def torrent(self, files, name="t", piece_length=PL, query="", **kw):
        return torrentkit.make_torrent(files, piece_length, announce=self.announce + query, name=name, **kw)

    def seed(self, t, *flags):
        """Start a seeder container for t; returns its container id."""
        E2E.seed_no += 1
        root = self.base / f"seed-{E2E.seed_no}"
        (root / "data").mkdir(parents=True)
        t.write_torrent(root / "x.torrent")
        torrentkit.write_files(root / "data", t)
        cid, _ = start_container(self.rt, "python:3.15.2-slim",
                                 ["python3", "-u", "/fx/peer.py", "seeder", "--torrent", "/seed/x.torrent",
                                  "--data", "/seed/data", *flags],
                                 [*PY_MOUNTS, (root, "/seed", "ro")], env=PY_ENV)
        self.addCleanup(dockenv.kill_container, cid, self.rt)
        return cid

    def dm(self, t, *extra, cwd=None, dock_args=()):
        cwd = cwd or self.cwd
        t.write_torrent(cwd / "data.torrent")
        return self.dock(*dock_args, "dm", "data.torrent", *extra, cwd=cwd)

    def found(self, root=None):
        root = root or self.cwd
        found = sorted(str(p.relative_to(root)) for p in root.rglob("*"))
        part = [f for f in found if f.endswith(".dm-part") or "/.dm-part" in f or ".dm-part/" in f]
        self.assertFalse(part, found)
        return [f for f in found if f != "data.torrent"]

    def assertFailed(self, r, text, code=1):
        self.assertEqual(r.returncode, code, r.stderr)
        self.assertEqual(r.stdout, "")
        lines = r.stderr.strip().splitlines()
        self.assertEqual(len(lines), 1, r.stderr)
        self.assertTrue(lines[0].startswith("[dev.dock.dm] error: dm: "), lines[0])
        self.assertIn(text, lines[0])

    # AC1, AC4
    def test_single_file_download_has_matching_bytes(self):
        t = self.torrent([("a.bin", torrentkit.content(1_000_000))], name="a.bin", piece_length=65536)
        self.seed(t)
        r = self.dm(t)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "saved a.bin (1.0 MB, 1 file)")
        self.assertEqual(self.found(), ["a.bin"])
        self.assertEqual((self.cwd / "a.bin").read_bytes(), t.blob)

    def test_multi_file_download_has_matching_bytes(self):
        files = [("one.txt", torrentkit.content(70000, "1")), ("sub/two.bin", torrentkit.content(40000, "2")),
                 ("sub/empty", b"")]
        t = self.torrent(files, name="pack", piece_length=32768)
        self.seed(t)
        r = self.dm(t)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "saved pack (110.0 kB, 3 files)")
        self.assertEqual(torrentkit.tree(self.cwd / "pack"), dict(files))
        self.assertEqual(self.found(),
                         ["pack", "pack/one.txt", "pack/sub", "pack/sub/empty", "pack/sub/two.bin"])

    def test_missing_and_outside_torrents_follow_the_rules(self):
        r = self.dock("dm", "missing.torrent", cwd=self.cwd)
        self.assertFailed(r, "invalid URL")
        (self.cwd.parent / "outside.torrent").write_bytes(b"de")
        # The container mounts only the current folder, so a file above it does not exist there: the plugin
        # reports it like any other unknown argument. (The "outside the current folder" message is covered
        # in-process, where the file is visible.)
        r = self.dock("dm", "../outside.torrent", cwd=self.cwd)
        self.assertFailed(r, "invalid URL")

    # AC6
    def test_hostile_paths_are_refused_and_nothing_appears_outside(self):
        sandbox = Path(tempfile.mkdtemp(prefix="sb-", dir=self.base))
        work = sandbox / "work"
        work.mkdir()
        cases = {"traversal": [("../x", b"evil")], "deeper": [("a/../../x", b"evil")],
                 "dot": [("./x", b"evil")], "conflict": [("a", b"1"), ("a/b", b"2")],
                 "duplicate": [("a", b"1"), ("a", b"2")]}
        texts = {"conflict": "conflicting paths", "duplicate": "conflicting paths"}
        for label, files in cases.items():
            with self.subTest(label):
                t = self.torrent(files, name="evil", multi=True)
                r = self.dm(t, cwd=work)
                self.assertFailed(r, f"data.torrent: invalid torrent: {texts.get(label, 'unsafe path')}")
                self.assertEqual(sorted(p.name for p in work.iterdir()), ["data.torrent"])
                self.assertEqual([p.name for p in sandbox.iterdir()], ["work"])

    # AC7
    def test_existing_folder_gets_a_numbered_sibling_and_stays_untouched(self):
        files = [("f", torrentkit.content(30000, "f")), ("empty", b"")]
        t = self.torrent(files, name="dataset", piece_length=16384)
        self.seed(t)
        (self.cwd / "dataset").mkdir()
        (self.cwd / "dataset" / "mine").write_text("keep")
        r = self.dm(t)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("saved dataset (1) (30.0 kB, 2 files)", r.stdout)
        self.assertEqual(torrentkit.tree(self.cwd / "dataset"), {"mine": b"keep"})
        self.assertEqual(torrentkit.tree(self.cwd / "dataset (1)"), dict(files))
        self.assertTrue((self.cwd / "dataset (1)" / "empty").is_file())

    # AC8
    def test_output_dir_and_both(self):
        t = self.torrent([("a.bin", torrentkit.content(40000))], name="a.bin")
        self.seed(t)
        r = self.dm(t, "-o", "renamed.bin")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.found(), ["renamed.bin"])
        (self.cwd / "sub").mkdir()
        r = self.dm(t, "-d", "sub")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("saved sub/a.bin", r.stdout)
        r = self.dm(t, "-o", "a", "-d", "b")
        self.assertFailed(r, "use -o or -d, not both", code=2)

    # AC10
    def test_ctrl_c_cancels_and_keeps_the_partial(self):
        t = self.torrent([("slow.bin", torrentkit.content(256 * PL))], name="slow.bin")
        self.seed(t, "--delay", "0.1")
        t.write_torrent(self.cwd / "data.torrent")
        p = subprocess.Popen([sys.executable, "-m", "cli", "dm", "--no-dht", "data.torrent"], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, env=self.env(), cwd=self.cwd,
                             **dockenv.SIGINT_DEFAULT)
        self.addCleanup(lambda: (p.poll() is None and p.kill(), p.communicate()))
        end = time.monotonic() + 30
        while not list(self.cwd.glob(".*.dm-part")):
            self.assertIsNone(p.poll(), "download ended before Ctrl-C")
            self.assertLess(time.monotonic(), end, "no .dm-part within 30 s")
            time.sleep(0.1)
        p.send_signal(signal.SIGINT)
        try:
            _, err = p.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            self.fail("dock did not exit within 15 s of Ctrl-C")
        self.assertEqual(p.returncode, 130, err)
        left = sorted(x.name for x in self.cwd.iterdir())
        self.assertEqual(len(left), 2, left)  # the torrent and the kept partial: no lock file
        self.assertEqual(left[1], "data.torrent")
        self.assertRegex(left[0], r"^\.slow\.bin\.[0-9a-f]{24}\.dm-part$")
        self.assertTrue(dockenv.wait_clients_gone(self.home), "a plugin container is still running")

    # AC11
    def test_passkey_and_peer_addresses_stay_out_of_the_output(self):
        t = self.torrent([("a.bin", torrentkit.content(40000))], name="a.bin", query="?passkey=SECRET")
        self.seed(t)
        r = self.dm(t)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("SECRET", r.stdout + r.stderr)
        self.assertIsNone(re.search(r"\d+\.\d+\.\d+\.\d+", r.stdout), r.stdout)
        # the peers table is the host's own screen and may show them; nothing else may
        outside = re.sub(r"(?m)^peer +client +down +up\n(?:.+\n?)*", "", r.stderr)
        self.assertIsNone(re.search(r"\d+\.\d+\.\d+\.\d+", outside), r.stderr)

    def test_verbose_output_and_the_plugin_log_have_no_peer_address_outside_the_peers_table(self):
        files = [(f"{n}.bin", torrentkit.content(60_000, n)) for n in "ab"]
        t = self.torrent(files, name="pack", piece_length=PL)
        self.seed(t, "--delay", "0.01")
        logs = self.home / "logs"
        before = {p: p.stat().st_size for p in logs.glob("*.log")} if logs.is_dir() else {}
        r = self.dm(t, dock_args=("-v",))
        self.assertEqual(r.returncode, 0, r.stderr)
        address = r"\d+\.\d+\.\d+\.\d+:\d+"
        self.assertIsNotNone(re.search(address, r.stderr), "the peers table still shows the peer")
        outside = re.sub(r"(?m)^peer +client +down +up\n(?:.+\n?)*", "", r.stderr)
        self.assertIn("peer #1 connected", outside, "-v prints the debug lines, which name peers by number")
        self.assertIsNone(re.search(address, outside), outside)
        written = "".join(p.read_bytes()[before.get(p, 0):].decode("utf-8", "replace") for p in logs.glob("*.log"))
        self.assertIn("start", written)  # something was written to it
        self.assertIsNone(re.search(address, written), written)

    # AC15 (plain): the areas print once at the end, and a failure still prints one line
    def test_piped_output_prints_each_area_once_and_the_saved_line_is_unchanged(self):
        files = [(f"{n}.bin", torrentkit.content(120_000, n)) for n in "abc"]
        t = self.torrent(files, name="pack", piece_length=PL)
        self.seed(t, "--delay", "0.01")
        r = self.dm(t)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "saved pack (360.0 kB, 3 files)\n")
        self.assertNotIn("\x1b", r.stderr)
        lines = r.stderr.splitlines()
        self.assertLess(len(lines), 20, r.stderr)
        for n in "abc":
            self.assertEqual(sum(1 for line in lines if re.fullmatch(rf"{n}\.bin  \[#{{20}}\]  100\.0%  .*", line)), 1,
                             r.stderr)
        self.assertEqual(sum(1 for line in lines if line.startswith("total  [####")), 1, r.stderr)
        self.assertEqual(sum(1 for line in lines if re.fullmatch(r"peer +client +down +up", line)), 1, r.stderr)
        self.assertEqual(sum(1 for line in lines if line.startswith("down  ")), 1, r.stderr)

    # AC12
    def test_no_peers_found_fails_at_the_stall_time_and_leaves_nothing(self):
        # no seeder announces it
        t = self.torrent([("a.bin", torrentkit.content(40000, "nobody"))], name="a.bin")
        start = time.monotonic()
        r = self.dm(t, "--stall-seconds", "2")
        self.assertFailed(r, "data.torrent: no peers found")
        self.assertLess(time.monotonic() - start, 60)
        left = sorted(x.name for x in self.cwd.iterdir())  # nothing was verified: no partial, no lock
        self.assertEqual(left, ["data.torrent"], left)

    def aria2_image(self):
        if not dockenv.image_exists(ARIA2_IMAGE):
            r = subprocess.run([self.rt, "build", "-t", ARIA2_IMAGE, str(ARIA2_DIR)], capture_output=True,
                               text=True, timeout=600, check=False)
            self.assertEqual(r.returncode, 0, f"needs network to build the aria2 image: {r.stderr[-500:]}")

    def aria2_seed(self, t):
        """Start aria2 seeding t (its payload written under data/); returns its IP."""
        root = self.base / f"aria2-seed-{E2E.seed_no}"
        E2E.seed_no += 1
        (root / "data").mkdir(parents=True)
        t.write_torrent(root / "x.torrent")
        torrentkit.write_files(root / "data", t)
        cid, ip = start_container(
            self.rt, ARIA2_IMAGE,
            ["--enable-dht=false", "--bt-enable-lpd=false", "--enable-peer-exchange=false",
             "--listen-port=6881",
             "--seed-ratio=0.0", "--bt-seed-unverified", "--console-log-level=info", "-d", "/seed/data",
             "/seed/x.torrent"],
            [(root, "/seed", "rw")], ready="listening on TCP", wait=40)
        self.addCleanup(dockenv.kill_container, cid, self.rt)
        return ip

    def test_aria2_as_a_leecher_is_served_blocks_by_dm_while_dm_downloads(self):
        self.aria2_image()
        t = self.torrent([("a.bin", torrentkit.content(4 * 1024 * 1024))], name="a.bin", piece_length=65536)
        root = self.base / "aria2-leech"
        (root / "data").mkdir(parents=True)
        t.write_torrent(root / "x.torrent")
        cid, aria_ip = start_container(
            self.rt, ARIA2_IMAGE,
            ["--enable-dht=false", "--bt-enable-lpd=false", "--enable-peer-exchange=false",
             "--listen-port=6881",
             "--seed-time=0", "--console-log-level=info", "-d", "/leech/data", "/leech/x.torrent"],
            [(root, "/leech", "rw")], ready="listening on TCP", wait=40)
        self.addCleanup(dockenv.kill_container, cid, self.rt)
        self.seed(t, "--delay", "0.05")  # slow enough for dm to hold pieces aria2 still lacks
        r = self.dm(t, dock_args=("-v",))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.cwd / "a.bin").read_bytes(), t.blob)
        # a log names peers by number, not address; only aria2 asks dm for blocks (the test seeder just serves)
        served = re.findall(r"peer #\d+ served (\d+) blocks", r.stderr)
        self.assertTrue(served and int(served[0]) > 0, r.stderr[-2000:])

    # AC15
    def test_aria2_seeds_a_three_file_torrent_and_dm_downloads_it(self):
        self.aria2_image()
        files = [("one.bin", torrentkit.content(1_000_000, "one")),
                 ("dir/two.bin", torrentkit.content(1_000_000, "two")),
                 ("dir/three.txt", torrentkit.content(97_152, "three"))]  # 2 MiB in all
        t = self.torrent(files, name="interop", piece_length=256 * 1024)
        self.aria2_seed(t)
        r = self.dm(t)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "saved interop (2.1 MB, 3 files)")
        self.assertEqual(torrentkit.tree(self.cwd / "interop"), dict(files))
        self.found()

    def big_seed(self, size, piece_length):
        """Seed a random single-file torrent of this size from a container. Returns (torrent, payload)."""
        E2E.seed_no += 1
        root = self.base / f"seed-{E2E.seed_no}"
        (root / "data").mkdir(parents=True)
        t, payload = torrentkit.make_big_torrent(root / "data", "big.bin", size, piece_length, self.announce)
        t.write_torrent(root / "x.torrent")
        cid, _ = start_container(self.rt, "python:3.15.2-slim",
                                 ["python3", "-u", "/fx/peer.py", "seeder", "--torrent", "/seed/x.torrent",
                                  "--data", "/seed/data"],
                                 [*PY_MOUNTS, (root, "/seed", "ro")], env=PY_ENV)
        self.addCleanup(dockenv.kill_container, cid, self.rt)
        return t, payload

    def run_big(self, size, piece_length):
        t, payload = self.big_seed(size, piece_length)
        t.write_torrent(self.cwd / "data.torrent")
        start = time.monotonic()
        r = self.dock("-v", "dm", "data.torrent", cwd=self.cwd)
        took = time.monotonic() - start
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        self.assertEqual((self.cwd / "big.bin").stat().st_size, size)
        self.assertEqual(torrentkit.file_digest(self.cwd / "big.bin"), torrentkit.file_digest(payload))
        m = re.search(r"peak_rss_kb=(\d+)", r.stderr)
        self.assertIsNotNone(m, r.stderr[-2000:])
        return took, int(m.group(1)) / 1024

    # AC13 (NFR2)
    def test_512_mib_download_stays_in_bounded_memory(self):
        took, rss_mb = self.run_big(512 * 1024 * 1024, 1024 * 1024)
        print(f"\n[perf] 512 MiB: {took:.1f} s, peak_rss {rss_mb:.0f} MB", file=sys.stderr)
        self.assertLess(rss_mb, 200)

    # AC14 (NFR4)
    def test_64_mib_from_one_seeder_takes_under_30_seconds(self):
        took, rss_mb = self.run_big(64 * 1024 * 1024, 256 * 1024)
        print(f"\n[perf] 64 MiB: {took:.1f} s, peak_rss {rss_mb:.0f} MB", file=sys.stderr)
        self.assertLess(took, 30)

    # spike (plan step 6)
    def test_aria2_handshake(self):
        """Our handshake and message reader against a real client, container to container."""
        self.aria2_image()
        t = self.torrent([("seed.bin", torrentkit.content(10000))], name="seed.bin")
        root = self.base / "aria2-seed"
        (root / "data").mkdir(parents=True)
        t.write_torrent(root / "x.torrent")
        torrentkit.write_files(root / "data", t)
        cid, ip = start_container(
            self.rt, ARIA2_IMAGE,
            ["--enable-dht=false", "--bt-enable-lpd=false", "--enable-peer-exchange=false",
             "--listen-port=6881",
             "--seed-ratio=0.0", "--bt-seed-unverified", "--console-log-level=info", "-d", "/seed/data",
             "/seed/x.torrent"],
            [(root, "/seed", "rw")], ready="listening on TCP", wait=40)
        self.addCleanup(dockenv.kill_container, cid, self.rt)
        probe = (
            "import asyncio, sys\n"
            "from dmcore.torrent import wire\n"
            "async def main(ip, ih):\n"
            "    ih = bytes.fromhex(ih)\n"
            "    r, w = await asyncio.wait_for(asyncio.open_connection(ip, 6881), 10)\n"
            "    w.write(wire.handshake(ih, b'-DM0200-' + b'p' * 12))\n"
            "    pid, _ = await asyncio.wait_for(wire.read_handshake(r, ih), 10)\n"
            "    print('peer', pid[:8].decode(), flush=True)\n"
            "    w.write(wire.msg(wire.INTERESTED))\n"
            "    seen = []\n"
            "    for _ in range(4):\n"
            "        m = await asyncio.wait_for(wire.read_message(r, 1), 10)\n"
            "        seen.append(None if m is None else (m[0], m[1].hex()))\n"
            "        kinds = [x[0] for x in seen if x]\n"
            "        if wire.UNCHOKE in kinds and wire.BITFIELD in kinds:\n"
            "            break\n"
            "    print('messages', seen)\n"
            "asyncio.run(main(sys.argv[1], sys.argv[2]))\n")
        name = f"dm-probe-{os.getpid()}"
        self.addCleanup(dockenv.kill_container, name, self.rt)
        r = subprocess.run([self.rt, "run", "--rm", "--name", name, "--label", "dock-test=dmtorrent",
                            "-e", "PYTHONPATH=/dm", "-v", f"{DM}:/dm:ro", "python:3.15.2-slim",
                            "python3", "-c", probe, ip, t.info_hash.hex()], capture_output=True, text=True,
                           timeout=120, check=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertRegex(r.stdout, r"peer A2-")
        self.assertIn("(5, '80')", r.stdout)  # a bitfield with its one piece
        self.assertIn("(1, '')", r.stdout)  # and an unchoke after our interested

    # magnet spike (plan step 1)
    def test_aria2_serves_metadata(self):
        """BEP 10 + BEP 9 against a real client: aria2 seeding serves the info dictionary in 16 KiB blocks."""
        self.aria2_image()
        files = [(f"d/f{i:04d}-{'x' * 40}.bin", torrentkit.content(10, f"m{i}")) for i in range(300)]
        t = self.torrent(files, name="meta", multi=True)
        ip = self.aria2_seed(t)
        probe = (
            "import asyncio, hashlib, sys\n"
            "from dmcore.torrent import bencode, wire\n"
            "async def main(ip, ih):\n"
            "    ih = bytes.fromhex(ih)\n"
            "    r, w = await asyncio.wait_for(asyncio.open_connection(ip, 6881), 10)\n"
            "    w.write(wire.handshake(ih, b'-DM0300-' + b'p' * 12))\n"
            "    pid, reserved = await asyncio.wait_for(wire.read_handshake(r, ih), 10)\n"
            "    assert wire.supports_extensions(reserved), 'no BEP 10 bit'\n"
            "    w.write(wire.extended(0, bencode.encode({'m': {'ut_metadata': 3}})))\n"
            "    their = None\n"
            "    while their is None:\n"
            "        m = await asyncio.wait_for(wire.read_message(r, None), 10)\n"
            "        if m and m[0] == wire.EXTENDED and m[1][0] == 0:\n"
            "            their, _ = bencode.decode_prefix(m[1][1:])\n"
            "    uid, size = their[b'm'][b'ut_metadata'], their[b'metadata_size']\n"
            "    print('ut_metadata', uid, 'size', size, flush=True)\n"
            "    blocks = -(-size // 16384)\n"
            "    for i in range(blocks):\n"
            "        w.write(wire.extended(uid, bencode.encode({'msg_type': 0, 'piece': i})))\n"
            "    got = {}\n"
            "    while len(got) < blocks:\n"
            "        m = await asyncio.wait_for(wire.read_message(r, None), 10)\n"
            "        if m and m[0] == wire.EXTENDED and m[1][0] == 3:\n"
            "            head, end = bencode.decode_prefix(m[1][1:])\n"
            "            assert head[b'msg_type'] == 1, head\n"
            "            got[head[b'piece']] = m[1][1 + end:]\n"
            "    blob = b''.join(got[i] for i in range(blocks))\n"
            "    ok = hashlib.sha1(blob).hexdigest() == ih.hex()\n"
            "    print('blocks', blocks, 'sha1', ok, len(blob) == size)\n"
            "asyncio.run(main(sys.argv[1], sys.argv[2]))\n")
        name = f"dm-probe-{os.getpid()}"
        self.addCleanup(dockenv.kill_container, name, self.rt)
        r = subprocess.run([self.rt, "run", "--rm", "--name", name, "--label", "dock-test=dmtorrent",
                            "-e", "PYTHONPATH=/dm", "-v", f"{DM}:/dm:ro", "python:3.15.2-slim",
                            "python3", "-c", probe, ip, t.info_hash.hex()], capture_output=True, text=True,
                           timeout=120, check=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertRegex(r.stdout, r"blocks [2-9] sha1 True True")  # more than one block, hash and size right


if __name__ == "__main__":
    unittest.main()
