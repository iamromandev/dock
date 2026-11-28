"""extract plugin for dock: unpacks an archive (zip, tar, tar.gz/bz2/xz/zst, or a single .gz/.bz2/.xz/.zst file)
safely into a new folder (stdlib only). JSON-RPC 2.0, one JSON object line on stdin/stdout. See protocol/PROTOCOL.md.

--list shows what an unpack would do (one table, nothing written); --only keeps entries matching glob patterns; a zip
encrypted with the old ZipCrypto scheme asks the host for its password through a `form` request.
"""

import bz2
import errno
import fnmatch
import gzip
import json
import lzma
import os
import queue
import re
import shutil
import stat
import sys
import tarfile
import tempfile
import threading
import time
import zipfile
import zlib
from collections.abc import Callable
from compression import zstd
from dataclasses import dataclass, field
from typing import Any

BLOCK = 1 << 20  # bytes read, written and checked per step
DEFAULT_MAX_BYTES = 10 * 1024**3
DEFAULT_MAX_ENTRIES = 100_000
RATIO = 1000  # unpacked : packed
MAX_NAME = 4096  # bytes in an entry name
MAX_PART = 255  # bytes in one name component
MAX_DEPTH = 128  # components in an entry name
MAX_HEADER = 4 * 1024**2  # bytes of one tar member's headers (a PAX or GNU long-name header is held in memory)
MAX_FILES_LISTED = 1000
MAX_ROWS = 1000  # rows in the --list table
BAR_EVERY = 0.25  # seconds between progress bars (4 a second; the final bar makes it at most 5)
SUFFIXES = (".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst", ".zip", ".tar", ".tgz", ".tbz2", ".txz", ".tzst",
            ".gz", ".bz2", ".xz", ".zst")
COMPRESSION_SUFFIXES = (".gz", ".bz2", ".xz", ".zst")
TAR_SUFFIXES = (".tar", ".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst", ".tgz", ".tbz2", ".txz", ".tzst")
MAGIC = {"gz": b"\x1f\x8b", "bz2": b"BZh", "xz": b"\xfd7zXZ\x00", "zst": b"\x28\xb5\x2f\xfd"}
# what a decompressor or archive reader raises on damaged data
DAMAGE = (zipfile.BadZipFile, tarfile.TarError, EOFError, lzma.LZMAError, zlib.error, zstd.ZstdError, OSError,
          ValueError, NotImplementedError, RuntimeError)


class Cancelled(Exception):
    pass


class Failure(Exception):
    def __init__(self, message, code=-32000):
        super().__init__(message)
        self.code = code


class BadInput(Exception):
    """An input or archive the user can fix; the message is told to the user."""


inbox: queue.Queue = queue.Queue()
cancelled = threading.Event()
state_lock = threading.Lock()
pending = 0  # invokes read from stdin and not yet finished; guarded by state_lock. A cancel with none is ignored.
replies: dict[str, queue.Queue] = {}  # request id -> where the reader puts the host's answer; guarded by state_lock
request_ids = iter(range(1, 1 << 62))  # ids of our requests ("x1", ...), apart from the host's own ids


def send(msg):
    try:
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", **msg}) + "\n")
        sys.stdout.flush()
    except OSError:  # the host is gone (broken pipe); nobody to tell
        pass


def notify(method, **params):
    send({"method": method, "params": params})


def say(level, text):
    notify("log", level=level, message=text)


def bar(percent, **extra):
    notify("bar", id="extract", label="extracting", percent=percent, **extra)


def stop_if_cancelled():
    if cancelled.is_set():
        raise Cancelled()


def ask(method, **params):
    """A request to the host; waits for its answer (the result), checking for a cancel every 0.1 s."""
    with state_lock:
        req_id = f"x{next(request_ids)}"
        box: queue.Queue = queue.Queue()
        replies[req_id] = box
    try:
        send({"id": req_id, "method": method, "params": params})
        while True:
            try:
                reply = box.get(timeout=0.1)
                break
            except queue.Empty:
                stop_if_cancelled()
    finally:
        with state_lock:
            replies.pop(req_id, None)  # an answer that comes later finds nobody and is dropped
    stop_if_cancelled()
    if "error" in reply:
        error = reply["error"]
        raise BadInput(str(error.get("message") if isinstance(error, dict) else error)[:200])
    result = reply.get("result")
    return result if isinstance(result, dict) else {}


# ---- entries

@dataclass
class Entry:
    """One archive member. `open()` gives a readable (and closable) body of a file; it is called once, in order."""

    name: str
    kind: str  # file | dir | symlink | hardlink | special
    size: int | None = 0  # declared; never trusted; None when unknown (a single compressed file)
    mode: int | None = None
    mtime: float | None = None
    target: str | None = None  # symlinks and hard links
    encrypted: str | None = None  # None | "zipcrypto" | "aes"
    packed: int | None = None  # compressed size, for the per-entry ratio (zip)
    problem: str | None = None  # why the entry cannot be read (a symlink's target); the checks refuse it with this
    open: Callable[[], Any] | None = field(default=None, repr=False)


