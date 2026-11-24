"""Fetching a torrent's metadata from peers (BEP 9 over the BEP 10 extension protocol).

Pure bookkeeping, no I/O: message helpers and `MetadataAssembler`, which decides what to ask which peer for
and checks the result against the info-hash. Whatever peers send, the assembler holds at most MAX_METADATA
bytes (NFR1): every peer has one block in flight, at most MAX_SIZES announced sizes are tracked, and blocks
are only stored for a size some peer announced, in the length that size implies.
"""

import hashlib
import io
import time
from dataclasses import dataclass

from . import bencode, wire
from .metainfo import MAX_TORRENT_BYTES

BLOCK = 16 * 1024
MAX_METADATA = MAX_TORRENT_BYTES
OUR_UT_METADATA = 3  # the id we put in our "m"; peers send our ut_metadata messages under it
MAX_SIZES = 3  # announced sizes tracked at once
REQUEST_TIMEOUT = 10  # seconds before a block that was not answered goes to another peer
STALE = 10  # seconds without a stored block before an attempt may be evicted for another one

REQUEST, DATA, REJECT = 0, 1, 2


@dataclass(frozen=True)
class ExtInfo:
    ut_metadata: int | None  # the id they expect our ut_metadata requests under
    metadata_size: int | None
    client: str | None = None  # their `v`: the name and version they call themselves


def ext_handshake(metadata_size=None):
    """The body of our extension handshake (send it with wire.extended(0, body))."""
    top = {"m": {"ut_metadata": OUR_UT_METADATA}, "v": "dm 0.6.0", "reqq": 250}
    if metadata_size is not None:
        top["metadata_size"] = metadata_size
    return bencode.encode(top)


def parse_ext_handshake(body):
    """ExtInfo from a peer's extension handshake body; ValueError on anything malformed."""
    top, _ = bencode.decode_prefix(body)
    if not isinstance(top, dict):
        raise ValueError("bad extension handshake")
    ids = top.get(b"m", {})
    if not isinstance(ids, dict):
        raise ValueError("bad extension handshake")
    uid = ids.get(b"ut_metadata")
    if uid is not None and (not isinstance(uid, int) or not 0 <= uid <= 255):
        raise ValueError("bad ut_metadata id")
    size = top.get(b"metadata_size")
    if size is not None and not isinstance(size, int):
        raise ValueError("bad metadata_size")
    client = top.get(b"v")
    client = client[:64].decode("utf-8", "replace") if isinstance(client, bytes) else None
    return ExtInfo(uid or None, size, client)


def _message(their_id, head, data=b""):
    return wire.extended(their_id, bencode.encode(head) + data)


def request_msg(their_id, piece):
    return _message(their_id, {"msg_type": REQUEST, "piece": piece})


def reject_msg(their_id, piece):
    return _message(their_id, {"msg_type": REJECT, "piece": piece})


def data_msg(their_id, piece, total, data):
    return _message(their_id, {"msg_type": DATA, "piece": piece, "total_size": total}, data)


def parse_metadata_msg(body):
    """(msg_type, piece, total_size or None, data) from a ut_metadata body; ValueError if malformed."""
    head, end = bencode.decode_prefix(body)
    if not isinstance(head, dict):
        raise ValueError("bad metadata message")
    kind, piece, total = head.get(b"msg_type"), head.get(b"piece"), head.get(b"total_size")
    if not isinstance(kind, int) or not isinstance(piece, int) or piece < 0:
        raise ValueError("bad metadata message")
    if total is not None and not isinstance(total, int):
        raise ValueError("bad metadata message")
    return kind, piece, total, body[end:]


