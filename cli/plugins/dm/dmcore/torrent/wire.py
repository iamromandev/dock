"""BitTorrent peer wire format (BEP 3): handshake and length-prefixed messages with hard limits."""

import struct

PROTOCOL = b"BitTorrent protocol"
MAX_BLOCK = 16 * 1024
MAX_MSG = 1 << 20
MAX_EXT = 64 * 1024  # an extension message: its id byte, a small dictionary and at most one 16 KiB block
MAX_BITFIELD = (2 ** 20 + 7) // 8  # while the piece count is unknown: the most pieces metainfo allows
RESERVED = bytes([0, 0, 0, 0, 0, 0x10, 0, 0])  # BEP 10: we speak the extension protocol

CHOKE, UNCHOKE, INTERESTED, NOT_INTERESTED, HAVE, BITFIELD, REQUEST, PIECE, CANCEL = range(9)
EXTENDED = 20

_EMPTY = {CHOKE, UNCHOKE, INTERESTED, NOT_INTERESTED}


def handshake(info_hash, peer_id):
    return bytes([len(PROTOCOL)]) + PROTOCOL + RESERVED + info_hash + peer_id


async def read_handshake(reader, info_hash):
    """(peer id, reserved bytes); ValueError if the protocol or info-hash is wrong, or the data is short."""
    try:
        head = await reader.readexactly(1 + len(PROTOCOL))
        if head != bytes([len(PROTOCOL)]) + PROTOCOL:
            raise ValueError("not a BitTorrent handshake")
        rest = await reader.readexactly(8 + 20 + 20)
    except EOFError:  # IncompleteReadError is an EOFError
        raise ValueError("short handshake") from None
    if rest[8:28] != info_hash:
        raise ValueError("wrong info-hash")
    return rest[28:], rest[:8]


def supports_extensions(reserved):
    return len(reserved) == 8 and bool(reserved[5] & 0x10)


def extended(ext_id, body):
    return msg(EXTENDED, bytes([ext_id]) + body)


def msg(mid, payload=b""):
    return struct.pack(">I", len(payload) + 1) + bytes([mid]) + payload


def have(index):
    return msg(HAVE, struct.pack(">I", index))


def bitfield(bits):
    return msg(BITFIELD, bytes(bits))


def request(index, begin, length):
    return msg(REQUEST, struct.pack(">III", index, begin, length))


def cancel(index, begin, length):
    return msg(CANCEL, struct.pack(">III", index, begin, length))


def piece(index, begin, data):
    return msg(PIECE, struct.pack(">II", index, begin) + data)


def keep_alive():
    return b"\x00\x00\x00\x00"


def _limit(mid, npieces):
    """(min, max) payload length for a known id, or None for an unknown one."""
    if mid in _EMPTY:
        return 0, 0
    if mid == HAVE:
        return 4, 4
    if mid == BITFIELD:
        if npieces is None:
            return 1, MAX_BITFIELD
        n = -(-npieces // 8)
        return n, n
    if mid == EXTENDED:
        return 1, MAX_MSG  # a larger one is skipped by read_message, not an error
    if mid in (REQUEST, CANCEL):
        return 12, 12
    if mid == PIECE:
        return 8 + 1, 8 + MAX_BLOCK
    return None


async def read_message(reader, npieces):
    """(id, payload), or None for a keep-alive or an oversized extension message (skipped).

    ValueError when a size limit is broken.

    npieces is None while the piece count is unknown (magnet link before its metadata).

    The length is checked before the body is read, so a hostile length never allocates memory.
    """
    length = struct.unpack(">I", await reader.readexactly(4))[0]
    if length == 0:
        return None
    if length > MAX_MSG:
        raise ValueError("message too large")
    mid = (await reader.readexactly(1))[0]
    size = length - 1
    limit = _limit(mid, npieces)
    if limit is not None and not limit[0] <= size <= limit[1]:
        raise ValueError(f"bad size for message {mid}")
    if mid == EXTENDED and size > MAX_EXT:  # nothing we use is this big: read it in pieces and ignore it
        while size:
            size -= len(await reader.readexactly(min(size, MAX_BLOCK)))
        return None
    return mid, (await reader.readexactly(size) if size else b"")


def parse_have(payload):
    return struct.unpack(">I", payload)[0]


def parse_request(payload):
    return struct.unpack(">III", payload)


def parse_piece(payload):
    index, begin = struct.unpack(">II", payload[:8])
    return index, begin, payload[8:]


def parse_bitfield(payload, npieces):
    """Indices of the pieces set in a bitfield; ValueError if a spare bit is set."""
    out = []
    for i, byte in enumerate(payload):
        for bit in range(8):
            if byte & (0x80 >> bit):
                index = i * 8 + bit
                if index >= npieces:
                    raise ValueError("spare bit set in bitfield")
                out.append(index)
    return out
