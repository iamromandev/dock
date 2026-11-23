"""Sparse file storage for a torrent's pieces.

Files are created once in prepare() (O_EXCL, never following symlinks) and then reopened
on demand through a small LRU cache of descriptors. Every path component is opened relative
to a directory descriptor with O_NOFOLLOW, so a folder swapped for a symlink cannot redirect
a write outside the root, and a deleted temp folder is never re-created by a late write.
"""

import bisect
import collections
import errno
import os
import stat
import threading

from ..target import DmError

MAX_OPEN_FILES = 64

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
# what opening an entry says when it is missing or of the wrong kind (a link, a device, a folder, a file)
_NOT_OURS = {errno.ENOENT, errno.ELOOP, errno.ENOTDIR, errno.EISDIR, errno.ENXIO, errno.EMLINK}


class PrepareError(DmError):
    """Creating the files failed; `reason` is the system's, with no torrent-chosen names."""

    def __init__(self, name, reason):
        super().__init__(f"cannot create files in {name}: {reason}")
        self.reason = reason


class LayoutMismatch(PrepareError):
    """The existing partial is not exactly the layout of this torrent: it must not be trusted."""

    def __init__(self, name):
        super().__init__(name, "the partial does not match the torrent")


_DIR = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _NOFOLLOW | _CLOEXEC
class Storage:
    def __init__(self, root, files, piece_length, total, single):
        self.root = root
        self.files = tuple(files)
        self.piece_length = piece_length
        self.total = total
        self.single = single
        self._base = None  # dir fd: the root folder, or the parent of a single-file root
        self._cache = collections.OrderedDict()  # file index -> fd, least recently used first
        self._offsets = [f.offset for f in self.files]
        self._closed = False
        self._lock = threading.Lock()

    def _parts(self, i):
        return (os.path.basename(self.root),) if self.single else self.files[i].parts

    def _parent(self, parts, made=None):
        """Dir fd of the folder holding parts[-1], walking (and optionally creating) folders
        one component at a time. The caller closes it unless it is the base fd."""
        fd = self._base
        try:
            for i in range(len(parts) - 1):
                if made is not None and parts[:i + 1] not in made:
                    os.mkdir(parts[i], 0o777, dir_fd=fd)
                    made.add(parts[:i + 1])
                nxt = os.open(parts[i], _DIR, dir_fd=fd)
                if fd != self._base:
                    os.close(fd)
                fd = nxt
        except BaseException:
            if fd != self._base:
                os.close(fd)
            raise
        return fd

    def _open(self, i, flags, made=None):
        parts = self._parts(i)
        parent = self._parent(parts, made)
        try:
            return os.open(parts[-1], flags | _NOFOLLOW | _CLOEXEC, 0o666, dir_fd=parent)
        finally:
            if parent != self._base:
                os.close(parent)

    def prepare(self):
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
        try:
            try:
                if self.single:
                    self._base = os.open(os.path.dirname(self.root) or ".", _DIR & ~_NOFOLLOW)
                else:
                    self._base = os.open(self.root, _DIR)
                made = set()
                for i, f in enumerate(self.files):
                    fd = self._open(i, flags, made)
                    try:
                        os.ftruncate(fd, f.length)
                    finally:
                        os.close(fd)
            except OSError as e:  # the raw error carries full paths with torrent-chosen names
                raise PrepareError(os.path.basename(self.root), e.strerror or "error") from None
        except BaseException:
            self.close()
            raise

    def open_existing(self):
        """Reuse a partial made by prepare(): nothing is created, nothing followed through a link.

        The root, every folder and every file must be exactly what prepare() makes: regular files of the
        right size, no other entry anywhere. Anything else raises LayoutMismatch.
        """
        name = os.path.basename(self.root)
        try:
            try:
                if self.single:
                    self._base = os.open(os.path.dirname(self.root) or ".", _DIR & ~_NOFOLLOW)
                else:
                    self._base = os.open(self.root, _DIR)
                    self._check_entries()
                for i, f in enumerate(self.files):
                    fd = self._open(i, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
                    try:
                        st = os.fstat(fd)
                    finally:
                        os.close(fd)
                    if not stat.S_ISREG(st.st_mode) or st.st_size != f.length or st.st_nlink != 1:
                        raise LayoutMismatch(name)
            except OSError as e:
                if e.errno in _NOT_OURS:  # a missing or swapped entry: not what we made
                    raise LayoutMismatch(name) from e
                raise  # EMFILE, EIO, EACCES...: the data may be fine, the caller keeps it
        except BaseException:
            self.close()
            raise

    def _check_entries(self):
        """Every entry under the root is one the layout names, of the right kind (links are never one)."""
        expected = {(): set()}
        for f in self.files:
            for k in range(len(f.parts)):
                expected.setdefault(f.parts[:k], set()).add(f.parts[k])
        for folder, names in expected.items():
            fd = self._parent(folder + ("x",))
            try:
                with os.scandir(fd) as entries:
                    found = {e.name: e for e in entries}
            finally:
                if fd != self._base:
                    os.close(fd)
            if set(found) != names:
                raise LayoutMismatch(os.path.basename(self.root))
            for n, entry in found.items():
                is_folder = folder + (n,) in expected
                kind_ok = entry.is_dir(follow_symlinks=False) if is_folder else entry.is_file(follow_symlinks=False)
                if not kind_ok:
                    raise LayoutMismatch(os.path.basename(self.root))

    def piece_size(self, index):
        if not 0 <= index < -(-self.total // self.piece_length):
            raise ValueError("piece index out of range")
        return min(self.piece_length, self.total - index * self.piece_length)

    def _spans(self, start, length):
        """(file index, offset in file, count) for the byte range [start, start+length)."""
        i = max(bisect.bisect_right(self._offsets, start) - 1, 0)
        end = start + length
        while start < end and i < len(self.files):
            f = self.files[i]
            if start < f.offset + f.length:  # skips zero-length files
                n = min(end, f.offset + f.length) - start
                yield i, start - f.offset, n
                start += n
            i += 1

    def _fd(self, i):
        """Cached descriptor for file i (lock held); reopened without O_CREAT if evicted."""
        fd = self._cache.get(i)
        if fd is not None:
            self._cache.move_to_end(i)
            return fd
        fd = self._open(i, os.O_RDWR)
        self._cache[i] = fd
        while len(self._cache) > MAX_OPEN_FILES:
            _, old = self._cache.popitem(last=False)
            os.close(old)
        return fd

    def _check_open(self):
        if self._closed:
            raise OSError(errno.EBADF, "storage is closed")

    def write_piece(self, index, data):
        if len(data) != self.piece_size(index):
            raise ValueError("wrong piece size")
        view = memoryview(data)
        pos = 0
        with self._lock:
            self._check_open()
            for i, offset, n in self._spans(index * self.piece_length, len(data)):
                fd = self._fd(i)
                if os.fstat(fd).st_nlink == 0:  # the file was deleted under us
                    raise FileNotFoundError(errno.ENOENT, "file was removed")
                while n:
                    done = os.pwrite(fd, view[pos:pos + n], offset)
                    pos += done
                    offset += done
                    n -= done

    def read_block(self, index, begin, length):
        if begin < 0 or length < 0 or begin + length > self.piece_size(index):
            raise ValueError("block out of range")
        out = []
        with self._lock:
            self._check_open()
            for i, offset, n in self._spans(index * self.piece_length + begin, length):
                fd = self._fd(i)
                while n:
                    chunk = os.pread(fd, n, offset)
                    if not chunk:
                        raise OSError(errno.EIO, "unexpected end of file")
                    out.append(chunk)
                    offset += len(chunk)
                    n -= len(chunk)
        return b"".join(out)

    def close(self):
        with self._lock:
            self._closed = True
            fds = list(self._cache.values())
            self._cache.clear()
            if self._base is not None:
                fds.append(self._base)
                self._base = None
            for fd in fds:
                try:
                    os.close(fd)
                except OSError:
                    pass
