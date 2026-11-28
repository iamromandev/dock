"""The extract plugin driven over its stdio protocol (real main.py), plus unit tests of its pure parts."""

import bz2
import errno
import gzip
import io
import json
import lzma
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import tomllib
import tracemalloc
import types
import unittest
import zipfile
from compression import zstd
from pathlib import Path
from unittest import mock

PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN))
EXTRACT = PLUGIN / "main.py"

import main as extract  # the loop only runs under __main__, so the pure helpers can be tested directly

MIB = 1 << 20
MTIME = 1_600_000_000


# ---- archive builders

def zinfo(name, mode=None, ctype=zipfile.ZIP_DEFLATED):
    zi = zipfile.ZipInfo(name, (2020, 1, 2, 3, 4, 6))
    zi.compress_type = ctype
    if mode is not None:
        zi.external_attr = mode << 16
    return zi


def mkzip(path, items, ctype=zipfile.ZIP_DEFLATED):
    """items: (name or ZipInfo, bytes)."""
    with zipfile.ZipFile(path, "w", ctype) as z:
        for name, data in items:
            z.writestr(name, data)
    return path


def ti(name, type=tarfile.REGTYPE, data=b"", mode=0o644, link=""):
    info = tarfile.TarInfo(name)
    info.type, info.mode, info.linkname, info.mtime = type, mode, link, MTIME
    info.size = len(data) if type == tarfile.REGTYPE else 0
    return info, data


def tar_bytes(items):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as t:
        for info, data in items:
            if isinstance(info, str):  # a plain (name, bytes) file
                info, data = ti(info, data=data)
            t.addfile(info, io.BytesIO(data) if info.type == tarfile.REGTYPE else None)
    return buf.getvalue()


COMPRESSORS = {"gz": gzip.compress, "bz2": bz2.compress, "xz": lzma.compress, "zst": zstd.compress}


def mktar(path, items, comp=None):
    data = tar_bytes(items)
    Path(path).write_bytes(COMPRESSORS[comp](data) if comp else data)
    return path


def files_of(n, size=10):
    return [(f"f{i}.txt", bytes([65 + i % 26]) * size) for i in range(n)]


def gz_stream(*chunks, level=1):
    """gzip of the chunks, written one by one (a huge repeated chunk costs little to build)."""
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=level) as g:
        for chunk in chunks:
            g.write(chunk)
    return buf.getvalue()


def header_bomb(n):
    """A .tar.gz whose first member has a PAX header of about n bytes (one huge comment), then a small file."""
    rest = b" comment=" + b"A" * n + b"\n"
    for width in range(1, 12):  # the record starts with its own length
        if len(str(len(rest) + width)) == width:
            break
    record = str(len(rest) + width).encode() + rest
    info = tarfile.TarInfo("PaxHeader")
    info.type, info.size = tarfile.XHDTYPE, len(record)
    pad = bytes(-len(record) % 512)
    body = tar_bytes([("f.txt", b"hi")])
    return gz_stream(info.tobuf(tarfile.USTAR_FORMAT), record[:-n - 1], b"A" * n, b"\n", pad, body)


class Zeros:
    """An endless-looking entry body: `n` zero bytes, read in blocks."""

    def __init__(self, n):
        self.left = n

    def read(self, k=-1):
        k = self.left if k < 0 else min(k, self.left)
        self.left -= k
        return bytes(k)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ---- the plugin over stdio

class Scratch(unittest.TestCase):
    def tmp(self):
        """A new temp folder, removed when the test ends."""
        path = Path(tempfile.mkdtemp(prefix="dock-extract-")).resolve()
        self.addCleanup(shutil.rmtree, path, ignore_errors=True)
        return path


