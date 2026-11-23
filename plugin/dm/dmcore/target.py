"""Where the download goes: -o / -d resolved against the current folder."""

import os
from dataclasses import dataclass
from pathlib import Path

from .names import MAX_BYTES


class DmError(Exception):
    """A user-facing failure; the message is the text after 'dm: '."""

    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Target:
    folder: Path
    fixed_name: str | None


def _inside(cwd_real, path_real):
    try:
        return os.path.commonpath([cwd_real, path_real]) == cwd_real
    except ValueError:
        return False


def _folder(cwd_real, cwd, given):
    real = os.path.realpath(os.path.join(cwd, given))
    if not _inside(cwd_real, real):
        raise DmError(f"{given} is outside the current folder")
    if not os.path.isdir(real):
        raise DmError(f"folder {given} does not exist")
    return Target(Path(real), None)


def _file_name(name):
    """The user's own name, kept as given: rejected rather than renamed."""
    bad = (name in ("", ".", "..") or len(name.encode("utf-8", "surrogateescape")) > MAX_BYTES
           or any(ord(c) < 32 or ord(c) == 127 for c in name))
    if bad:
        raise DmError(f"invalid file name '{name}'")
    return name


def resolve(cwd, output, dir):
    if output is not None and dir is not None:
        raise DmError("use -o or -d, not both", 2)
    cwd_real = os.path.realpath(cwd)
    if dir is not None:
        if dir == "":
            raise DmError("invalid folder ''")
        return _folder(cwd_real, cwd, dir)
    if output is None:
        return Target(Path(cwd_real), None)
    if os.path.basename(output) in (".", ".."):
        _file_name(os.path.basename(output))
    if output == "" or "\x00" in output:
        _file_name(output)
    cand = os.path.join(cwd, output)
    if os.path.isdir(cand):
        return _folder(cwd_real, cwd, output)
    if not _inside(cwd_real, os.path.realpath(cand)):
        raise DmError(f"{output} is outside the current folder")
    parent_given = os.path.dirname(output) or "."
    folder = _folder(cwd_real, cwd, parent_given).folder
    return Target(folder, _file_name(os.path.basename(cand)))
