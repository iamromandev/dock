"""Turning torrent paths into safe paths relative to the downloaded item."""

import unicodedata
from dataclasses import dataclass

from ..names import safe_name
from .metainfo import MAX_FILES, TorrentError

MAX_DEPTH = 32


@dataclass(frozen=True)
class FileSpec:
    parts: tuple  # cleaned; relative to the item root (single file: (name,))
    length: int
    offset: int


def _unsafe():
    return TorrentError("invalid torrent: unsafe path")


def _clean(raw):
    """Clean one path part; traversal and separators are refused, never rewritten."""
    if raw in ("", ".", "..") or any(c in raw for c in "/\\\x00"):
        raise _unsafe()
    # safe_name also strips leading dots (".hidden" -> "hidden", "..." -> "download"):
    # intentional, it keeps downloads from being hidden files or odd dot-only names.
    return safe_name(raw)


def _key(parts):
    """Compare names the way the strictest filesystems do: case-folded, Unicode NFC."""
    return tuple(unicodedata.normalize("NFC", unicodedata.normalize("NFC", p).casefold())
                 for p in parts)


def item_name(meta):
    """The cleaned name of the file or folder that ends up in the target folder."""
    return _clean(meta.name)


def plan_files(meta):
    name = item_name(meta)
    if not meta.multi:
        return (FileSpec((name,), meta.files[0].length, 0),)
    if len(meta.files) > MAX_FILES:
        raise TorrentError("invalid torrent: too many files")
    specs = []
    seen = set()
    offset = 0
    for entry in meta.files:
        if len(entry.path) > MAX_DEPTH:
            raise TorrentError("invalid torrent: path too deep")
        parts = tuple(_clean(p) for p in entry.path)
        key = _key(parts)
        if key in seen:
            raise TorrentError("invalid torrent: conflicting paths")
        seen.add(key)
        specs.append(FileSpec(parts, entry.length, offset))
        offset += entry.length
    for parts in seen:
        if any(parts[:i] in seen for i in range(1, len(parts))):
            raise TorrentError("invalid torrent: conflicting paths")
    return tuple(specs)