class Base(Scratch):
    def setUp(self):
        self.root = self.tmp()
        self.dir = self.root / "out"  # the cwd
        self.inn = self.root / "in"
        self.outside = self.root / "outside"
        self.work = self.root / "work"
        for d in (self.dir, self.inn, self.outside, self.work):
            d.mkdir()

    def tree(self):
        """Everything under the scratch root (links by target): a before/after snapshot."""
        return sorted(str(p.relative_to(self.root)) + (" -> " + os.readlink(p) if p.is_symlink() else "")
                      for p in self.root.rglob("*"))

    def begin(self, inputs, in_dir="default", work=None, env=None, params=None):
        e = {**os.environ}
        e.pop("DOCK_EXTRACT_MAX_BYTES", None)
        e.pop("DOCK_EXTRACT_MAX_ENTRIES", None)
        e.update(env or {})
        p = subprocess.Popen([sys.executable, str(EXTRACT)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, env=e)
        msg = {"command": "extract", "args": [], "cwd": str(self.dir), "inputs": inputs}
        if in_dir == "default":
            in_dir = str(self.inn)
        if in_dir is not None:
            msg["in_dir"] = in_dir
        if work is not None:
            msg["work"] = str(work)
        msg.update(params or {})
        self.send(p, id=1, method="initialize", params={})
        self.send(p, id=2, method="invoke", params=msg)
        return p

    @staticmethod
    def send(p, **msg):
        p.stdin.write(json.dumps({"jsonrpc": "2.0", **msg}) + "\n")
        p.stdin.flush()

    @staticmethod
    def collect(p, lines=None):
        # Read until the shutdown answer before closing stdin: closing it mid-invoke cancels the invoke.
        lines = [] if lines is None else lines
        for line in p.stdout:
            lines.append(line)
            if line.startswith("{") and json.loads(line).get("id") == 3:
                break
        rest, _ = p.communicate(timeout=60)
        msgs = [json.loads(x) for x in "".join(lines).splitlines() + rest.splitlines() if x.startswith("{")]
        return next(m for m in msgs if m.get("id") == 2), [m for m in msgs if "method" in m]

    def run_plugin(self, inputs, **kw):
        """Returns (the invoke answer, the notifications seen before it)."""
        p = self.begin(inputs, **kw)
        self.send(p, id=3, method="shutdown", params={})
        return self.collect(p)

    def put(self, name, data):
        (self.inn / name).write_bytes(data)
        return name

    @staticmethod
    def said(notes, level="error"):
        return " ".join(n["params"]["message"] for n in notes if n["method"] == "log" and n["params"]["level"] == level)

    def assertRefused(self, answer, notes, before, contains=""):
        self.assertEqual(answer.get("result"), {"exit_code": 1}, answer)
        self.assertIn(contains, self.said(notes))
        self.assertTrue(self.said(notes))
        self.assertEqual(self.tree(), before)  # nothing created anywhere: no destination, no .part


class Manifest(unittest.TestCase):
    def test_the_manifest_matches_the_spec(self):  # R1
        m = tomllib.loads((PLUGIN / "plugin.toml").read_text())
        # Bug caught: a wrong id, a network permission, or an input that does not match the CLI flags.
        self.assertEqual((m["id"], m["version"], m["api_version"]), ("dev.dock.extract", "0.1.0", "2"))
        self.assertEqual(m["capabilities"]["permissions"], ["fs:read:in", "fs:write:cwd"])
        self.assertEqual(m["capabilities"]["env"], ["DOCK_EXTRACT_MAX_BYTES", "DOCK_EXTRACT_MAX_ENTRIES"])
        self.assertEqual(m["run"]["container"], {"cmd": "python3", "args": ["/plugin/main.py"]})
        (cmd,) = m["commands"]
        self.assertEqual(cmd["name"], "extract")
        self.assertEqual({i["name"]: (i["type"], i.get("flag"), i.get("short"), i.get("required", False))
                          for i in cmd["inputs"]},
                         {"file": ("string", None, None, True), "dir": ("string", "--dir", "-d", False),
                          "list": ("bool", "--list", None, False), "only": ("string", "--only", None, False)})
        for word in ("extract", "unzip", "untar", "unpack", "archive", "decompress", "zip", "tar"):
            self.assertIn(word, cmd["keywords"])


# ---- pure parts

class Units(Scratch):
    def test_check_name_normalises_and_refuses_hostile_names(self):  # R7
        # Bug caught: a hostile entry name (parent part, absolute, drive, NUL, over-long) is accepted.
        for raw, want in [("a/b.txt", "a/b.txt"), ("./a//b/", "a/b"), ("a\\b\\c.txt", "a/b/c.txt"),
                          ("./", ""), (".", ""), ("d/./e", "d/e")]:
            with self.subTest(raw):
                self.assertEqual(extract.check_name(raw), want)
        for raw in ["../x", "a/../x", "a\\..\\x", "/etc/passwd", "\\x", "C:/x", "c:\\x", "a\0b", "a" * 4097,
                    "d/" + "a" * 256, ".."]:
            with self.subTest(raw[:20]):
                with self.assertRaises(extract.BadInput):
                    extract.check_name(raw)

    def test_dest_name_strips_suffixes_and_finds_a_free_name(self):  # R4
        # Bug caught: the suffix is kept, a taken name is reused, or a bad -d name is accepted.
        d = self.tmp()
        for archive, want in [("a.tar.gz", "a"), ("B.TGZ", "B"), ("x.TAR.Zst", "x"), ("n.txt.gz", "n.txt"),
                              ("a.b.zip", "a.b"), (".gz", "archive"), ("plain", "plain")]:
            with self.subTest(archive):
                self.assertEqual(extract.dest_name(archive, None, str(d)), want)
        (d / "a").mkdir()
        (d / "a-2").write_text("")
        self.assertEqual(extract.dest_name("a.zip", None, str(d)), "a-3")
        self.assertEqual(extract.dest_name("a.zip", "out", str(d)), "out")
        for wanted in ["a", "x/y", "x\\y", "..", ".", "a\0b", "n" * 256]:
            with self.subTest(wanted[:10]):
                with self.assertRaises(extract.BadInput):
                    extract.dest_name("a.zip", wanted, str(d))

    def test_sniff_goes_by_content_not_by_suffix(self):  # R3
        # Bug caught: the format is guessed from the file name, or a PNG / a compressed non-tar is mistaken.
        d = self.tmp()
        t = tar_bytes(files_of(2))
        cases = {"zip": (mkzip(d / "1.bin", files_of(1)).read_bytes(), ("zip", None)),
                 "tar": (t, ("tar", None)), "tgz": (gzip.compress(t), ("tar", "gz")),
                 "tbz": (bz2.compress(t), ("tar", "bz2")), "txz": (lzma.compress(t), ("tar", "xz")),
                 "tzst": (zstd.compress(t), ("tar", "zst")), "gz": (gzip.compress(b"hello"), ("single", "gz")),
                 "zst": (zstd.compress(b"hello"), ("single", "zst")), "empty tar": (b"\0" * 10240, ("tar", None))}
        for name, (data, want) in cases.items():
            with self.subTest(name):
                with io.BytesIO(data) as fh:
                    self.assertEqual(extract.sniff(fh), want)
        for name, data in {"png": b"\x89PNG\r\n\x1a\n" + b"x" * 600, "empty": b"", "text": b"hi\n" * 400}.items():
            with self.subTest(name):
                with self.assertRaises(extract.BadInput) as cm, io.BytesIO(data) as fh:
                    extract.sniff(fh)
                self.assertIn("unsupported archive format", str(cm.exception))

    def test_a_200_mb_entry_streams_in_bounded_memory(self):  # NFR5
        # Bug caught: an entry is read whole into memory before it is written.
        temp = self.tmp()
        ex = extract.Extractor(temp, extract.Limits(1 << 40, 10), lambda n: None, lambda: None)
        tracemalloc.start()
        try:
            ex.add(extract.Entry("big.bin", "file", size=200 * MIB, open=lambda: Zeros(200 * MIB)))
            ex.finish()
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 64 * MIB)
        self.assertEqual((Path(temp) / "big.bin").stat().st_size, 200 * MIB)

    def test_a_lying_declared_size_cannot_write_past_the_limit(self):  # NFR2
        # Bug caught: the limit is checked against the declared size, so an entry that declares 1 KB and
        # delivers 50 MB is written out in full.
        temp = self.tmp()
        seen = []
        ex = extract.Extractor(temp, extract.Limits(1_000_000, 10), seen.append, lambda: None)
        with self.assertRaises(extract.BadInput) as cm:
            ex.add(extract.Entry("lie.bin", "file", size=1024, open=lambda: Zeros(50 * MIB)))
        self.assertLessEqual(sum(seen), 1_000_000 + MIB)
        self.assertIn("DOCK_EXTRACT_MAX_BYTES", str(cm.exception))

    def test_single_name_never_gives_an_empty_or_dot_name(self):  # R4
        # Bug caught: "..gz" loses its suffix to ".", and the run makes an empty folder and says exit 0.
        for archive, want in [("..gz", "archive"), ("...gz", "archive"), (".gz", "archive"), ("n.txt.gz", "n.txt"),
                              ("a.b", "a.b")]:
            with self.subTest(archive):
                self.assertEqual(extract.single_name(archive), want)

    def test_a_huge_tar_header_is_refused_before_it_is_held_in_memory(self):  # NFR5
        # Bug caught: tarfile reads a 20 MB PAX header whole into memory, uncounted (the header is "just a read").
        data = header_bomb(20 * MIB)
        tracemalloc.start()
        try:
            with self.assertRaises(extract.BadInput) as cm, io.BytesIO(data) as fh:
                extract.sniff(fh)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertIn("header", str(cm.exception))
        self.assertLess(peak, 12 * MIB)

    def test_implicit_parent_folders_count_against_the_entry_limit(self):  # R10
        # Bug caught: one entry per few hundred bytes makes up to 128 folders each, far past the entry limit.
        temp = self.tmp()
        ex = extract.Extractor(str(temp), extract.Limits(1 << 30, 5), lambda n: None, lambda: None)
        with self.assertRaises(extract.BadInput) as cm:
            ex.add(extract.Entry("a/b/c/d/e/f/g.txt", "file", open=lambda: Zeros(1)))
        ex.close()
        self.assertIn("DOCK_EXTRACT_MAX_ENTRIES", str(cm.exception))
        self.assertEqual(os.listdir(temp), [])

    def test_a_cancel_inside_finish_stops_it(self):  # R18
        # Bug caught: finish() checks for a cancel once, so a deep tree keeps being walked after a cancel.
        temp, armed = self.tmp(), []

        def stop():  # once armed: the 1st check passes (finish's own), the 2nd (inside its loops) cancels
            if armed:
                armed.append(1)
                if len(armed) > 2:
                    raise extract.Cancelled

        ex = extract.Extractor(str(temp), extract.Limits(1 << 30, 100), lambda n: None, stop)
        for name, kind in (("a/", "dir"), ("a/b/", "dir"), ("l", "symlink"), ("d/", "dir")):
            ex.add(extract.Entry(name, kind, target="a" if kind == "symlink" else None))
        armed.append(1)
        with self.assertRaises(extract.Cancelled):
            ex.finish()
        ex.close()

    def test_a_cancel_between_entries_removes_the_part_folder(self):  # R18
        # Bug caught: a cancel seen between two entries leaves the .part folder, or lets the run finish.
        cwd, gate, waiting, outcome = self.tmp(), threading.Event(), threading.Event(), []

        class Reader:
            total = archive_size = None

            def entries(self):
                yield extract.Entry("one.txt", "file", 3, open=lambda: io.BytesIO(b"one"))
                waiting.set()
                gate.wait(10)  # the second entry is not ready until the test says so
                yield extract.Entry("two.txt", "file", 3, open=lambda: io.BytesIO(b"two"))

            def close(self):
                pass

        archive = cwd / "a.tar"
        archive.write_bytes(b"")

        def run():
            with archive.open("rb") as fh, mock.patch.object(extract, "build_reader", lambda *a: Reader()):
                try:
                    outcome.append(extract.unpack(fh, "tar", None, "a.tar", "a", str(cwd), extract.Limits(1 << 30, 10)))
                except BaseException as e:  # handed to the test thread
                    outcome.append(e)

        worker = threading.Thread(target=run)
        worker.start()
        try:
            self.assertTrue(waiting.wait(10))
            (part,) = (p for p in cwd.iterdir() if p.name.endswith(".part"))
            self.assertEqual((part / "one.txt").read_bytes(), b"one")  # the first file is really there
            extract.cancelled.set()
        finally:
            gate.set()
            worker.join(10)
            extract.cancelled.clear()
        self.assertIsInstance(outcome[0], extract.Cancelled)
        self.assertEqual(sorted(p.name for p in cwd.iterdir()), ["a.tar"])  # the .part folder is gone, no "a"

    def test_a_cancelled_invoke_is_answered_32800(self):  # R18
        # Bug caught: a Cancelled escaping unpack is reported as an internal error (or not answered at all).
        sent = []
        extract.pending = 1  # the reader thread counts an invoke before the loop gets it
        with mock.patch.object(extract, "invoke", side_effect=extract.Cancelled), \
                mock.patch.object(extract, "send", sent.append):
            extract.handle({"id": 7, "method": "invoke", "params": {}})
        self.assertEqual(sent[0]["error"]["code"], -32800)
        self.assertEqual(extract.pending, 0)

    def test_a_failed_rename_is_a_write_error_not_a_damaged_archive(self):  # R6
        # Bug caught: the catch for reader errors also turns ENOSPC / ENOTEMPTY from os.rename into "damaged".
        cwd = self.tmp()
        archive = cwd / "a.tar"
        archive.write_bytes(tar_bytes(files_of(1)))
        for code, want in ((errno.ENOSPC, "no space left"), (errno.ENOTEMPTY, "x already exists"),
                           (errno.EEXIST, "x already exists")):
            with self.subTest(os.strerror(code)):
                with (archive.open("rb") as fh, mock.patch.object(extract.os, "rename", side_effect=OSError(code, "e")),
                      self.assertRaises(extract.BadInput) as cm):
                    extract.unpack(fh, "tar", None, "a.tar", "x", str(cwd), extract.Limits(1 << 30, 10))
                self.assertEqual(str(cm.exception), want)
                self.assertEqual(sorted(p.name for p in cwd.iterdir()), ["a.tar"])

    def test_a_zip_with_too_many_entries_is_refused_on_opening_and_checks_for_a_cancel(self):  # R10
        # Bug caught: the sizes of all entries are summed (a slow loop with no cancel check) before the count is checked.
        path = mkzip(self.tmp() / "z.zip", files_of(6))

        def stop():
            raise extract.Cancelled

        with path.open("rb") as fh:
            with self.assertRaises(extract.BadInput) as cm:
                extract.ZipReader(fh, extract.Limits(1 << 30, 5), lambda: None)
            self.assertIn("DOCK_EXTRACT_MAX_ENTRIES", str(cm.exception))
            with self.assertRaises(extract.Cancelled):
                extract.ZipReader(fh, extract.Limits(1 << 30, 100), stop)