def check_name(raw):
    """The normalised relative name ("" for the archive root itself), or BadInput for a hostile one."""
    shown = repr(raw[:60])
    if "\0" in raw:
        raise BadInput(f"entry name {shown} has a NUL character")
    try:
        size = len(os.fsencode(raw))
    except UnicodeEncodeError:
        raise BadInput(f"entry name {shown} is not a valid name") from None
    if size > MAX_NAME:
        raise BadInput(f"entry name {shown} is longer than {MAX_NAME} bytes")
    name = raw.replace("\\", "/")
    if name.startswith("/"):
        raise BadInput(f"entry name {shown} is an absolute path")
    if re.match(r"[A-Za-z]:", name):
        raise BadInput(f"entry name {shown} starts with a drive letter")
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if ".." in parts:
        raise BadInput(f"entry name {shown} leaves the destination folder")
    if len(parts) > MAX_DEPTH:
        raise BadInput(f"entry name {shown} is deeper than {MAX_DEPTH} folders (the limit)")
    if any(len(os.fsencode(p)) > MAX_PART for p in parts):
        raise BadInput(f"entry name {shown} has a part longer than {MAX_PART} bytes")
    return "/".join(parts)


def check_link_target(name, target):
    """A symlink at `name` may point only inside the destination: no absolute target, and `..` only as leading
    parts that stay inside (a `..` after a normal part could pass through another link)."""
    shown = repr(name[:60])
    if not target or "\0" in target or target.startswith("/"):
        raise BadInput(f"link {shown} has a target that is empty, absolute or has a NUL character")
    parts = [p for p in target.split("/") if p not in ("", ".")]
    ups = 0
    while ups < len(parts) and parts[ups] == "..":
        ups += 1
    if ".." in parts[ups:] or ups > name.count("/"):
        raise BadInput(f"link {shown} points outside the destination folder")


class Checker:
    """The safety checks over a run's entries, in archive order and without touching the disk: names, kinds, link
    targets, collisions and the entry count. `check` raises BadInput for an entry that must be refused; the state
    (names seen, regular files) is the same whether the entries are then listed or written."""

    def __init__(self, limits):
        self.limits = limits
        self.count = 0
        self.seen: dict[str, str] = {}  # casefolded path of every entry -> its kind
        self.dirs: set[str] = set()  # casefolded paths that are a folder (explicit or a parent)
        self.regular: set[tuple] = set()  # parts of every regular file seen
        self.kept: set[tuple] = set()  # parts of the regular files that are kept (not filtered out)

    def tick(self):
        self.count += 1
        if self.count > self.limits.max_entries:
            raise BadInput(too_many_entries(self.limits))

    def check(self, entry, keep=True):
        """The normalised name and its parts (("", []) for the archive root)."""
        name = check_name(entry.name)
        shown = repr(entry.name[:60])
        if entry.problem:
            raise BadInput(entry.problem)
        if entry.kind == "special":
            raise BadInput(f"entry {shown} is a device, a pipe or another special file")
        if not name:
            if entry.kind != "dir":  # not skipped: its body would still be read
                raise BadInput(f"entry {shown} is not a folder but is named like the archive root")
            return "", []
        parts = name.split("/")
        self.collide(name, parts, entry.kind, shown)
        if entry.kind == "symlink":
            if entry.target is not None or not entry.encrypted:  # an unread encrypted link has no target to check
                check_link_target(name, entry.target or "")
        elif entry.kind == "hardlink":
            target = tuple(check_name(entry.target or "").split("/"))
            if target not in self.regular:
                raise BadInput(f"hard link {shown} points to something that is not a regular file already unpacked")
            if keep and target not in self.kept:
                raise BadInput(f"hard link {shown} points to {entry.target[:60]!r}, which --only left out")
        elif entry.kind == "file":
            self.regular.add(tuple(parts))
            if keep:
                self.kept.add(tuple(parts))
        return name, parts

    def collide(self, name, parts, kind, shown):
        key = name.casefold()
        if key in self.seen or (kind != "dir" and key in self.dirs):
            raise BadInput(f"entry {shown} collides with another entry of the same name")
        keys = ["/".join(parts[:i]).casefold() for i in range(1, len(parts))]
        for parent in keys:
            if parent in self.seen and self.seen[parent] != "dir":
                raise BadInput(f"entry {shown} is inside a name that is a file or a link")
        self.seen[key] = kind
        self.dirs.update(keys)
        if kind == "dir":
            self.dirs.add(key)


# ---- limits and writing

@dataclass
class Limits:
    max_bytes: int
    max_entries: int
    ratio: int = RATIO


def too_many_entries(limits):
    return f"the archive has more than {limits.max_entries} entries (limit DOCK_EXTRACT_MAX_ENTRIES)"


