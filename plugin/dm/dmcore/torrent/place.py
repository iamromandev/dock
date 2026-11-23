"""Moving a finished download into the target folder without replacing anything."""

import errno
import os

from ..names import candidates
from ..target import DmError

# link() is unsupported on some filesystems; fall back to exclusive create + replace
_NO_LINK = {errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EXDEV}


def _drop_placeholder(path, made):
    """Remove the empty file we created, unless someone replaced or filled it."""
    try:
        now = os.lstat(path)
        if (now.st_dev, now.st_ino) == (made.st_dev, made.st_ino) and now.st_size == 0:
            os.unlink(path)
    except OSError:
        pass


def _drop_dir(path):
    try:
        os.rmdir(path)  # only succeeds while it is still empty
    except OSError:
        pass


def place_file(tmp, folder, base):
    """Move tmp to the first free name; never replaces an existing file."""
    for name in candidates(base):
        final = folder / name
        try:
            os.link(tmp, final)  # atomic, fails if the name exists
        except FileExistsError:
            continue
        except OSError as e:
            if e.errno not in _NO_LINK:
                raise
            # No hard links here: reserve the name with an exclusive create, then replace
            # it. This briefly shows an empty file, only on filesystems without hard links.
            try:
                fd = os.open(final, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
            except FileExistsError:
                continue
            try:
                made = os.fstat(fd)
            finally:
                os.close(fd)
            try:
                os.replace(tmp, final)
            except BaseException:
                _drop_placeholder(final, made)
                raise
            return final
        os.unlink(tmp)
        return final
    raise DmError(f"no free name for '{base}'")


def place_dir(tmpdir, folder, base):
    """Move a folder to the first free name; never replaces or fills an existing entry.

    The name is reserved with mkdir (fails on any existing entry, symlinks included), then
    the rename replaces only our own empty placeholder: if someone put something in it
    meanwhile, the rename fails and their folder is left alone.
    """
    for name in candidates(base, is_dir=True):
        final = folder / name
        try:
            os.mkdir(final)
        except FileExistsError:
            continue
        try:
            os.rename(tmpdir, final)
        except OSError as e:
            if e.errno in (errno.ENOTEMPTY, errno.EEXIST):
                continue
            _drop_dir(final)
            raise
        except BaseException:  # e.g. KeyboardInterrupt between mkdir and rename
            _drop_dir(final)
            raise
        return final
    raise DmError(f"no free name for '{base}'")
