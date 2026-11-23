"""Which blocks to ask which peer for: rarest first, then endgame. Pure bookkeeping, no I/O."""

import random

BLOCK = 16 * 1024
BUFFER_BYTES = 64 * 1024 * 1024  # in-flight piece buffers stay under this (but at least 4 pieces)


class BadBlock(ValueError):
    """A block we did not ask that peer for, or with the wrong size."""


class _Bag:
    """A set with O(1) add, discard and random pick."""

    def __init__(self):
        self.items = []
        self.pos = {}

    def __len__(self):
        return len(self.items)

    def __contains__(self, item):
        return item in self.pos

    def add(self, item):
        if item not in self.pos:
            self.pos[item] = len(self.items)
            self.items.append(item)

    def discard(self, item):
        i = self.pos.pop(item, None)
        if i is not None:
            last = self.items.pop()
            if i < len(self.items):
                self.items[i] = last
                self.pos[last] = i

    def pick(self, rng, have, exact=False):
        """A random member that is also in `have`, or None. Without `exact` a big bucket is only
        sampled, so None can mean "probably none": the caller falls back to an exact scan."""
        n = len(self.items)
        if n == 0:
            return None
        for _ in range(64):
            item = self.items[rng.randrange(n)]
            if item in have:
                return item
        if not exact and n > 4096 and len(have) > 4096:
            return None
        small, other = (self.items, have) if n <= len(have) else (have, self.pos)
        found = [i for i in small if i in other]
        return rng.choice(found) if found else None