# ---- the plugin end to end

class Basics(Base):
    def test_a_zip_is_unpacked_into_a_folder_named_after_it(self):  # R16, R4
        self.put("a.zip", mkzip(self.root / "t.zip", [("one.txt", b"1"), ("d/", b""), ("d/two.txt", b"22")]).read_bytes())
        answer, notes = self.run_plugin({"file": "a.zip"})
        dest = self.dir / "a"
        self.assertEqual(answer["result"], {"exit_code": 0, "outputs": {
            "dir": str(dest), "files": [str(dest / "one.txt"), str(dest / "d" / "two.txt")], "truncated": False}})
        self.assertEqual((dest / "d" / "two.txt").read_bytes(), b"22")
        self.assertEqual(sorted(os.listdir(self.dir)), ["a"])  # no .part left behind
        self.assertIn("extracted 2 files", self.said(notes, "info"))
        self.assertIn("into a/", self.said(notes, "info"))

    def test_every_format_is_found_by_content_whatever_the_suffix(self):  # R3, R5
        t = tar_bytes([ti("d/", tarfile.DIRTYPE), ti("d/x.txt", data=b"hello")])
        zip_path = mkzip(self.root / "z.zip", [("d/", b""), ("d/x.txt", b"hello")])
        archives = {"z.zip": zip_path.read_bytes(), "plain.tar": t, "c.tar.gz": gzip.compress(t),
                    "c.tar.bz2": bz2.compress(t), "c.tar.xz": lzma.compress(t), "c.tar.zst": zstd.compress(t),
                    "wrong.dat": gzip.compress(t), "nosuffix": zstd.compress(t), "liar.zip": t}
        for name, data in archives.items():
            with self.subTest(name):
                shutil_clean(self.dir)
                self.put(name, data)
                answer, _ = self.run_plugin({"file": name, "dir": "o"})
                self.assertEqual(answer["result"]["exit_code"], 0, answer)
                self.assertEqual((self.dir / "o" / "d" / "x.txt").read_bytes(), b"hello")
        for name, data in {"n.txt.gz": gzip.compress, "n.txt.bz2": bz2.compress, "n.txt.xz": lzma.compress,
                           "n.txt.zst": zstd.compress}.items():
            with self.subTest(name):
                shutil_clean(self.dir)
                self.put(name, data(b"just text\n"))
                answer, _ = self.run_plugin({"file": name})
                self.assertEqual((self.dir / "n.txt" / "n.txt").read_bytes(), b"just text\n")
                self.assertEqual(answer["result"]["outputs"]["files"], [str(self.dir / "n.txt" / "n.txt")])

    def test_a_png_is_unsupported(self):  # R3
        self.put("pic.png", b"\x89PNG\r\n\x1a\n" + b"x" * 600)
        before = self.tree()
        answer, notes = self.run_plugin({"file": "pic.png"})
        self.assertRefused(answer, notes, before, "unsupported archive format")

    def test_a_taken_name_gets_the_next_free_number_and_dir_names_the_folder(self):  # R4
        self.put("a.tar.gz", gzip.compress(tar_bytes(files_of(1))))
        for want in ("a", "a-2", "a-3"):
            answer, _ = self.run_plugin({"file": "a.tar.gz"})
            self.assertEqual(answer["result"]["outputs"]["dir"], str(self.dir / want))
        answer, _ = self.run_plugin({"file": "a.tar.gz", "dir": "out"})
        self.assertEqual(answer["result"]["outputs"]["dir"], str(self.dir / "out"))
        before = self.tree()
        for dirname in ("out", "x/y", ".."):  # taken, not a plain name, not a name
            with self.subTest(dirname):
                answer, notes = self.run_plugin({"file": "a.tar.gz", "dir": dirname})
                self.assertRefused(answer, notes, before)

    def test_a_1200_file_archive_lists_1000_and_says_truncated(self):  # R16
        self.put("many.tar", tar_bytes([ti(n, data=d) for n, d in files_of(1200)]))
        answer, _ = self.run_plugin({"file": "many.tar"})
        out = answer["result"]["outputs"]
        self.assertEqual((len(out["files"]), out["truncated"]), (1000, True))
        self.assertEqual(out["files"][0], str(self.dir / "many" / "f0.txt"))
        self.assertEqual(len(os.listdir(self.dir / "many")), 1200)

    def test_phase_two_options_are_refused_for_now(self):  # R17
        self.put("a.zip", mkzip(self.root / "t.zip", files_of(1)).read_bytes())
        before = self.tree()
        for extra in ({"list": True}, {"only": "*.txt"}):
            with self.subTest(extra):
                answer, notes = self.run_plugin({"file": "a.zip", **extra})
                self.assertRefused(answer, notes, before, "not supported yet")

    def test_wrong_input_types_are_32602_and_an_unknown_command_32601(self):  # R17
        self.put("a.zip", mkzip(self.root / "t.zip", files_of(1)).read_bytes())
        for inputs in ({"file": 5}, {}, {"file": "a.zip", "dir": 3}, {"file": "a.zip", "list": "yes"},
                       {"file": "a.zip", "only": 1}):
            with self.subTest(inputs):
                answer, _ = self.run_plugin(inputs)
                self.assertEqual(answer["error"]["code"], -32602)
        answer, _ = self.run_plugin({"file": "a.zip"}, params={"in_dir": 7})
        self.assertEqual(answer["error"]["code"], -32602)
        p = self.begin({"file": "a.zip"}, params={"command": "nope"})
        self.send(p, id=3, method="shutdown", params={})
        answer, _ = self.collect(p)
        self.assertEqual(answer["error"]["code"], -32601)