class Extractor:
    """Writes entries into the private folder `temp`, never through a link and never outside it.

    `on_bytes(n)` is told each written block; `stop()` raises Cancelled; `archive_size` (tar and single files, not
    zip) enables the whole-stream ratio check. Links and directory metadata are applied by finish()."""

    def __init__(self, temp, limits, on_bytes, stop, archive_size=None, keep=None):
        self.temp = temp
        self.keep = keep or (lambda name: True)  # which normalised names are extracted
        self.checker = Checker(limits)
        self.limits = limits
        self.on_bytes = on_bytes
        self.stop = stop
        self.archive_size = archive_size
        self.root = os.open(temp, os.O_RDONLY | os.O_DIRECTORY)
        self.fds = [self.root]  # fds[i]: the open folder of the first i parts of the last path asked for (a cache)
        self.cached: list[str] = []
        self.written = 0
        self.dirmeta: dict[tuple, tuple] = {}  # folder parts -> (mode, mtime)
        self.links: list[Entry] = []
        self.files: list[tuple] = []  # parts of the regular files, in archive order

    def close(self):
        for fd in self.fds:
            try:
                os.close(fd)
            except OSError:
                pass
        self.fds = [self.root]
        self.cached = []

    # -- folders through directory fds, so no link is ever followed

    def dir_fd(self, parts):
        """The fd of the folder `parts` (made when missing), owned by the cache: the caller must not close it, and it
        stays valid only until the next call. Folders shared with the previous path are not walked again, so a run
        over many entries costs time in step with the folders it makes, not with their depth."""
        keep = 0
        while keep < len(self.cached) and keep < len(parts) and self.cached[keep] == parts[keep]:
            keep += 1
        while len(self.cached) > keep:
            self.cached.pop()
            os.close(self.fds.pop())
        for part in parts[keep:]:
            try:
                os.mkdir(part, 0o700, dir_fd=self.fds[-1])
            except FileExistsError:
                pass
            self.fds.append(os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.fds[-1]))
            self.cached.append(part)
        return self.fds[-1]

    def open_dir(self, parts):  # a private fd for the caller to close (no cache), for a second folder at once
        fd = os.dup(self.root)
        try:
            for part in parts:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = nxt
        except BaseException:
            os.close(fd)
            raise
        return fd

    def make_parents(self, parts):
        for i in range(1, len(parts)):
            self.dirmeta.setdefault(tuple(parts[:i]), (None, None))
        self.check_folders()

    def check_folders(self):
        # parents are made implicitly, so one entry can make up to MAX_DEPTH folders: the entry limit caps them too
        if len(self.dirmeta) > self.limits.max_entries:
            raise BadInput(f"the archive has more than {self.limits.max_entries} folders "
                           "(limit DOCK_EXTRACT_MAX_ENTRIES)")

    # -- entries

    def add(self, entry):
        """Checks the entry (all of them, kept or not) and writes it if kept. Returns whether it was written."""
        self.stop()
        self.checker.tick()
        probe = check_name(entry.name)
        wanted = self.keep(probe)
        name, parts = self.checker.check(entry, wanted)
        if not name or not wanted:
            return False  # the archive root, or an entry left out by --only
        try:
            if entry.kind == "dir":
                self.make_parents(parts)
                self.dirmeta[tuple(parts)] = (entry.mode, entry.mtime)
                self.check_folders()
                self.dir_fd(parts)
            elif entry.kind == "file":
                self.make_parents(parts)
                self.write_file(parts, entry)
                self.files.append(tuple(parts))
            else:
                self.make_parents(parts)
                self.links.append(entry)
        except OSError as e:
            raise write_error(e) from None
        return True

    def write_file(self, parts, entry):
        fd = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
                     dir_fd=self.dir_fd(parts[:-1]))
        start = self.written
        try:
            with os.fdopen(fd, "wb", closefd=False) as out:
                self.copy(entry, out, start)
            mode = (entry.mode if entry.mode is not None else 0o644) & 0o777 | 0o600
            os.fchmod(fd, mode)
            set_time(fd, entry.mtime)
        finally:
            os.close(fd)

    def copy(self, entry, out, start):
        limits = self.limits
        try:
            src = entry.open()
        except DAMAGE as e:
            raise Damaged(e) from None
        with src:
            while True:
                self.stop()
                try:
                    block = src.read(BLOCK)
                except DAMAGE as e:
                    raise Damaged(e) from None
                if not block:
                    break
                if self.written + len(block) > limits.max_bytes:
                    raise BadInput(f"the unpacked data is over {limits.max_bytes} bytes "
                                   "(limit DOCK_EXTRACT_MAX_BYTES)")
                out.write(block)
                self.written += len(block)
                self.on_bytes(len(block))
                self.check_ratio(entry, self.written - start)

    def check_ratio(self, entry, entry_written):
        ratio = self.limits.ratio
        if entry.packed is not None:  # zip: per entry
            if entry_written > BLOCK and entry_written / max(entry.packed, 1) > ratio:
                raise BadInput(f"entry {entry.name[:60]!r} unpacks over {ratio}:1 (compression ratio limit)")
        elif self.archive_size is not None and self.written > BLOCK and self.written / max(self.archive_size, 1) > ratio:
            raise BadInput(f"the archive unpacks over {ratio}:1 (compression ratio limit)")

    # -- the end

    def finish(self):
        """Makes the links, then applies folder modes and times (deepest first). Returns the regular files' parts."""
        self.stop()
        try:
            for entry in self.links:
                self.stop()
                parts = check_name(entry.name).split("/")
                pfd = self.dir_fd(parts[:-1])
                if entry.kind == "symlink":
                    os.symlink(entry.target or "", parts[-1], dir_fd=pfd)
                else:
                    tparts = check_name(entry.target).split("/")
                    tfd = self.open_dir(tparts[:-1])  # a second folder at once, so not through the cache
                    try:
                        os.link(tparts[-1], parts[-1], src_dir_fd=tfd, dst_dir_fd=pfd, follow_symlinks=False)
                    finally:
                        os.close(tfd)
            self.dirmeta[()] = (0o755, None)
            # Parents sort before their children, and every parent is a key, so the cached path moves one step at a
            # time. A folder's own mode keeps the owner's rwx, so changing a parent first never locks us out.
            for parts in sorted(self.dirmeta):
                self.stop()
                mode, mtime = self.dirmeta[parts]
                fd = self.dir_fd(list(parts))
                os.fchmod(fd, (mode if mode is not None else 0o755) & 0o777 | 0o700)
                set_time(fd, mtime)
        except OSError as e:
            raise write_error(e) from None
        return self.files


