"""KRPC messages (BEP 5), encoded and parsed strictly. Everything received is hostile."""

import socket
import struct
from dataclasses import dataclass, field
from typing import NamedTuple

from .. import bencode

MAX_DATAGRAM = 1500
MAX_NODES = 16
MAX_VALUES = 100
MAX_TID = 8
MAX_TOKEN = 64
MAX_METHOD = 32
MAX_DEPTH = 8
MAX_ITEMS = 2000
NODE_SIZE = 26
PEER_SIZE = 6


class KrpcError(ValueError):
    pass


class Node(NamedTuple):
    id: bytes
    addr: tuple


@dataclass(frozen=True)
class Reply:
    tid: bytes
    id: bytes
    nodes: list = field(default_factory=list)  # [(id, (ip, port))]
    values: list = field(default_factory=list)  # [(ip, port)]
    token: bytes = None


@dataclass(frozen=True)
class Query:
    tid: bytes
    method: str


@dataclass(frozen=True)
class ErrorMsg:
    tid: bytes


def query(tid, method, args):
    """A KRPC query for the wire. "ro": 1 marks this node read-only (BEP 43)."""
    return bencode.encode({b"t": tid, b"y": b"q", b"q": method, b"a": args, b"ro": 1})


def _ip_int(host):
    """The integer of a dotted-quad string, or None for anything else (no ipaddress: this runs per entry)."""
    parts = host.split(".")
    if len(parts) != 4:
        return None
    n = 0
    for p in parts:
        if not (p.isascii() and p.isdigit()) or len(p) > 3 or (p[0] == "0" and len(p) > 1) or int(p) > 255:
            return None
        n = n << 8 | int(p)
    return n


# (network, mask) of the ranges that are not the public Internet
_PRIVATE = ((0x0A000000, 0xFF000000),  # 10/8
            (0xAC100000, 0xFFF00000),  # 172.16/12
            (0xC0A80000, 0xFFFF0000),  # 192.168/16
            (0xA9FE0000, 0xFFFF0000),  # 169.254/16, link-local and cloud metadata
            (0x64400000, 0xFFC00000),  # 100.64/10, CGNAT
            (0xF0000000, 0xF0000000),  # 240/4, reserved
            (0xC6120000, 0xFFFE0000))  # 198.18/15, benchmarking


def _ok(ip, port, allow_loopback, allow_private):
    if not 1 <= port <= 65535 or ip >> 24 == 0 or ip >> 28 == 0xE or ip == 0xFFFFFFFF:
        return False  # 0/8, multicast, broadcast
    if ip >> 24 == 127:
        return allow_loopback
    if allow_private:
        return True
    return not any(ip & mask == net for net, mask in _PRIVATE)


def usable(addr, allow_loopback=False, allow_private=False):
    """May dm send to this (ip, port)? IPv4 only, a real port.

    Never 0/8, broadcast or multicast. Loopback needs allow_loopback; 10/8, 172.16/12, 192.168/16,
    169.254/16, 100.64/10, 240/4 and 198.18/15 need allow_private: a hostile node must not steer dm at the
    LAN.
    """
    try:
        host, port = addr
        if not isinstance(host, str) or type(port) is not int:
            return False
    except (TypeError, ValueError):
        return False
    ip = _ip_int(host)
    return ip is not None and _ok(ip, port, allow_loopback, allow_private)


def _addr(chunk):
    return socket.inet_ntoa(chunk[:4]), struct.unpack(">H", chunk[4:6])[0]


def compact_nodes(data, allow_loopback=False, allow_private=False):
    """[(id, addr)] from 26-byte entries; unusable entries are skipped, at most MAX_NODES kept."""
    out = []
    for i in range(0, min(len(data), MAX_NODES * NODE_SIZE), NODE_SIZE):
        chunk = data[i:i + NODE_SIZE]
        if len(chunk) == NODE_SIZE and _ok(
                int.from_bytes(chunk[20:24], "big"), int.from_bytes(chunk[24:26], "big"),
                allow_loopback, allow_private):
            out.append((bytes(chunk[:20]), _addr(chunk[20:])))
    return out


def compact_peers(values, allow_loopback=False, allow_private=False):
    """[(ip, port)] from 6-byte strings; wrong sizes and unusable entries are skipped.

    At most MAX_VALUES are read.
    """
    out = []
    for v in values[:MAX_VALUES]:
        if isinstance(v, bytes) and len(v) == PEER_SIZE and _ok(
                int.from_bytes(v[:4], "big"), int.from_bytes(v[4:6], "big"), allow_loopback, allow_private):
            out.append(_addr(v))
    return out


def _get(d, key, kind):
    value = d.get(key)
    if not isinstance(value, kind):
        raise KrpcError(f"bad {key.decode()}")
    return value


class Header(NamedTuple):
    """The cheap part of a message: enough to say which outstanding query it answers."""
    tid: bytes
    kind: bytes  # b"q", b"r" or b"e"
    msg: dict


def parse_header(data):
    """A Header after the size, bencode and the t and y keys are checked; the body is not looked at."""
    if len(data) > MAX_DATAGRAM:
        raise KrpcError("datagram too large")
    try:
        # KRPC encoders in the wild do not always sort keys: accept any order, still refuse a repeated key
        msg, _ = bencode.decode(data, max_depth=MAX_DEPTH, max_nodes=MAX_ITEMS, sorted_keys=False)
    except bencode.BencodeError as e:
        raise KrpcError(str(e)) from None
    if not isinstance(msg, dict):
        raise KrpcError("not a dictionary")
    tid = _get(msg, b"t", bytes)
    if not 1 <= len(tid) <= MAX_TID:
        raise KrpcError("bad transaction id")
    kind = _get(msg, b"y", bytes)
    if kind not in (b"q", b"r", b"e"):
        raise KrpcError("unknown message type")
    return Header(tid, kind, msg)


def parse_body(head, allow_loopback=False, allow_private=False):
    """A Reply, Query or ErrorMsg from a Header: the nodes, values and token are validated here."""
    msg, tid = head.msg, head.tid
    if head.kind == b"q":
        method = _get(msg, b"q", bytes)
        if len(method) > MAX_METHOD:
            raise KrpcError("bad method")
        return Query(tid, method.decode("latin-1"))
    if head.kind == b"e":
        _get(msg, b"e", list)
        return ErrorMsg(tid)
    body = _get(msg, b"r", dict)
    nid = _get(body, b"id", bytes)
    if len(nid) != 20:
        raise KrpcError("bad node id")
    nodes = []
    if b"nodes" in body:
        raw = _get(body, b"nodes", bytes)
        if len(raw) % NODE_SIZE:
            raise KrpcError("bad nodes")
        nodes = compact_nodes(raw, allow_loopback, allow_private)
    values = []
    if b"values" in body:
        raw = _get(body, b"values", list)[:MAX_VALUES]
        if not all(isinstance(v, bytes) for v in raw):
            raise KrpcError("bad values")
        values = compact_peers(raw, allow_loopback, allow_private)
    token = None
    if b"token" in body:
        token = _get(body, b"token", bytes)
        if len(token) > MAX_TOKEN:
            raise KrpcError("bad token")
    return Reply(tid, nid, nodes, values, token)


def parse(data, allow_loopback=False, allow_private=False):
    """A Reply, Query or ErrorMsg; KrpcError for anything that is not a well-formed KRPC message."""
    return parse_body(parse_header(data), allow_loopback, allow_private)