def shutil_clean(path):
    for p in Path(path).iterdir():
        shutil.rmtree(p) if p.is_dir() and not p.is_symlink() else p.unlink()


class Source(Base):
    def test_files_outside_the_in_folder_are_refused(self):  # R2
        # Bug caught: a path outside in_dir (parent part, absolute, symlink out, FIFO, NUL) is opened.
        mkzip(self.outside / "o.zip", files_of(1))
        (self.inn / "link.zip").symlink_to(self.outside / "o.zip")
        os.mkfifo(self.inn / "pipe.zip")
        before = self.tree()
        for name in ("../outside/o.zip", str(self.outside / "o.zip"), "link.zip", "pipe.zip", "a\0.zip", "missing.zip"):
            with self.subTest(name):
                answer, notes = self.run_plugin({"file": name})
                self.assertRefused(answer, notes, before)

    def test_a_file_in_the_work_folder_is_accepted(self):  # R2
        mkzip(self.work / "w.zip", files_of(1))
        (self.inn / "viaw.zip").symlink_to(self.work / "w.zip")
        for name in (str(self.work / "w.zip"), "viaw.zip"):
            with self.subTest(name):
                answer, _ = self.run_plugin({"file": name, "dir": f"o{len(name)}"}, work=self.work)
                self.assertEqual(answer["result"]["exit_code"], 0, answer)
        answer, notes = self.run_plugin({"file": str(self.work / "w.zip")})  # no work folder given: outside
        self.assertEqual(answer["result"], {"exit_code": 1})