class Damaged(Exception):
    """The archive's data is bad; `reason` is the decompressor's or reader's message."""

    def __init__(self, reason):
        super().__init__(str(reason) or type(reason).__name__)


def write_error(e, exists=None):
    """A BadInput for a filesystem error; `exists` is the name told when the error means it is already there."""
    if e.errno == errno.ENOSPC:
        return BadInput("no space left")
    if exists and e.errno in (errno.ENOTEMPTY, errno.EEXIST):
        return BadInput(f"{exists} already exists")
    return BadInput(f"cannot write the files: {e.strerror or e}")


def set_time(fd, mtime):
    if mtime is None:
        return
    try:
        ns = int(mtime * 1_000_000_000)
        os.utime(fd, ns=(ns, ns))
    except (OSError, OverflowError, ValueError):
        pass  # a time the disk cannot hold is not worth failing for


# ---- formats

def decompressor(comp, fh):
    if comp == "gz":
        return gzip.GzipFile(fileobj=fh)
    return {"bz2": bz2.BZ2File, "xz": lzma.LZMAFile, "zst": zstd.ZstdFile}[comp](fh)


class CountingReader:
    """The bytes of a tar (already decompressed) as tarfile reads them, in "r:" mode: forward only, so tarfile's
    reads and seeks all pass through here. Every byte read or skipped counts against the byte limit and the stream's
    compression ratio (with `limits`), each read checks for a cancel, and a member's headers (a PAX or GNU long name
    is held whole in memory by tarfile) may not pass MAX_HEADER: tarfile asks for big headers in 1 MiB reads, so the
    size of a read says nothing; what a member's header reads add up to does."""

    def __init__(self, raw, stop, limits=None, archive_size=None):
        self.raw, self.stop, self.limits, self.archive_size = raw, stop, limits, archive_size
        self.pos = 0
        self.in_header = True  # tarfile reads the first header while it opens
        self.header_bytes = 0

    def begin_header(self):
        self.in_header, self.header_bytes = True, 0

    def tell(self):
        return self.pos

    def seekable(self):
        return True

    def close(self):
        pass

    def read(self, n=-1):
        if n is None or n < 0 or n > MAX_HEADER:
            raise BadInput(f"the tar asks for a read of {n} bytes (over the {MAX_HEADER} byte limit)")
        return self.take(n, self.in_header)

    def seek(self, offset, whence=os.SEEK_SET):
        if whence != os.SEEK_SET or offset < self.pos:
            raise tarfile.ReadError("the tar data is out of order")
        while self.pos < offset:  # skipping is reading: the skipped bytes count too
            if not self.take(min(BLOCK, offset - self.pos), False):
                break
        return self.pos

    def take(self, n, header):
        self.stop()
        data = self.raw.read(n)
        self.pos += len(data)
        if header:
            self.header_bytes += len(data)
            if self.header_bytes > MAX_HEADER:
                raise BadInput(f"a tar header is over {MAX_HEADER} bytes (the archive is damaged or hostile)")
        if self.limits:
            if self.pos > self.limits.max_bytes:
                raise BadInput(f"the unpacked data is over {self.limits.max_bytes} bytes "
                               "(limit DOCK_EXTRACT_MAX_BYTES)")
            if self.pos > BLOCK and self.pos / max(self.archive_size or 1, 1) > self.limits.ratio:
                raise BadInput(f"the archive unpacks over {self.limits.ratio}:1 (compression ratio limit)")
        return data