class _Attempt:
    """The blocks gathered for one announced size."""

    def __init__(self, size):
        self.size = size
        self.nblocks = -(-size // BLOCK)
        self.blocks = {}  # piece -> bytes
        self.senders = set()  # peers that supplied a stored block
        self.flight = {}  # piece -> (peer, deadline)
        self.last_block = None
        self.solo = False  # a set of several peers failed the hash: every block now comes from one peer
        self.owner = None  # that peer
        self.touched = None  # when it was chosen or last sent a block

    def length(self, piece):
        return min(BLOCK, self.size - piece * BLOCK)

    def stored(self):
        return sum(len(b) for b in self.blocks.values())

    def clear(self):
        self.blocks.clear()
        self.senders.clear()


class MetadataAssembler:
    def __init__(self, info_hash, clock=time.monotonic):
        self.info_hash = info_hash
        self._clock = clock
        self.banned = set()  # peers whose own complete set failed the hash
        self.metadata = None  # the verified info dictionary bytes, once complete
        self._attempts = {}  # announced size -> _Attempt
        self._size_of = {}  # peer -> the size it announced
        self._asked = {}  # peer -> pieces it was asked for and has not answered
        self._flying = {}  # peer -> (piece, deadline) of its current request
        self._refused = set()  # peers that rejected a request: not asked again
        self._evicted = {}  # peer -> size, for peers whose size lost its slot: offered again once one frees
        self._stored = 0

    # peers

    def offer(self, peer, size):
        """A peer announced metadata_size. False means drop the peer: the size is not valid. A valid size
        that finds no slot (every slot holds fresh blocks) leaves the peer unused for metadata for now, which
        is no reason to drop it: next_request offers it again once a slot frees."""
        if peer in self.banned or not isinstance(size, int) or not 0 < size <= MAX_METADATA:
            return False
        if self._size_of.get(peer) == size or self._evicted.get(peer) == size:
            return True
        self.peer_gone(peer)  # whatever it announced or was asked before is void
        if not self._admit(peer, size, self._clock()):
            self._evicted[peer] = size
        return True

    def _admit(self, peer, size, now):
        if size not in self._attempts:
            if len(self._attempts) >= MAX_SIZES and not self._evict_slot(now):
                return False
            self._attempts[size] = _Attempt(size)
        self._size_of[peer] = size
        return True

    def peer_gone(self, peer):
        self._release(peer)
        self._asked.pop(peer, None)
        self._refused.discard(peer)
        self._evicted.pop(peer, None)
        self._disown(peer)
        size = self._size_of.pop(peer, None)
        if size is not None:
            self._forget(size)

    def _evict_slot(self, now):
        """Free a slot: one that holds no blocks, or whose blocks are stale with nothing in flight. The one
        with nothing in flight, the fewest peers and the fewest bytes goes first."""
        def live(att):
            return any(now < deadline for _, deadline in att.flight.values())

        idle = [a for a in self._attempts.values()
                if not a.blocks or (not live(a) and now - a.last_block >= STALE)]
        if not idle:
            return False

        def cost(att):
            return (live(att), sum(1 for s in self._size_of.values() if s == att.size), att.stored())

        victim = min(idle, key=cost)
        for peer in [p for p, s in self._size_of.items() if s == victim.size]:
            self._release(peer)
            self._asked.pop(peer, None)
            self._refused.discard(peer)
            del self._size_of[peer]
            self._evicted[peer] = victim.size
        self._stored -= victim.stored()
        del self._attempts[victim.size]
        return True

    def _reset(self, att):
        """Throw away what an attempt holds, and the requests for it."""
        self._stored -= att.stored()
        att.clear()
        att.flight.clear()
        att.owner = None
        for peer, size in self._size_of.items():
            if size == att.size:
                self._flying.pop(peer, None)

    def _disown(self, peer):
        for att in self._attempts.values():
            if att.owner == peer:
                self._reset(att)

    @staticmethod
    def _held_by_other(att, piece, peer, now):
        held = att.flight.get(piece)
        return held is not None and held[0] != peer and now < held[1]

    def _release(self, peer):
        flying = self._flying.pop(peer, None)
        size = self._size_of.get(peer)
        if flying is not None and size in self._attempts:
            flight = self._attempts[size].flight
            if flight.get(flying[0], (None,))[0] == peer:  # not one that was handed to someone else since
                del flight[flying[0]]

    def _forget(self, size):
        """Drop an attempt no connected peer offers, with whatever blocks it holds."""
        att = self._attempts.get(size)
        if att is not None and not any(s == size for s in self._size_of.values()):
            self._stored -= att.stored()
            del self._attempts[size]

    # requests

    def _evict_stale(self, keep, now, need):
        """Free space for `need` bytes by clearing other attempts that stopped receiving blocks."""
        others = sorted((a for a in self._attempts.values() if a is not keep and a.blocks),
                        key=lambda a: -a.stored())
        for att in others:
            if self._reserved() + need <= MAX_METADATA:
                return
            if att.last_block is None or now - att.last_block >= STALE:
                self._stored -= att.stored()
                att.clear()
                self._forget(att.size)

    def _reserved(self):
        return self._stored + sum(att.length(p) for att in self._attempts.values()
                                  for p in att.flight)

    def next_request(self, peer, now):
        """A block this peer should be asked for, or None. One request per peer is in flight; an unanswered
        one times out after REQUEST_TIMEOUT and its block goes to whoever asks next."""
        if peer in self._evicted and self.metadata is None and peer not in self.banned:
            if self._admit(peer, self._evicted[peer], now):
                del self._evicted[peer]
        size = self._size_of.get(peer)
        if (self.metadata is not None or size is None or peer in self.banned or peer in self._refused
                or size not in self._attempts):
            return None
        att = self._attempts[size]
        flying = self._flying.get(peer)
        if flying is not None:
            if now < flying[1]:
                return None
            self._release(peer)
        if att.solo:
            if att.owner is None or att.owner == peer:
                if att.owner is None:
                    att.owner, att.touched = peer, now
            elif now - att.touched < STALE:
                return None
            else:  # the owner stopped sending: someone else tries the whole set
                self._reset(att)
                att.owner, att.touched = peer, now
        owed = self._asked.get(peer, ())  # never ask a peer again for a block it still owes
        missing = [p for p in range(att.nblocks) if p not in att.blocks and p not in owed]
        free = [p for p in missing if not self._held_by_other(att, p, peer, now)]
        for piece in free or missing:  # nothing unheld left: ask for a block someone else is slow with
            need = att.length(piece)
            if self._reserved() + need > MAX_METADATA:
                self._evict_stale(att, now, need)
                if size not in self._attempts or self._reserved() + need > MAX_METADATA:
                    return None
            deadline = now + REQUEST_TIMEOUT
            att.flight[piece] = (peer, deadline)
            self._flying[peer] = (piece, deadline)
            self._asked.setdefault(peer, set()).add(piece)
            return piece
        return None

    # answers

    def received(self, peer, piece, data, total=None):
        """A block from a peer. The full metadata once every block is in and it hashes to the info-hash, else
        None. ValueError means drop the peer: unrequested block, wrong length, or a total that differs from
        the size it announced. A set that fails the hash bans its sender when it came from one peer; when it
        came from several, nobody is banned and the next set for that size is taken from a single peer."""
        if peer in self._evicted:
            return None
        size = self._size_of.get(peer)
        att = self._attempts.get(size)
        if att is None:
            raise ValueError("unrequested metadata block")
        if total is not None and total != size:
            raise ValueError("metadata size changed")
        asked = self._asked.get(peer, ())
        if piece not in asked:
            raise ValueError("unrequested metadata block")
        asked.discard(piece)
        flying = self._flying.get(peer)
        if flying is not None and flying[0] == piece:
            del self._flying[peer]
        if att.flight.get(piece, (None,))[0] == peer:
            del att.flight[piece]
        if piece >= att.nblocks or len(data) != att.length(piece):
            raise ValueError("wrong metadata block length")
        if att.solo and att.owner != peer:
            return None  # a late block from a peer that does not own this attempt
        if self.metadata is not None or piece in att.blocks or self._stored + len(data) > MAX_METADATA:
            return None  # a late copy, or no room for it: the data is dropped
        att.blocks[piece] = bytes(data)
        att.senders.add(peer)
        att.last_block = att.touched = self._clock()
        self._stored += len(data)
        if len(att.blocks) < att.nblocks:
            return None
        digest = hashlib.sha1()  # block by block: a joined copy of a hostile set would double the memory
        for i in range(att.nblocks):
            digest.update(att.blocks[i])
        if digest.digest() == self.info_hash:
            out = io.BytesIO()  # each block is released once copied: no second full copy at the peak
            for i in range(att.nblocks):
                out.write(att.blocks.pop(i))
            whole = out.getvalue()  # shares the buffer, no copy
            out.close()
            self.metadata = whole
            self._stored -= len(whole)
            for other in self._attempts.values():
                other.clear()
                other.flight.clear()
            self._stored = 0
            self._flying.clear()
            return whole
        if len(att.senders) == 1:  # one peer's own complete set failed
            self.banned |= att.senders
        else:  # nobody can be blamed: the next set comes from one peer
            att.solo = True
        self._reset(att)
        return None

    def rejected(self, peer, piece):
        """The peer does not have that block: never ask it again."""
        self._asked.get(peer, set()).discard(piece)
        self._release(peer)
        self._disown(peer)
        self._refused.add(peer)