class _Active:
    """A piece being downloaded: its buffer, who was asked for which block, what has arrived."""

    def __init__(self, size):
        self.size = size
        self.nblocks = -(-size // BLOCK)
        self.buf = bytearray(size)
        self.asked = {}  # block number -> set of peers asked
        self.got = set()
        self.from_peers = set()

    def length(self, block):
        return min(BLOCK, self.size - block * BLOCK)


class Picker:
    def __init__(self, npieces, piece_length, total, max_active=None, rng=None):
        self.npieces = npieces
        self.piece_length = piece_length
        self.total = total
        self.max_active = max_active or max(4, BUFFER_BYTES // piece_length)
        self.rng = rng or random.Random()
        self.avail = [0] * npieces
        self.peers = {}  # peer -> set of piece indices it has
        self.missing = set(range(npieces))  # not verified and not started
        self.buckets = {0: _Bag()}  # availability -> missing pieces with that many peers
        for i in range(npieces):
            self.buckets[0].add(i)
        self.want = {}  # peer -> how many pieces it has that we have not verified
        self.active = {}  # piece -> _Active (includes a complete piece waiting for its hash)
        self.finished = set()
        self.out = {}  # peer -> {(piece, begin): length} requests it still owes us
        self.late = {}  # peer -> blocks we cancelled; a late copy is dropped quietly
        self.cancels = []
        self._done_bytes = 0

    def piece_size(self, index):
        return min(self.piece_length, self.total - index * self.piece_length)

    # peers

    def _check(self, index):
        if not 0 <= index < self.npieces:
            raise ValueError("piece index out of range")

    def _move(self, index, delta):
        """Change a piece's availability, keeping its rarity bucket right if it is still missing."""
        if index in self.missing:
            old = self.buckets[self.avail[index]]
            old.discard(index)
            if not old and self.avail[index]:
                del self.buckets[self.avail[index]]
            self.avail[index] += delta
            self.buckets.setdefault(self.avail[index], _Bag()).add(index)
        else:
            self.avail[index] += delta

    def peer_has(self, pid, index):
        self._check(index)
        have = self.peers.setdefault(pid, set())
        if index not in have:
            have.add(index)
            self._move(index, 1)
            if index not in self.finished:
                self.want[pid] = self.want.get(pid, 0) + 1

    def peer_bitfield(self, pid, indices):
        indices = list(indices)
        for index in indices:
            self._check(index)
        for index in indices:
            self.peer_has(pid, index)

    def release(self, pid):
        """Forget what we asked pid for (it choked us): those blocks can go to others."""
        for (index, begin) in self.out.pop(pid, {}):
            act = self.active.get(index)
            if act is not None:
                asked = act.asked.get(begin // BLOCK)
                if asked is not None:
                    asked.discard(pid)
        self.late.pop(pid, None)

    def peer_gone(self, pid):
        self.release(pid)
        for index in self.peers.pop(pid, ()):
            self._move(index, -1)
        self.want.pop(pid, None)

    def interesting(self, pid):
        return self.want.get(pid, 0) > 0

    def outstanding(self, pid):
        return len(self.out.get(pid, ()))

    # choosing blocks

    def _open(self, index):
        self._unmiss(index)
        self.active[index] = _Active(self.piece_size(index))

    def _unmiss(self, index):
        if index in self.missing:
            self.missing.discard(index)
            bucket = self.buckets[self.avail[index]]
            bucket.discard(index)
            if not bucket and self.avail[index]:
                del self.buckets[self.avail[index]]

    def _miss(self, index):
        self.missing.add(index)
        self.buckets.setdefault(self.avail[index], _Bag()).add(index)

    def _rarest(self, have):
        for exact in (False, True):  # sampling first: a peer that lacks the rare pieces costs little
            for level in sorted(self.buckets):
                index = self.buckets[level].pick(self.rng, have, exact)
                if index is not None:
                    return index
        return None

    def _take(self, pid, index, block, out, mine):
        act = self.active[index]
        act.asked.setdefault(block, set()).add(pid)
        key = (index, block * BLOCK)
        mine[key] = act.length(block)
        out.append((index, block * BLOCK, act.length(block)))

    def next_requests(self, pid, max_n):
        """Up to max_n more (piece, begin, length) requests for this peer."""
        have = self.peers.get(pid)
        if not have or max_n <= 0:
            return []
        out = []
        mine = self.out.setdefault(pid, {})
        for index in sorted(have & self.active.keys()):  # finish started pieces first
            act = self.active[index]
            for block in range(act.nblocks):
                if len(out) >= max_n:
                    return out
                if block not in act.got and not act.asked.get(block):
                    self._take(pid, index, block, out, mine)
        while len(out) < max_n and len(self.active) < self.max_active:
            index = self._rarest(have)
            if index is None:
                break
            self._open(index)
            act = self.active[index]
            for block in range(act.nblocks):
                if len(out) >= max_n:
                    break
                self._take(pid, index, block, out, mine)
        if not self.missing and len(out) < max_n:  # endgame: every remaining block is requested
            for index in sorted(have & self.active.keys()):
                act = self.active[index]
                for block in range(act.nblocks):
                    if len(out) >= max_n:
                        return out
                    if block not in act.got and (index, block * BLOCK) not in mine:
                        self._take(pid, index, block, out, mine)
        return out

    # blocks arriving

    def block_received(self, pid, index, begin, data):
        """Store a block. The whole piece when it was the last one, else None.

        BadBlock if pid was not asked for exactly this block. A copy we already cancelled is ignored.
        """
        key = (index, begin)
        mine = self.out.get(pid)
        if mine is None or key not in mine:
            late = self.late.get(pid)
            if late is not None and key in late:
                late.discard(key)
                return None
            raise BadBlock("unrequested block")
        if mine.pop(key) != len(data):
            raise BadBlock("wrong block length")
        act = self.active.get(index)
        if act is None:
            return None
        block = begin // BLOCK
        asked = act.asked.get(block)
        if asked is not None:
            asked.discard(pid)
        if block in act.got:
            return None
        act.got.add(block)
        act.from_peers.add(pid)
        act.buf[begin:begin + len(data)] = data
        for other in list(asked or ()):  # endgame duplicates: cancel them
            theirs = self.out.get(other)
            if theirs is not None and theirs.pop(key, None) is not None:
                self.late.setdefault(other, set()).add(key)
                self.cancels.append((other, index, begin, len(data)))
        if asked is not None:
            asked.clear()
        if len(act.got) == act.nblocks:
            data, act.buf = act.buf, None  # handed over: the piece is not held twice while it is hashed
            return data
        return None

    def drain_cancels(self):
        out, self.cancels = self.cancels, []
        return out

    # verified or failed pieces

    def piece_done(self, index):
        act = self.active.pop(index, None)
        if act is not None and index not in self.finished:
            self.finished.add(index)
            self._done_bytes += act.size
            for pid, have in self.peers.items():
                if index in have:
                    self.want[pid] -= 1

    def piece_failed(self, index):
        """Throw the piece away so it is fetched again; the peers that sent any block of it."""
        act = self.active.pop(index, None)
        self._miss(index)
        return set(act.from_peers) if act is not None else set()

    def done(self):
        return len(self.finished) == self.npieces

    def verified_bytes(self):
        return self._done_bytes