def open_tar(counting):
    return tarfile.open(fileobj=counting, mode="r:")  # the caller closes it; reads the first header


def sniff(fh, name=""):
    """(format, compression): ("zip", None), ("tar", None|comp) or ("single", comp). BadInput if unsupported; Damaged
    for a tar that does not parse, when its content (a "ustar" magic) or the archive's name says it is a tar."""
    fh.seek(0)
    head = fh.read(8)
    comp = next((c for c, magic in MAGIC.items() if head.startswith(magic)), None)
    fh.seek(0)
    if comp is None and head[:4] in (b"PK\x03\x04", b"PK\x05\x06") and zipfile.is_zipfile(fh):
        return "zip", None
    fh.seek(0)
    raw = decompressor(comp, fh) if comp else fh
    try:
        with open_tar(CountingReader(raw, stop_if_cancelled)):  # a member, or none for a valid empty tar
            pass
        return "tar", comp
    except (*DAMAGE, StopIteration) as e:
        failure = e
    finally:
        if comp:
            raw.close()
    fh.seek(0)
    first = b""
    try:
        first = (decompressor(comp, fh) if comp else fh).read(512)
    except DAMAGE:
        pass  # no readable start: only the name can say
    if first[257:262] == b"ustar" or name.lower().endswith(TAR_SUFFIXES):
        raise Damaged(failure)
    if comp:
        return "single", comp
    raise BadInput("unsupported archive format")


class CryptBody:
    """An encrypted entry's body: a bad password shows up as an error or a CRC mismatch, on any such entry."""

    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.body.close()
        return False

    def read(self, n=-1):
        try:
            return self.body.read(n)
        except (zipfile.BadZipFile, RuntimeError, zlib.error, EOFError):
            raise BadInput("wrong password") from None


def zip_kind(info):
    mode = info.external_attr >> 16
    if info.is_dir() or stat.S_ISDIR(mode):
        return "dir"
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_IFMT(mode) in (stat.S_IFCHR, stat.S_IFBLK, stat.S_IFIFO, stat.S_IFSOCK):
        return "special"
    return "file"


class ZipReader:
    def __init__(self, fh, limits, stop, password=None):
        """`password` is bytes (or None); no error raised here or below may carry its text."""
        self.stop = stop
        self.zf = zipfile.ZipFile(fh)
        self.password = password
        if password:
            self.zf.setpassword(password)
        self.archive_size = None
        try:
            infos = self.zf.infolist()
            if len(infos) > limits.max_entries:  # before anything is summed or written
                raise BadInput(too_many_entries(limits))
            total = 0
            for info in infos:
                stop()
                if not info.is_dir():
                    total += info.file_size
            self.total = total or None
        except BaseException:
            self.zf.close()
            raise

    def open(self, info):
        encrypted = bool(info.flag_bits & 0x1)
        try:
            body = self.zf.open(info)
        except NotImplementedError:  # a compression method zipfile cannot read (deflate64, ...)
            raise BadInput("unsupported zip compression") from None
        except (zipfile.BadZipFile, RuntimeError, zlib.error, EOFError):
            if encrypted:  # "Bad password for file ..."; the text is replaced, whatever it holds
                raise BadInput("wrong password") from None
            raise
        except Exception:
            if encrypted:
                raise BadInput("cannot read an encrypted entry") from None
            raise
        return CryptBody(body) if encrypted else body

    def entries(self):
        for info in self.zf.infolist():
            self.stop()
            mode = info.external_attr >> 16
            kind = zip_kind(info)
            encrypted = "aes" if info.compress_type == 99 else "zipcrypto" if info.flag_bits & 0x1 else None
            when = None
            try:
                when = time.mktime((*info.date_time, 0, 0, -1))
            except (OverflowError, ValueError):
                pass
            # orig_filename: the name as stored; filename is cut at a NUL, which would hide the rest from check_name
            entry = Entry(info.orig_filename, kind, info.file_size, (mode & 0o7777) or None, when,
                          encrypted=encrypted, packed=info.compress_size)
            if encrypted and not (encrypted == "zipcrypto" and self.password):
                pass  # not read: listed only (AES, or no password)
            elif kind == "symlink":
                try:
                    with self.open(info) as body:
                        entry.target = body.read(MAX_NAME + 1).decode("utf-8", "surrogateescape")
                except BadInput as e:  # told when the entry is checked, so a listing can mark the row
                    entry.problem = str(e)
                except DAMAGE as e:
                    entry.problem = f"archive is damaged: {e or type(e).__name__}"
            elif kind == "file":
                entry.open = lambda info=info: self.open(info)
            yield entry

    def close(self):
        self.zf.close()


