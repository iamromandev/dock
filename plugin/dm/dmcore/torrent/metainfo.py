"""Reading and validating a .torrent file (BitTorrent v1 metainfo)."""

import hashlib
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urlsplit

from ..target import DmError
from .bencode import BencodeError, decode, encode

MAX_TORRENT_BYTES = 10 * 1024 * 1024
MAX_PIECES = 2 ** 20
MAX_FILES = 100_000
MIN_PIECE_LENGTH = 16 * 1024
MAX_PIECE_LENGTH = 16 * 1024 * 1024
_SCHEMES = {"http", "https", "udp"}


class TorrentError(DmError):
    pass


@dataclass(frozen=True)
class FileEntry:
    path: tuple  # raw parts, decoded as UTF-8 with errors="replace"
    length: int


@dataclass(frozen=True)
class Meta:
    info_hash: bytes
    name: str
    piece_length: int
    hashes: tuple
    total: int
    files: tuple
    multi: bool
    trackers: tuple


def _invalid(reason):
    return TorrentError(f"invalid torrent: {reason}")


def _text(raw):
    return raw.decode("utf-8", errors="replace")


def _length(value, what):
    if not isinstance(value, int) or value < 0:
        raise _invalid(f"bad {what}")
    return value


def _files(info, name):
    if b"length" in info:
        return False, (FileEntry((name,), _length(info[b"length"], "length")),)
    raw = info.get(b"files")
    if not isinstance(raw, list) or not raw:
        raise _invalid("no length or files")
    if len(raw) > MAX_FILES:
        raise _invalid("too many files")
    out = []
    for item in raw:
        if not isinstance(item, dict):
            raise _invalid("bad file entry")
        path = item.get(b"path")
        if (not isinstance(path, list) or not path
                or not all(isinstance(p, bytes) for p in path)):
            raise _invalid("bad file path")
        out.append(FileEntry(tuple(_text(p) for p in path), _length(item.get(b"length"), "length")))
    return True, tuple(out)


def _trackers(top):
    tiers = []
    lists = top.get(b"announce-list")
    if isinstance(lists, list):
        for tier in lists:
            if isinstance(tier, list):
                urls = _urls(tier)
                if urls:
                    tiers.append(urls)
    if not tiers:
        urls = _urls([top.get(b"announce")])
        if urls:
            tiers.append(urls)
    return tuple(tiers)


def _urls(items):
    out = []
    for item in items:
        if not isinstance(item, bytes):
            continue
        url = _text(item)
        try:
            scheme = urlsplit(url).scheme
        except ValueError:
            continue
        if scheme in _SCHEMES:
            out.append(url)
    return tuple(out)


def parse(data):
    try:
        top, span = decode(data)
    except BencodeError as e:
        raise _invalid(str(e)) from None
    if not isinstance(top, dict):
        raise _invalid("not a dictionary")
    info = top.get(b"info")
    if span is None or not isinstance(info, dict):
        raise _invalid("missing info dictionary")
    if b"pieces" not in info and info.get(b"meta version") == 2:
        raise TorrentError("v2-only torrents are not supported")
    name = info.get(b"name")
    if not isinstance(name, bytes) or not name:
        raise _invalid("missing name")
    piece_length = info.get(b"piece length")
    if (not isinstance(piece_length, int) or piece_length & (piece_length - 1)
            or not MIN_PIECE_LENGTH <= piece_length <= MAX_PIECE_LENGTH):
        raise _invalid("bad piece length")
    pieces = info.get(b"pieces")
    if not isinstance(pieces, bytes) or len(pieces) % 20:
        raise _invalid("bad pieces")
    count = len(pieces) // 20
    if count > MAX_PIECES:
        raise _invalid("too many pieces")
    name = _text(name)
    multi, files = _files(info, name)
    total = sum(f.length for f in files)
    if total <= 0:
        raise _invalid("no data")
    if count != -(-total // piece_length):
        raise _invalid("pieces do not match the total size")
    return Meta(
        info_hash=hashlib.sha1(data[span[0]:span[1]]).digest(),
        name=name,
        piece_length=piece_length,
        hashes=tuple(pieces[i:i + 20] for i in range(0, len(pieces), 20)),
        total=total,
        files=files,
        multi=multi,
        trackers=_trackers(top),
    )


def from_info(info_bytes, trackers=()):
    """The Meta for a bare info dictionary (as fetched from peers); `trackers` become one tier.

    TorrentError for the same reasons as a .torrent file, or when anything follows the dictionary.
    """
    wrapped = b"d4:info" + bytes(info_bytes) + b"e"
    meta = parse(wrapped)
    if meta.info_hash != hashlib.sha1(info_bytes).digest():  # the dictionary ended before the bytes did
        raise _invalid("trailing data")
    tier = _urls([t.encode() for t in trackers])
    return replace(meta, trackers=(tier,) if tier else ())


def torrent_bytes(info_bytes, trackers):
    """A .torrent holding these exact info bytes: announce is the first tracker, announce-list all of them."""
    out = b"d"
    if trackers:
        out += encode("announce") + encode(trackers[0]) + encode("announce-list") + encode([list(trackers)])
    return out + encode("info") + bytes(info_bytes) + b"e"


def load(path):
    path = Path(path)
    try:
        if path.stat().st_size > MAX_TORRENT_BYTES:
            raise _invalid("file too large")
        with open(path, "rb") as f:
            data = f.read(MAX_TORRENT_BYTES + 1)
        if len(data) > MAX_TORRENT_BYTES:
            raise _invalid("file too large")
    except OSError as e:
        raise DmError(f"cannot read {path.name}: {e.strerror or type(e).__name__}") from None
    return parse(data)
