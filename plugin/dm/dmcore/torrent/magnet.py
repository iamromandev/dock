"""Parsing a magnet link (BEP 9): the v1 info-hash, trackers and peers. Nothing here touches the network.

A magnet is only ever identified by `label()`: the link, its display name, tracker URLs and peer addresses
never appear in errors.
"""

import base64
import binascii
import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

from .metainfo import _SCHEMES, TorrentError

MAX_LINK = 64 * 1024
MAX_TRACKERS = 100
MAX_PEERS = 200
MAX_NAME = 200
_PREFIX = "magnet:?"
_KEY = re.compile(r"(xt|tr|dn|x\.pe)(\.\d+)?")
_HOST = re.compile(r"[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?")
_HEX = re.compile(r"[0-9A-Fa-f]{40}")
_BASE32 = re.compile(r"[A-Za-z2-7]{32}")


class MagnetError(TorrentError):
    """A magnet that cannot be used. `info_hash` is the one it named when that could be read, else None."""

    def __init__(self, message, info_hash=None):
        super().__init__(message)
        self.info_hash = info_hash


@dataclass(frozen=True)
class Magnet:
    info_hash: bytes  # 20 bytes
    name: str | None  # dn, sanitised; shown nowhere and never used as a file name
    trackers: tuple  # http/https/udp only, de-duplicated, order kept
    peers: tuple  # ((host, port), ...)


def is_magnet(arg):
    return arg[:len(_PREFIX)].lower() == _PREFIX


def label(info_hash):
    return f"magnet {info_hash.hex()[:8]}…"


def _invalid():
    return MagnetError("invalid magnet link")


def _info_hash(text):
    if _HEX.fullmatch(text):
        return bytes.fromhex(text)
    if _BASE32.fullmatch(text):
        try:
            return base64.b32decode(text.upper())
        except binascii.Error:
            pass
    raise _invalid()


def _tracker(url):
    try:
        parts = urlsplit(url)
        return parts.scheme in _SCHEMES and bool(parts.hostname) and bool(parts.netloc)
    except ValueError:
        return False


def _peer(text):
    host, sep, port = text.rpartition(":")
    if not sep or not port.isascii() or not port.isdigit() or not 1 <= int(port) <= 65535:
        return None
    if host.startswith("["):
        if not host.endswith("]"):
            return None
        try:
            return str(ipaddress.IPv6Address(host[1:-1])), int(port)
        except ValueError:
            return None
    if not _HOST.fullmatch(host):
        return None
    return host, int(port)


def _name(text):
    clean = "".join(c for c in text if c >= " " and c != "\x7f" and c not in "/\\")
    return clean[:MAX_NAME].strip() or None


def parse(arg):
    """The Magnet for a link; MagnetError (a TorrentError) when it names no v1 torrent or no way to find
    peers."""
    if len(arg) > MAX_LINK or not is_magnet(arg):
        raise _invalid()
    hashes, v2 = set(), False
    name, trackers, peers = None, [], []
    for pair in arg[len(_PREFIX):].split("&"):
        key, _, value = pair.partition("=")
        m = _KEY.fullmatch(key.lower())
        if m is None:
            continue
        kind = m.group(1)
        if kind == "xt":
            topic = unquote(value)
            urn = topic[:9].lower()
            if urn == "urn:btih:":
                hashes.add(_info_hash(topic[9:]))
            elif urn == "urn:btmh:":
                v2 = True
            # any other topic (urn:sha1, urn:ed2k, ...) is not ours: ignored
        elif kind == "dn":
            if name is None:
                name = _name(unquote(value.replace("+", " ")))
        elif kind == "tr":
            url = unquote(value)
            if len(trackers) < MAX_TRACKERS and url not in trackers and _tracker(url):
                trackers.append(url)
        else:
            addr = _peer(unquote(value))
            if addr is not None and len(peers) < MAX_PEERS and addr not in peers:
                peers.append(addr)
    if len(hashes) > 1:
        raise _invalid()
    if not hashes:
        raise MagnetError("v2-only magnet links are not supported" if v2 else "invalid magnet link")
    info_hash = hashes.pop()
    if not trackers and not peers:
        raise MagnetError("magnet link has no trackers or peers (DHT is not supported)", info_hash)
    return Magnet(info_hash, name, tuple(trackers), tuple(peers))