class TarReader:
    def __init__(self, fh, comp, size, limits, stop):
        self.raw = decompressor(comp, fh) if comp else fh
        self.src = CountingReader(self.raw, stop, limits, size)
        try:
            self.tf = open_tar(self.src)
        except BaseException:
            self.close_raw(comp)
            raise
        self.comp = comp
        self.archive_size = size
        self.total = None

    def close_raw(self, comp):
        if comp:
            self.raw.close()

    def entries(self):
        while True:
            self.src.begin_header()
            m = self.tf.next()
            self.src.in_header = False
            if m is None:
                return
            kind = ("dir" if m.isdir() else "symlink" if m.issym() else "hardlink" if m.islnk()
                    else "file" if m.isreg() else "special")
            entry = Entry(m.name, kind, m.size, m.mode, m.mtime, m.linkname if kind in ("symlink", "hardlink") else None)
            if kind == "file":
                entry.open = lambda m=m: self.tf.extractfile(m)
            yield entry

    def close(self):
        self.tf.close()
        self.close_raw(self.comp)


class SingleReader:
    def __init__(self, fh, comp, name, size):
        self.fh, self.comp, self.name = fh, comp, name
        self.archive_size = size
        self.total = None

    def entries(self):
        yield Entry(self.name, "file", size=None, open=lambda: decompressor(self.comp, self.fh))

    def close(self):
        pass


def stem(archive):
    """The archive's name without its suffix (case-insensitive); "archive" when nothing is left."""
    lower = archive.lower()
    for suffix in SUFFIXES:
        if lower.endswith(suffix):
            archive = archive[:-len(suffix)]
            break
    return archive or "archive"


def single_name(archive):
    lower = archive.lower()
    for suffix in COMPRESSION_SUFFIXES:
        if lower.endswith(suffix):
            archive = archive[:-len(suffix)]
            break
    return archive if archive not in ("", ".", "..") else "archive"


def plain_name(name, what):
    if (not name or "/" in name or "\\" in name or "\0" in name or name in (".", "..")
            or len(os.fsencode(name)) > 255):
        raise BadInput(f"{what} must be a plain folder name (no slash, at most 255 bytes)")
    return name


def dest_name(archive, wanted, cwd):
    """The new folder's name: `wanted` (must not exist) or the archive's stem with the first free -2, -3 ... added."""
    if wanted:
        plain_name(wanted, "dir")
        if os.path.lexists(os.path.join(cwd, wanted)):
            raise BadInput(f"{wanted} already exists")
        return wanted
    base = stem(archive)
    if base in (".", "..") or "\0" in base:
        base = "archive"
    name, n = base, 1
    while os.path.lexists(os.path.join(cwd, name)):
        n += 1
        name = f"{base}-{n}"
    return plain_name(name, "the folder name")


# ---- inputs

def inside(real, root):
    try:
        return os.path.commonpath([real, root]) == root
    except ValueError:
        return False


def resolve_source(name, in_dir, work):
    """Opens the archive: inside the in folder (or the flow's work folder), a regular file, never through a
    final symlink or a FIFO. Returns a binary file object or raises BadInput."""
    base = in_dir or work
    if not base:
        raise BadInput("extract needs the in folder (the host sent no in_dir)")
    if "\0" in name or "\0" in base or (work and "\0" in work):
        raise BadInput("file must be a file name without NUL characters")
    real = os.path.realpath(os.path.join(base, name))
    roots = [os.path.realpath(r) for r in (in_dir, work) if r]
    if not any(inside(real, r) for r in roots):
        raise BadInput("file must be inside the in folder")
    try:
        # O_NONBLOCK: opening a FIFO must not wait for a writer; O_NOFOLLOW: no symlink swapped in after the check
        fd = os.open(real, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except OSError as e:
        raise BadInput(f"cannot read {name}: {e.strerror or e}") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise BadInput(f"{name} is not a file in the in folder")
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def env_limit(var, default):
    raw = os.environ.get(var)
    if raw is None or raw == "":
        return default
    if not re.fullmatch(r"[0-9]+", raw) or int(raw) < 1:
        raise BadInput(f"{var} must be a positive integer, not {raw[:30]!r}")
    return int(raw)


# ---- progress

class Progress:
    def __init__(self, total):
        self.total = total
        self.done = 0
        self.last = 0.0

    def update(self, n):
        self.done += n
        now = time.monotonic()
        if now - self.last >= BAR_EVERY:
            self.last = now
            bar(self.percent())

    def percent(self):
        if not self.total:
            return None
        return min(99, int(self.done * 100 / self.total))


def human(n):
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


# ---- the command

def extract(inputs, in_dir, work, cwd):
    file, dirname, listing, only = (inputs.get(k) for k in ("file", "dir", "list", "only"))
    if not isinstance(file, str) or not file:
        raise Failure("extract needs the input file", -32602)
    if not isinstance(dirname, str | None) or not isinstance(only, str | None) or not isinstance(listing, bool | None):
        raise Failure("dir and only must be strings and list a boolean", -32602)
    if any(v is not None and not isinstance(v, str) for v in (in_dir, work)):
        raise Failure("in_dir and work must be strings", -32602)
    try:
        limits = Limits(env_limit("DOCK_EXTRACT_MAX_BYTES", DEFAULT_MAX_BYTES),
                        env_limit("DOCK_EXTRACT_MAX_ENTRIES", DEFAULT_MAX_ENTRIES))
        patterns = [p for p in (item.strip() for item in (only or "").split(",")) if p]
        cwd = os.path.abspath(cwd)
        with resolve_source(file, in_dir, work) as fh:
            archive = os.path.basename(file.rstrip("/")) or "archive"
            kind, comp = sniff(fh, archive)
            if listing:
                return list_archive(fh, kind, comp, archive, limits, patterns)
            dest = dest_name(archive, dirname, cwd)
            password = zip_password(fh, archive, limits, patterns) if kind == "zip" else None
            return unpack(fh, kind, comp, archive, dest, cwd, limits, patterns, password)
    except BadInput as e:
        say("error", str(e))
        return {"exit_code": 1}
    except Damaged as e:
        say("error", f"archive is damaged: {e}")
        return {"exit_code": 1}
    except DAMAGE as e:
        say("error", f"archive is damaged: {e or type(e).__name__}")
        return {"exit_code": 1}


def matcher(patterns):
    """Whether a normalised name is kept: every name when there are no patterns, else any glob matches."""
    if not patterns:
        return lambda name: True
    return lambda name: any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)


