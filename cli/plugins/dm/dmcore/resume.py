"""Resuming an interrupted download: keys, partial names, the resume record, the lock.

A partial is found again by its name alone: `.<name>.<key>.dm-part` where the key is a hash of what makes
the download the same one (URL or info-hash, target folder, -o name). The record next to an HTTP partial
holds only the total length and the server's validator; the partial's own size is the progress. Nothing
here follows a symlink, and nothing read from disk is trusted before it is checked.
"""

import errno
import fcntl
import hashlib
import json
import logging
import os
import secrets
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .names import _cut
from .target import DmError

KEY_HEX = 24
RECORD_MAX = 4096
HEADER_MAX = 256
SUFFIX_PART, SUFFIX_RECORD, SUFFIX_LOCK = ".dm-part", ".dm-resume", ".dm-lock"
SUFFIX_META = ".dm-meta"
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_NO_LOCKING = {errno.ENOLCK, errno.EOPNOTSUPP, errno.EINVAL}
_QUOTA = {errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)}
log = logging.getLogger("dm")


def _digest(*parts):
    return hashlib.sha256(b"\0".join(parts)).hexdigest()[:KEY_HEX]


def _bytes(text):
    return text.encode("utf-8", "surrogateescape")


def http_key(url, target):
    """The same URL (without userinfo and fragment) into the same folder under the same -o name."""
    parts = urlsplit(url)
    bare = parts._replace(netloc=parts.netloc.rpartition("@")[2], fragment="").geturl()
    return _digest(b"http", _bytes(bare), _bytes(str(target.folder)), _bytes(target.fixed_name or ""))


def torrent_key(info_hash, target):
    return _digest(b"torrent", info_hash, _bytes(str(target.folder)), _bytes(target.fixed_name or ""))


@dataclass(frozen=True)
class Paths:
    part: Path
    record: Path
    lock: Path


def paths(folder, display, key):
    stem = f".{_cut(display, 100)}.{key}"
    return Paths(folder / (stem + SUFFIX_PART), folder / (stem + SUFFIX_RECORD), folder / (stem + SUFFIX_LOCK))


def torrent_lock(folder, key):
    """A torrent's lock is named by the key alone: a magnet link does not know the item's name yet."""
    return folder / f".dm.{key}{SUFFIX_LOCK}"


def meta_path(folder, key):
    """The stored metadata of a magnet download, named by the key alone like the lock."""
    return folder / f".dm.{key}{SUFFIX_META}"


def is_quota_error(exc):
    return isinstance(exc, OSError) and exc.errno in _QUOTA


# ---- the record -------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Record:
    total: int  # > 0
    etag: str | None  # strong only, never "W/..."
    last_modified: str | None  # at least one of etag / last_modified is set

    def validator(self):
        return self.etag or self.last_modified


def _header_ok(value):
    return (isinstance(value, str) and 0 < len(value) <= HEADER_MAX
            and all(" " <= c <= "~" for c in value))