class Safety(Base):
    def refuse(self, name, data, contains="", env=None, inputs=None):
        self.put(name, data)
        before = self.tree()
        answer, notes = self.run_plugin({"file": name, **(inputs or {})}, env=env)
        self.assertRefused(answer, notes, before, contains)

    def test_hostile_entry_names_fail_the_whole_run(self):  # R7
        # Bug caught: one bad entry among good ones is skipped or written, leaving a partial folder.
        good = ("good.txt", b"ok")
        cases = {"zip ..": mkzip(self.root / "1.zip", [good, ("a/../../evil", b"x")]),
                 "zip absolute": mkzip(self.root / "2.zip", [good, ("/tmp/evil", b"x")]),
                 "zip drive": mkzip(self.root / "3.zip", [good, ("C:/evil", b"x")]),
                 "zip backslash ..": mkzip(self.root / "4.zip", [good, ("a\\..\\..\\evil", b"x")]),
                 "zip long": mkzip(self.root / "5.zip", [good, ("d/" + "a" * 256, b"x")])}
        for name, path in cases.items():
            with self.subTest(name):
                self.refuse(f"{path.name}", path.read_bytes())
        for name, items in {"tar ..": [ti("../evil", data=b"x")], "tar absolute": [ti("/tmp/evil", data=b"x")],
                            "tar NUL": [ti("a\0b\u00e9", data=b"x")]}.items():
            with self.subTest(name):
                self.refuse(f"{name}.tar", tar_bytes([ti("good.txt", data=b"ok")] + items))
        self.put("ok.zip", mkzip(self.root / "ok.zip", [("./a\\b.txt", b"x"), ("./", b"")]).read_bytes())
        answer, _ = self.run_plugin({"file": "ok.zip"})
        self.assertEqual((self.dir / "ok" / "a" / "b.txt").read_bytes(), b"x")  # a backslash is a separator

    def test_links_that_stay_inside_are_kept_and_are_made_after_the_files(self):  # R8
        items = [ti("link", tarfile.SYMTYPE, link="d/f.txt"), ti("d/", tarfile.DIRTYPE), ti("d/f.txt", data=b"data"),
                 ti("hard", tarfile.LNKTYPE, link="d/f.txt"), ti("d/up", tarfile.SYMTYPE, link="../d/f.txt")]
        self.put("l.tar", tar_bytes(items))
        answer, _ = self.run_plugin({"file": "l.tar"})
        self.assertEqual(answer["result"]["exit_code"], 0, answer)
        dest = self.dir / "l"
        self.assertEqual(os.readlink(dest / "link"), "d/f.txt")
        self.assertEqual(os.readlink(dest / "d" / "up"), "../d/f.txt")
        self.assertEqual((dest / "hard").read_bytes(), b"data")
        self.assertEqual(os.stat(dest / "hard").st_ino, os.stat(dest / "d" / "f.txt").st_ino)
        self.assertEqual(answer["result"]["outputs"]["files"], [str(dest / "d" / "f.txt")])

    def test_links_that_leave_the_folder_or_are_written_through_fail(self):  # R8
        # Bug caught: a link out of the destination is created, or a later entry is written through a link.
        cases = {
            "symlink ../..": [ti("l", tarfile.SYMTYPE, link="../../x")],
            "symlink absolute": [ti("l", tarfile.SYMTYPE, link="/tmp")],
            "symlink via link": [ti("d/", tarfile.DIRTYPE), ti("d/a", tarfile.SYMTYPE, link=".."),
                                 ti("d/b", tarfile.SYMTYPE, link="a/../../x")],
            "file under symlink": [ti("d/", tarfile.DIRTYPE), ti("l", tarfile.SYMTYPE, link="d"),
                                   ti("l/evil", data=b"x")],
            "file under absolute symlink": [ti("l", tarfile.SYMTYPE, link="/tmp"), ti("l/evil", data=b"x")],
            "hardlink outside": [ti("h", tarfile.LNKTYPE, link="../outside/o.txt")],
            "hardlink absolute": [ti("h", tarfile.LNKTYPE, link="/etc/passwd")],
            "hardlink to a missing file": [ti("h", tarfile.LNKTYPE, link="nope")],
            "hardlink to a directory": [ti("d/", tarfile.DIRTYPE), ti("h", tarfile.LNKTYPE, link="d")],
        }
        for name, items in cases.items():
            with self.subTest(name):
                self.refuse("l.tar", tar_bytes([ti("good.txt", data=b"ok")] + items))

    def test_devices_and_fifos_fail_the_run(self):  # R9
        for name, kind in (("chr", tarfile.CHRTYPE), ("fifo", tarfile.FIFOTYPE)):
            with self.subTest(name):
                self.refuse("s.tar", tar_bytes([ti("good.txt", data=b"ok"), ti("dev", kind)]))

    def test_permissions_and_times_are_kept_but_setuid_is_stripped(self):  # R9
        items = [ti("exe", data=b"x", mode=0o4755), ti("priv", data=b"x", mode=0o000), ti("d/", tarfile.DIRTYPE, mode=0o2755),
                 ti("d/f", data=b"x", mode=0o640)]
        self.put("p.tar", tar_bytes(items))
        answer, _ = self.run_plugin({"file": "p.tar"})
        dest = self.dir / "p"
        self.assertEqual(stat.S_IMODE((dest / "exe").stat().st_mode), 0o755)
        self.assertFalse((dest / "exe").stat().st_mode & stat.S_ISUID)
        self.assertEqual(stat.S_IMODE((dest / "priv").stat().st_mode), 0o600)  # the owner can always read and write
        self.assertEqual(stat.S_IMODE((dest / "d" / "f").stat().st_mode), 0o640)
        self.assertEqual(stat.S_IMODE((dest / "d").stat().st_mode), 0o755)
        self.assertEqual(int((dest / "d" / "f").stat().st_mtime), MTIME)

    def test_the_limits_stop_the_run_and_name_themselves(self):  # R10
        # Bug caught: a limit is missing, off, or checked after the data is already on disk.
        lie = bytearray(mkzip(self.root / "lie.zip", [("big.bin", os.urandom(MIB) * 10)], zipfile.ZIP_STORED).read_bytes())
        at = lie.index(b"PK\x01\x02")
        lie[at + 24:at + 28] = (1024).to_bytes(4, "little")  # the central directory declares 1 KB
        cases = {
            "bytes honest": (mkzip(self.root / "a.zip", [("z", bytes(10 * MIB))], zipfile.ZIP_BZIP2).read_bytes(),
                             {"DOCK_EXTRACT_MAX_BYTES": "1000000"}, "DOCK_EXTRACT_MAX_BYTES"),
            "bytes lying": (bytes(lie), {"DOCK_EXTRACT_MAX_BYTES": "1000000"}, ""),
            "entries": (tar_bytes([ti(n, data=d) for n, d in files_of(6)]), {"DOCK_EXTRACT_MAX_ENTRIES": "5"},
                        "DOCK_EXTRACT_MAX_ENTRIES"),
            "zip ratio": (mkzip(self.root / "b.zip", [("z", bytes(5 * MIB))], zipfile.ZIP_BZIP2).read_bytes(), {}, "ratio"),
            "tar ratio": (bz2.compress(tar_bytes([ti("z", data=bytes(20 * MIB))])), {}, "ratio"),
        }
        for name, (data, env, contains) in cases.items():
            with self.subTest(name):
                self.refuse(f"{name.replace(' ', '_')}.dat", data, contains, env)
        self.put("ok.tar", tar_bytes(files_of(5)))
        answer, _ = self.run_plugin({"file": "ok.tar"}, env={"DOCK_EXTRACT_MAX_ENTRIES": "5"})  # the limit itself is fine
        self.assertEqual(answer["result"]["exit_code"], 0)

    def test_a_limit_that_is_not_a_positive_integer_is_refused(self):  # R10
        self.put("a.tar", tar_bytes(files_of(1)))
        before = self.tree()
        for var in ("DOCK_EXTRACT_MAX_BYTES", "DOCK_EXTRACT_MAX_ENTRIES"):
            for value in ("abc", "0", "-5", "1.5"):
                with self.subTest(f"{var}={value}"):
                    answer, notes = self.run_plugin({"file": "a.tar"}, env={var: value})
                    self.assertRefused(answer, notes, before, var)

    def test_colliding_names_fail(self):  # R11
        # Bug caught: A.txt and a.txt (one file on a case-insensitive disk), or a file that is also a folder.
        cases = {"case": [ti("A.txt", data=b"1"), ti("a.txt", data=b"2")],
                 "same path": [ti("x", data=b"1"), ti("x", data=b"2")],
                 "file then child": [ti("d", data=b"1"), ti("d/x", data=b"2")],
                 "child then file": [ti("d/x", data=b"2"), ti("D", data=b"1")]}
        for name, items in cases.items():
            with self.subTest(name):
                self.refuse("c.tar", tar_bytes(items))

    def test_an_encrypted_zip_is_refused_for_now(self):  # R17
        raw = bytearray(mkzip(self.root / "e.zip", files_of(1)).read_bytes())
        for sig, off in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
            raw[raw.index(sig) + off] |= 1  # the "encrypted" general purpose flag
        self.refuse("e.zip", bytes(raw), "encrypted")

    def test_a_path_deeper_than_128_parts_is_refused(self):  # R10
        # Bug caught: a 1000-deep entry is accepted and walked again from the root for every folder (64 s).
        self.refuse("deep.tar", tar_bytes([ti("/".join(["d"] * 129), data=b"x")]), "128")

    def test_a_128_deep_tree_with_many_folders_unpacks_fast(self):  # R10
        # Bug caught: every entry and every folder is walked from the root again, so deep trees take seconds.
        chain = ["/".join(["d"] * i) for i in range(1, 128)]
        base = chain[-1]
        items = [ti(f"{d}/", tarfile.DIRTYPE) for d in chain] + [ti(f"{base}/x{i}/", tarfile.DIRTYPE) for i in range(800)]
        self.put("deep.tar", tar_bytes(items + [ti(f"{base}/f.txt", data=b"x")]))
        start = time.monotonic()
        answer, _ = self.run_plugin({"file": "deep.tar"})
        self.assertLess(time.monotonic() - start, 2.0)
        self.assertEqual(answer["result"]["exit_code"], 0, answer)
        self.assertEqual((self.dir / "deep" / base / "f.txt").read_bytes(), b"x")
        self.assertTrue((self.dir / "deep" / base / "x799").is_dir())

    def test_a_member_named_like_the_archive_root_is_refused_not_skipped(self):  # R7
        # Bug caught: a "./" regular member is dropped silently while tarfile decompresses its 2 GiB body.
        info = tarfile.TarInfo("./")
        info.size = 2**31
        self.refuse("root.tar.gz", gz_stream(info.tobuf(tarfile.USTAR_FORMAT), bytes(2 * MIB)), "archive root")

    def test_a_nul_in_a_zip_entry_name_is_refused_not_truncated(self):  # R7
        # Bug caught: zipfile cuts the name at the NUL, so "a<NUL>../../x" is written as "a" and passes.
        raw = mkzip(self.root / "n.zip", [("aZ../../x", b"x")]).read_bytes()
        self.refuse("n.zip", raw.replace(b"aZ../../x", b"a\0../../x"), "NUL")

    def test_an_unsupported_zip_compression_method_is_named(self):  # R6
        # Bug caught: NotImplementedError from zipfile (deflate64, method 9) is reported as "archive is damaged".
        raw = bytearray(mkzip(self.root / "c.zip", files_of(1), zipfile.ZIP_STORED).read_bytes())
        raw[raw.index(b"PK\x03\x04") + 8] = 9
        raw[raw.index(b"PK\x01\x02") + 10] = 9
        self.refuse("c.zip", bytes(raw), "unsupported zip compression")

    def test_a_damaged_tar_is_not_mistaken_for_a_single_file(self):  # R6
        # Bug caught: a tar(.gz) with a bad first header falls through to "single" and is written as garbage, exit 0.
        good = tar_bytes(files_of(2))
        bad = bytearray(good)
        bad[148] ^= 0xFF  # the checksum field of the first header
        cases = {"hdr.tar.gz": gzip.compress(bytes(bad)), "magic.dat": gzip.compress(bytes(bad)),
                 "cut.tgz": gzip.compress(good)[:12], "hdr.tar": bytes(bad), "hdr.tar.xz": lzma.compress(bytes(bad))}
        for name, data in cases.items():
            with self.subTest(name):
                self.refuse(name, data, "archive is damaged")