def check_declared(total, limits):
    if total and total > limits.max_bytes:
        raise BadInput(f"the archive unpacks to {total} bytes (limit DOCK_EXTRACT_MAX_BYTES {limits.max_bytes})")


def zip_password(fh, archive, limits, patterns):
    """The password (bytes) for a zip with ZipCrypto entries that would be extracted, asked of the host once; None when
    no such entry exists. AES anywhere refuses the run before any question, and so do the declared-size and --only
    checks that would refuse it anyway."""
    fh.seek(0)
    with zipfile.ZipFile(fh) as zf:
        infos = zf.infolist()
        if len(infos) > limits.max_entries:
            raise BadInput(too_many_entries(limits))
        encrypted = False
        total = 0
        for info in infos:
            stop_if_cancelled()
            if info.compress_type == 99:
                raise BadInput("AES-encrypted zips are not supported")
            encrypted = encrypted or (bool(info.flag_bits & 0x1) and not info.is_dir())
            if not info.is_dir():
                total += info.file_size
        if not encrypted:
            return None
        keep, matched, needed = matcher(patterns), False, False
        for info in infos:  # a hostile name fails the run later anyway; say so before asking
            stop_if_cancelled()
            name = check_name(info.orig_filename)
            if name and keep(name):
                matched = True
                needed = needed or (bool(info.flag_bits & 0x1) and zip_kind(info) != "dir")
    if not needed:
        return None
    check_declared(total, limits)
    if patterns and not matched:
        raise BadInput("no entry matches --only")
    result = ask("form", title=f"Password for {archive}", fields=[
        {"name": "password", "label": "Password", "type": "text", "secret": True}])
    values = result.get("values")
    password = values.get("password") if isinstance(values, dict) else None
    if not isinstance(password, str) or not password:  # eof, an empty answer or an odd one
        raise BadInput("no password given")
    try:
        return password.encode()
    except UnicodeEncodeError:  # a lone surrogate: its text must not reach the error
        raise BadInput("no password given") from None


def build_reader(kind, fh, comp, archive, size, limits, password=None):
    try:  # opening reads the archive's start (zip: the directory; tar: the first header)
        if kind == "zip":
            return ZipReader(fh, limits, stop_if_cancelled, password)
        if kind == "tar":
            return TarReader(fh, comp, size, limits, stop_if_cancelled)
        return SingleReader(fh, comp, single_name(archive), size)
    except DAMAGE as e:
        raise Damaged(e) from None


def guarded(entries):
    """The reader's entries, with a damaged archive's errors told as Damaged."""
    it = iter(entries)
    while True:
        try:
            entry = next(it)
        except StopIteration:
            return
        except DAMAGE as e:
            raise Damaged(e) from None
        yield entry


def list_archive(fh, kind, comp, archive, limits, patterns):
    """One table of the entries (the matching ones with --only); nothing is written and no password is asked."""
    size = os.fstat(fh.fileno()).st_size
    fh.seek(0)
    keep, checker, rows, total = matcher(patterns), Checker(limits), [], 0
    reader = build_reader(kind, fh, comp, archive, size, limits)
    try:
        for entry in guarded(reader.entries()):
            stop_if_cancelled()
            checker.tick()
            try:
                name = check_name(entry.name)
            except BadInput as e:
                name, note = entry.name, f"refused: {e}"
            else:
                note = ""
            if name == "" and entry.kind == "dir":
                continue  # the archive root
            wanted = keep(name)
            try:
                if not note:
                    checker.check(entry, wanted)
            except BadInput as e:
                note = f"refused: {e}"
            if not wanted:
                continue
            total += 1
            if len(rows) < MAX_ROWS:
                kind_name = {"dir": "dir", "symlink": "link", "hardlink": "link"}.get(entry.kind, "file")
                rows.append([name[:MAX_NAME], "" if entry.size is None else entry.size, kind_name,
                             note or ("encrypted" if entry.encrypted else "")])
    finally:
        reader.close()
    if patterns and not total:
        raise BadInput("no entry matches --only")
    notify("table", id="entries", title=archive, columns=["name", "size", "type", "note"], rows=rows)
    if total > len(rows):
        say("info", f"… {total - len(rows)} more entries not shown")
    return {"exit_code": 0, "outputs": {"entries": total}}