def _strong(value):
    """A header value usable as a validator, else None."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if _header_ok(value) and not value.startswith("W/") else None


def _make_record(total, etag, last_modified):
    if isinstance(total, bool) or not isinstance(total, int) or total <= 0:
        return None
    etag = None if etag is None else _strong(etag) or False
    last_modified = None if last_modified is None else _strong(last_modified) or False
    if etag is False or last_modified is False or (etag is None and last_modified is None):
        return None
    return Record(total, etag, last_modified)


def record_from_headers(headers, length):
    """The record for a response, or None when it cannot be resumed (no length or no strong validator)."""
    if length is None or length <= 0:
        return None
    return _make_record(length, _strong(headers.get("ETag")), _strong(headers.get("Last-Modified")))


def read_record(path, key):
    """The valid record at path for this key, else None. Never raises."""
    try:
        fd = os.open(path, os.O_RDONLY | _NOFOLLOW | _NONBLOCK)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        raw = os.read(fd, RECORD_MAX + 1)
        if len(raw) > RECORD_MAX:
            return None
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict) or data.get("v") != 1 or data.get("key") != key:
            return None
        return _make_record(data.get("total"), data.get("etag"), data.get("lm"))
    except Exception:  # whatever is in the file, it is just not a record
        return None
    finally:
        os.close(fd)


def write_record(path, key, rec):
    """Write the record atomically: a temp file next to it, then a replace."""
    text = json.dumps({"v": 1, "key": key, "total": rec.total, "etag": rec.etag, "lm": rec.last_modified})
    tmp = f"{path}.{secrets.token_hex(8)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o666)
    try:
        try:
            os.write(fd, text.encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---- stored metadata ---------------------------------------------------------------------------------------


def read_meta(path, info_hash, discard_bad=True):
    """The info dictionary stored at path when it is a regular file of at most MAX_TORRENT_BYTES whose SHA-1
    is info_hash; else None, and a file that is there but wrong is deleted unless `discard_bad` is False (a
    read-only caller). Never follows a link."""
    from .torrent.metainfo import MAX_TORRENT_BYTES

    try:
        fd = os.open(path, os.O_RDONLY | _NOFOLLOW | _NONBLOCK)
    except OSError as e:
        if e.errno == errno.ELOOP and discard_bad:  # a link in the way: remove the link itself
            discard(path)
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        raw = b""
        while len(raw) <= MAX_TORRENT_BYTES:
            chunk = os.read(fd, MAX_TORRENT_BYTES + 1 - len(raw))
            if not chunk:
                break
            raw += chunk
    except OSError:
        return None
    finally:
        os.close(fd)
    if len(raw) <= MAX_TORRENT_BYTES and hashlib.sha1(raw).digest() == info_hash:
        return raw
    if discard_bad:
        discard(path)
    return None


def write_meta(path, info_bytes):
    """Store the metadata atomically: a temp file next to it, then a replace."""
    tmp = f"{path}.{secrets.token_hex(8)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o666)
    try:
        try:
            done = 0
            while done < len(info_bytes):
                done += os.write(fd, info_bytes[done:])
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---- the lock ---------------------------------------------------------------------------------------------------


class Lock:
    """One writer per download key. flock on a lock file that is removed with the download."""

    def __init__(self, path):
        self.path = Path(path)
        self.fd = None

    def _fail(self, exc):
        return DmError(f"cannot write to {self.path.parent}: {exc.strerror or type(exc).__name__}")

    def acquire(self):
        for _ in range(3):
            try:
                fd = os.open(self.path, os.O_RDWR | os.O_CREAT | _NOFOLLOW | _NONBLOCK, 0o666)
            except OSError as e:
                if e.errno == errno.ELOOP:  # a symlink in the way: replace the link itself, never follow it
                    try:
                        os.unlink(self.path)
                    except OSError as e2:
                        raise self._fail(e2) from None
                    continue
                raise self._fail(e) from None
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                raise DmError("already being downloaded in this folder") from None
            except OSError as e:
                if e.errno not in _NO_LOCKING:
                    os.close(fd)
                    raise self._fail(e) from None
                log.debug("no file locking here")
                self.fd = fd
                return
            try:  # the holder before us may have removed the file after we opened it
                same = os.stat(self.path, follow_symlinks=False).st_ino == os.fstat(fd).st_ino
            except OSError:
                same = False
            if same:
                self.fd = fd
                return
            os.close(fd)
        raise DmError("already being downloaded in this folder")

    def release(self, remove):
        if self.fd is None:
            return
        if remove:  # while still holding the lock, so nobody locks a file that is about to vanish
            try:
                os.unlink(self.path)
            except OSError:
                pass
        os.close(self.fd)
        self.fd = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release(remove=True)


# ---- removing ---------------------------------------------------------------------------------------------------


def discard(*items):
    """Delete files, or a folder tree, without following symlinks. Missing items are fine, and so is a
    failure to delete: a cleanup problem must never replace the error that ended the download."""
    for item in items:
        try:
            st = os.lstat(item)
        except OSError:
            continue
        if stat.S_ISDIR(st.st_mode):
            shutil.rmtree(item, ignore_errors=True)
        else:
            try:
                os.unlink(item)
            except OSError:
                pass


def clean_temps(record):
    """Remove record temp files that a killed run left next to this download's record. Never follows a link."""
    prefix = record.name + "."
    try:
        names = os.listdir(record.parent)
    except OSError:
        return
    for name in names:
        if name.startswith(prefix) and name.endswith(".tmp"):
            path = record.parent / name
            try:
                if not stat.S_ISDIR(os.lstat(path).st_mode):
                    os.unlink(path)
            except OSError:
                pass


def exists(path):
    return os.path.lexists(path)