class Failures(Base):
    def test_a_damaged_archive_leaves_nothing_behind_and_other_part_dirs_alone(self):  # R6, R17
        # Bug caught: a half-written folder or temp dir is left, or an unrelated .part dir is deleted.
        (self.dir / ".extract-old.part").mkdir()
        (self.dir / ".extract-old.part" / "keep").write_text("mine")
        full = tar_bytes([ti(f"f{i}.txt", data=bytes(2000)) for i in range(5)])
        cut = full[:2 * 2560 + 512 + 700]  # a member is 512 + 2048 bytes: cut inside the 3rd member's data
        for name, data in (("cut.tar", cut), ("cut.tar.gz", gzip.compress(full)[:len(gzip.compress(full)) // 2]),
                           ("cut.zip", bytes(zip_with_bad_crc(self.root)))):
            with self.subTest(name):
                self.put(name, data)
                before = self.tree()
                answer, notes = self.run_plugin({"file": name})
                self.assertRefused(answer, notes, before, "archive is damaged")

    def test_a_cancel_with_nothing_running_is_ignored(self):  # R18
        self.put("a.tar", tar_bytes(files_of(1)))
        p = self.begin({"file": "a.tar"})
        self.send(p, id=3, method="shutdown", params={})
        answer, _ = self.collect(p)
        self.assertEqual(answer["result"]["exit_code"], 0)
        p = subprocess.Popen([sys.executable, str(EXTRACT)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.send(p, method="cancel", params={})
        self.send(p, id=2, method="invoke", params={"command": "extract", "cwd": str(self.dir),
                                                    "in_dir": str(self.inn), "inputs": {"file": "a.tar", "dir": "b"}})
        self.send(p, id=3, method="shutdown", params={})
        answer, _ = self.collect(p)
        self.assertEqual(answer["result"]["exit_code"], 0)

    def test_progress_rises_at_most_five_times_a_second_and_never_passes_99_before_the_end(self):  # R15
        # Bug caught: a bar per block (flooding the host), a percent that falls or reaches 100 before the end.
        # A fake clock instead of a long archive: 100 blocks over one second of fake time.
        bars, now = [], [100.0]
        clock = types.SimpleNamespace(monotonic=lambda: now[0])
        with mock.patch.object(extract, "bar", lambda percent, **kw: bars.append(percent)), \
                mock.patch.object(extract, "time", clock):
            progress = extract.Progress(1000)
            for _ in range(100):
                now[0] += 0.01
                progress.update(10)
        self.assertTrue(1 < len(bars) <= 5, bars)
        self.assertEqual(bars, sorted(bars))
        self.assertTrue(all(isinstance(x, int) and 0 <= x <= 99 for x in bars), bars)
        self.assertTrue(any(0 < x < 99 for x in bars), bars)

    def test_a_zip_reports_progress_and_ends_with_100_done(self):  # R15
        self.put("a.zip", mkzip(self.root / "t.zip", files_of(3, 5000)).read_bytes())
        answer, notes = self.run_plugin({"file": "a.zip"})
        self.assertEqual(answer["result"]["exit_code"], 0)
        bars = [n["params"] for n in notes if n["method"] == "bar"]
        self.assertEqual({(b["id"], b["label"]) for b in bars}, {("extract", "extracting")})
        self.assertEqual(bars[-1], {"id": "extract", "label": "extracting", "percent": 100, "done": True})
        percents = [b["percent"] for b in bars]
        self.assertTrue(all(isinstance(x, int) for x in percents))
        self.assertEqual(percents, sorted(percents))
        self.assertTrue(all(x <= 99 for x in percents[:-1]))

    def test_a_tar_has_no_declared_total_so_its_percent_is_null(self):  # R15
        self.put("a.tar", tar_bytes(files_of(3)))
        answer, notes = self.run_plugin({"file": "a.tar"})
        bars = [n["params"] for n in notes if n["method"] == "bar"]
        self.assertTrue(all(b["percent"] is None for b in bars[:-1]))
        self.assertEqual(bars[-1]["percent"], 100)


def zip_with_bad_crc(root):
    """5 stored entries of 1 KB with a byte flipped inside the 3rd one's data."""
    path = mkzip(root / "crc.zip", [(f"f{i}", bytes([i]) * 1000) for i in range(5)], zipfile.ZIP_STORED)
    raw = bytearray(path.read_bytes())
    raw[raw.index(bytes([2]) * 1000) + 500] ^= 0xFF
    path.unlink()
    return raw


if __name__ == "__main__":
    unittest.main()