def unpack(fh, kind, comp, archive, dest, cwd, limits, patterns=(), password=None):
    size = os.fstat(fh.fileno()).st_size
    fh.seek(0)
    try:
        temp = tempfile.mkdtemp(prefix=".extract-", suffix=".part", dir=cwd)  # mode 0o700
    except OSError as e:
        raise write_error(e) from None
    reader = ex = None
    renamed = False
    try:
        reader = build_reader(kind, fh, comp, archive, size, limits, password)
        check_declared(reader.total, limits)
        progress = Progress(reader.total)
        ex = Extractor(temp, limits, progress.update, stop_if_cancelled, reader.archive_size, matcher(patterns))
        bar(progress.percent())
        kept = 0
        for entry in guarded(reader.entries()):
            kept += ex.add(entry)
        if patterns and not kept:
            raise BadInput("no entry matches --only")
        files = ex.finish()
        stop_if_cancelled()
        target = os.path.join(cwd, dest)
        if os.path.lexists(target):
            raise BadInput(f"{dest} already exists")
        try:
            os.rename(temp, target)  # only now does the destination appear
        except OSError as e:
            raise write_error(e, dest) from None
        renamed = True
    finally:
        if ex:
            ex.close()
        if reader:
            reader.close()
        if not renamed:
            shutil.rmtree(temp, ignore_errors=True)  # only the folder this run made
    paths = [os.path.join(target, *p) for p in files[:MAX_FILES_LISTED]]
    bar(100, done=True)
    links = len(ex.links)
    said = f"{len(files)} file{'s' if len(files) != 1 else ''}" +(f" and {links} link{'s' if links != 1 else ''}" if links else "")
    say("info", f"extracted {said} ({human(ex.written)}) into {dest}/")
    return {"exit_code": 0, "outputs": {"dir": target, "files": paths, "truncated": len(files) > MAX_FILES_LISTED}}


def invoke(params):
    if params.get("command") != "extract":
        raise Failure(f"unknown command {params.get('command')}", -32601)
    inputs = params.get("inputs") or {}
    cwd = params.get("cwd") or os.getcwd()
    if not isinstance(inputs, dict) or not isinstance(cwd, str):
        raise Failure("inputs must be an object and cwd a string", -32602)
    return extract(inputs, params.get("in_dir"), params.get("work"), cwd)


def handle(msg):
    global pending
    method, msg_id, params = msg.get("method"), msg.get("id"), msg.get("params")
    if method is None or msg_id is None:
        return False
    counted = method == "invoke"  # the reader counted this invoke as pending
    try:
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise Failure("params must be an object", -32602)
        if method == "initialize":
            result = {"name": "extract"}
        elif method == "invoke":
            result = invoke(params)
        elif method == "shutdown":
            send({"id": msg_id, "result": {}})
            return True
        else:
            raise Failure(f"unknown method {method}", -32601)
        send({"id": msg_id, "result": result})
    except Cancelled:
        send({"id": msg_id, "error": {"code": -32800, "message": "cancelled"}})
    except Failure as e:
        send({"id": msg_id, "error": {"code": e.code, "message": str(e)}})
    except Exception as e:  # the plugin must always answer
        say("error", f"extract failed: {e!r}")
        send({"id": msg_id, "error": {"code": -32603, "message": f"internal error: {e}"}})
    finally:
        if counted:
            with state_lock:
                pending -= 1
                cancelled.clear()
    return False


def read_stdin():
    global pending
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(msg, dict):
            continue
        if "method" not in msg and isinstance(msg.get("id"), str) and ("result" in msg or "error" in msg):
            with state_lock:  # the answer to one of our requests; one nobody waits for (any more) is dropped
                box = replies.get(msg["id"])
            if box:
                box.put(msg)
            continue
        if msg.get("method") == "cancel":
            with state_lock:
                if pending:  # a cancel with nothing pending or running is ignored
                    cancelled.set()  # seen at once, even while a file is being written
            continue
        if msg.get("method") == "invoke" and msg.get("id") is not None:
            with state_lock:
                pending += 1
        inbox.put(msg)
    with state_lock:
        if pending:  # stdin closed during an invoke: stop
            cancelled.set()
    inbox.put(None)


def main():
    threading.Thread(target=read_stdin, daemon=True).start()
    while True:
        message = inbox.get()
        if message is None or handle(message):
            break


if __name__ == "__main__":
    main()
