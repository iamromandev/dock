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
SIDE = ".dm-side"  # inside the partial: the bytes of unpicked files that share a needed piece (layout._clean
# strips leading dots from every torrent path part, so no torrent file can be named like it)

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
    def __init__(self, root, files, piece_length, total, single, wanted=None):
        self.root = root
        self.files = tuple(files)
        # indexes of the picked files; None when every file is (a single-file torrent always is)
        self._wanted = None if wanted is None or single or len(set(wanted)) >= len(self.files) \
            else frozenset(wanted)
        self.piece_length = piece_length
        self.total = total
        self.single = single
        self._base = None  # dir fd: the root folder, or the parent of a single-file root
        self._cache = collections.OrderedDict()  # file index -> fd, least recently used first
        self._offsets = [f.offset for f in self.files]
        self._found = {}  # file index -> in the side area? for the files open_existing() saw
        # unpicked files whose side file this Storage made or found: gone later means lost data
        self._made = set()
        self._closed = False
        self._lock = threading.Lock()

    @property
    def wanted(self):
        """The indexes of the picked files, or None when every file is picked."""
        return self._wanted

    def _picked(self, i):
        return self._wanted is None or i in self._wanted

    def _parts(self, i):
        if self.single:
            return (os.path.basename(self.root),)
        return self.files[i].parts if self._picked(i) else (SIDE,) + self.files[i].parts

    def _parent(self, parts, made=None, create=False):
        """Dir fd of the folder holding parts[-1], walking (and optionally creating) folders
        one component at a time. The caller closes it unless it is the base fd. `made` creates each folder
        once (a new partial); `create` makes the missing ones (the side area, whose folders may be there)."""
        fd = self._base
        try:
            for i in range(len(parts) - 1):
                if made is not None and parts[:i + 1] not in made:
                    os.mkdir(parts[i], 0o777, dir_fd=fd)
                    made.add(parts[:i + 1])
                elif create:
                    try:
                        os.mkdir(parts[i], 0o777, dir_fd=fd)
                    except FileExistsError:  # a link there fails the no-follow open below
                        pass
                nxt = os.open(parts[i], _DIR, dir_fd=fd)
                if fd != self._base:
                    os.close(fd)
                fd = nxt
        except BaseException:
            if fd != self._base:
                os.close(fd)
            raise
        return fd

    def _open(self, i, flags, made=None, create=False):
        parts = self._parts(i)
        parent = self._parent(parts, made, create)
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
                    if not self._picked(i):
                        continue  # never created: its bytes go to the side area, when a piece needs them
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
        """Reuse a partial left by an earlier run, whatever it had picked: nothing is created or moved here
        and nothing is followed through a link.

        Each file is at its real path or in the side area (never both) or nowhere (not written yet, or removed
        by an earlier placement that was cut short); the root holds nothing else but the side area. Every
        file there is a regular file of the right size. Anything else raises LayoutMismatch; reconcile() then
        brings the files to where the current selection wants them.
        """
        name = os.path.basename(self.root)
        try:
            try:
                if self.single:
                    self._base = os.open(os.path.dirname(self.root) or ".", _DIR & ~_NOFOLLOW)
                    fd = self._open(0, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
                    try:
                        st = os.fstat(fd)
                    finally:
                        os.close(fd)
                    if not stat.S_ISREG(st.st_mode) or st.st_size != self.files[0].length or st.st_nlink != 1:
                        raise LayoutMismatch(name)
                    self._found = {0: False}
                else:
                    self._base = os.open(self.root, _DIR)
                    self._found = self._scan()
            except OSError as e:
                if e.errno in _NOT_OURS:  # a missing or swapped entry: not what we made
                    raise LayoutMismatch(name) from e
                raise  # EMFILE, EIO, EACCES...: the data may be fine, the caller keeps it
        except BaseException:
            self.close()
            raise

    def _scan(self):
        """{file index: in the side area?} for every file present; LayoutMismatch for anything the layout
        does not name, a file in both places, or an entry of the wrong kind, size or link count."""
        name = os.path.basename(self.root)
        tree = {}
        for i, f in enumerate(self.files):
            node = tree
            for part in f.parts[:-1]:
                node = node.setdefault(part, {})
            node[f.parts[-1]] = i
        found = {}

        def walk(fd, node, side, top=False):
            with os.scandir(fd) as entries:
                listed = list(entries)
            for entry in listed:
                want = node.get(entry.name)
                if top and entry.name == SIDE:  # only at the root, and a real folder
                    if not entry.is_dir(follow_symlinks=False):
                        raise LayoutMismatch(name)
                    child = os.open(SIDE, _DIR, dir_fd=fd)
                    try:
                        walk(child, tree, True)
                    finally:
                        os.close(child)
                    continue
                if want is None:
                    raise LayoutMismatch(name)
                if isinstance(want, dict):
                    if not entry.is_dir(follow_symlinks=False):
                        raise LayoutMismatch(name)
                    child = os.open(entry.name, _DIR, dir_fd=fd)
                    try:
                        walk(child, want, side)
                    finally:
                        os.close(child)
                    continue
                st = entry.stat(follow_symlinks=False)
                if not stat.S_ISREG(st.st_mode) or st.st_size != self.files[want].length or st.st_nlink != 1 \
                        or want in found:
                    raise LayoutMismatch(name)
                found[want] = side

        walk(self._base, tree, False, True)
        return found

    def reconcile(self):
        """After open_existing(): bring every file to where this selection wants it, by rename inside the
        partial. A picked file found in the side area goes to its real path, an unpicked one found at its real
        path goes to the side area, a picked one found nowhere is created (sparse); an unpicked one found
        nowhere stays absent. Folders are walked and made one component at a time, never through a link."""
        name = os.path.basename(self.root)
        if self.single:
            return
        try:
            for i, f in enumerate(self.files):
                where = self._found.get(i)
                picked = self._picked(i)
                if where is None:
                    if picked:
                        fd = self._open(i, os.O_RDWR | os.O_CREAT | os.O_EXCL, create=True)
                        try:
                            os.ftruncate(fd, f.length)
                        finally:
                            os.close(fd)
                    continue
                if where == picked:  # in the side area but picked, or at its real path but not
                    real, side = f.parts, (SIDE,) + f.parts
                    self._move(*((side, real) if picked else (real, side)))
                if not picked:
                    self._made.add(i)  # its side file exists now: gone later means lost data
        except OSError as e:
            if e.errno in _NOT_OURS:
                raise LayoutMismatch(name) from e
            raise

    def _move(self, src, dst):
        """Rename the file at parts `src` to parts `dst`; the folders of dst are made as needed."""
        sp = self._parent(src)
        try:
            dp = self._parent(dst, create=True)
            try:
                os.rename(src[-1], dst[-1], src_dir_fd=sp, dst_dir_fd=dp)
            finally:
                if dp != self._base:
                    os.close(dp)
        finally:
            if sp != self._base:
                os.close(sp)

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

    def _create_side(self, i):
        """The side file of unpicked file i, made on first use: exclusive, no links followed, sparse."""
        fd = self._open(i, os.O_RDWR | os.O_CREAT | os.O_EXCL, create=True)
        try:
            os.ftruncate(fd, self.files[i].length)
        except BaseException:
            os.close(fd)
            raise
        return fd

    def _fd(self, i, create=False):
        """Cached descriptor for file i (lock held); reopened without O_CREAT if evicted. An unpicked file
        has none until a write needs it (`create`): reading one that was never written is
        FileNotFoundError."""
        fd = self._cache.get(i)
        if fd is not None:
            self._cache.move_to_end(i)
            return fd
        try:
            fd = self._open(i, os.O_RDWR)
        except FileNotFoundError:
            if not create or self._picked(i) or i in self._made:
                raise  # a vanished file is an error, never recreated as zeros
            fd = self._create_side(i)
            self._made.add(i)
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
                fd = self._fd(i, create=True)
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

    def finish_layout(self):
        """Before the partial is placed: drop the side area and every folder that holds no picked file.

        Everything is reached through directory descriptors without following links, so a link planted
        in the partial is removed itself, never what it points to."""
        with self._lock:
            self._check_open()
            if self.single:
                return
            _remove(self._base, SIDE)
            if self._wanted is None:
                return
            keep_files = {f.parts for i, f in enumerate(self.files) if self._picked(i)}
            keep_dirs = {f[:k] for f in keep_files for k in range(len(f))}  # the root () included
            self._prune(self._base, (), keep_files, keep_dirs, sorted(f.parts for f in self.files))

    def _prune(self, fd, folder, keep_files, keep_dirs, order):
        """One walk of the folder behind dir fd `fd`, never following a link. A folder is entered, then
        removed when it holds no picked file (rmdir only succeeds on an empty one). An entry that is not a
        folder is unlinked when it is a link, or sits where the layout has a folder (`order`: every file's
        parts, sorted); a picked file and anything else stays."""
        with os.scandir(fd) as entries:
            found = [(e.name, e.is_dir(follow_symlinks=False), e.is_symlink()) for e in entries]
        for name, is_dir, is_link in found:
            path = folder + (name,)
            if not is_dir:
                if path not in keep_files and (is_link or _is_folder_path(order, path)):
                    os.unlink(name, dir_fd=fd)
                continue
            child = os.open(name, _DIR, dir_fd=fd)
            try:
                self._prune(child, path, keep_files, keep_dirs, order)
            finally:
                os.close(child)
            if path not in keep_dirs:
                try:
                    os.rmdir(name, dir_fd=fd)
                except OSError:
                    pass

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


def _is_folder_path(order, path):
    """Whether some file of the sorted `order` lies below `path`."""
    i = bisect.bisect_left(order, path)
    while i < len(order) and order[i][:len(path)] == path:
        if len(order[i]) > len(path):
            return True
        i += 1
    return False


def _remove(parent, name):
    """Delete `name` under the dir fd `parent` with everything in it. A link is unlinked, never followed;
    a missing entry is fine."""
    try:
        fd = os.open(name, _DIR, dir_fd=parent)
    except FileNotFoundError:
        return
    except OSError:  # a link, a file or anything but a folder: remove that entry itself
        os.unlink(name, dir_fd=parent)
        return
    try:
        with os.scandir(fd) as entries:
            found = [(e.name, e.is_dir(follow_symlinks=False)) for e in entries]
        for child, is_dir in found:
            if is_dir:
                _remove(fd, child)
            else:
                os.unlink(child, dir_fd=fd)
    finally:
        os.close(fd)
    os.rmdir(name, dir_fd=parent)
