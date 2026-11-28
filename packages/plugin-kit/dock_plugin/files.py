"""Safe inputs and outputs: find a source file inside the allowed folders, check output names, write results
without ever replacing a file."""

import os
import stat

from .cancel import Cancelled, Failure


def inside(real, root):
    try:
        return os.path.commonpath([real, root]) == root
    except ValueError:
        return False


def resolve_source(name, in_dir, work, who="plugin"):
    """The absolute path of the input file: inside the in folder (or the flow's work folder), a regular file,
    never through a final symlink that leaves them or a FIFO. Raises Failure. `who` names the plugin."""
    base = in_dir or work
    if not base:
        raise Failure(f"{who} needs the in folder (the host sent no in_dir)")
    if "\0" in name or "\0" in base or (work and "\0" in work):
        raise Failure("file must be a file name without NUL characters")
    real = os.path.realpath(os.path.join(base, name))
    roots = [os.path.realpath(r) for r in (in_dir, work) if r]
    if not any(inside(real, r) for r in roots):
        raise Failure("file must be inside the in folder")
    try:
        # O_NONBLOCK: opening a FIFO must not wait for a writer; O_NOFOLLOW: no symlink swapped in after the check
        fd = os.open(real, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except OSError as e:
        raise Failure(f"cannot read {name}: {e.strerror or e}") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Failure(f"{name} is not a file in the in folder")
    finally:
        os.close(fd)
    return real


def plain_name(name, what, max_bytes=200):
    """`name` if it is a plain file name (no slash, not . or .., no control characters, at most max_bytes bytes),
    else Failure. `what` names the input in the message, e.g. "output"."""
    if "/" in name or "\\" in name or name in (".", "..") or any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise Failure(f"{what} must be a plain name without a folder (no slash)")
    if len(name.encode()) > max_bytes:
        raise Failure(f"{what} name is too long")
    return name


class Outputs:
    """Writes each file as .name.part, links it into place (never replacing a file) and removes the .part.
    `discard` removes the parts and the files this run linked, and nothing else. As a context manager it
    discards when the block raises."""

    def __init__(self):
        self.published: list[str] = []
        self.parts: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self.discard()
        return False

    @staticmethod
    def check(targets):
        """Raises Failure if any target path already exists, before any work is done."""
        for path in targets:
            if os.path.lexists(path):
                raise Failure(f"{os.path.basename(path)} already exists")

    def publish(self, files, call):
        with call.lock:  # a cancel (stop()) waits here, so it never lands halfway
            if call.cancelled():
                raise Cancelled()
            try:
                for path, text in files:
                    self.one(path, text)
            except BaseException:
                self.discard()
                raise

    def one(self, path, text):
        name = os.path.basename(path)
        part = os.path.join(os.path.dirname(path), f".{name}.part")
        try:
            fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            raise Failure(f".{name}.part already exists") from None
        except OSError as e:
            raise Failure(f"cannot write {name}: {e.strerror or e}") from None
        self.parts.append(part)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(text.encode("utf-8"))
            os.link(part, path)
        except FileExistsError:
            raise Failure(f"{name} already exists") from None
        except OSError as e:
            raise Failure(f"cannot write {name}: {e.strerror or e}") from None
        self.published.append(path)
        os.unlink(part)
        self.parts.remove(part)

    def discard(self):
        for path in self.parts + self.published:
            try:
                os.unlink(path)
            except OSError:
                pass
        self.parts.clear()
        self.published.clear()
