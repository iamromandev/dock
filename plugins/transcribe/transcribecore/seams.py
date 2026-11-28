"""Joining the cues of two overlapping windows."""

import difflib
import re

SEAM_WORDS = 3  # a common run of this many words (2 when a side has under SEAM_SHORT words) marks the splice point
SEAM_SHORT = 6


def norm(word):
    return re.sub(r"\W", "", word.casefold()) or word


def at(cue, i, n):
    """The time of word i of n in a cue, by linear interpolation."""
    return cue[0] + (cue[1] - cue[0]) * i / n if n else cue[0]


def splice(tail, new, lo, hi):
    """Joins the previous window's cues that end after `lo` (`tail`) with this window's `new` cues over the overlap
    [lo, hi]. Whisper segments each window differently, so the join is by words: from the longest run of words both
    versions share, this window's (uncut) version wins. With no such run, a time cut at the middle of the overlap."""
    head = [c for c in new if c[0] < hi]
    rest = [c for c in new if c[0] >= hi]
    if not tail or not head:
        return tail + new
    words = [[(w, i, k) for k, w in enumerate(c[2].split())] for i, c in enumerate(tail)]
    a = [x for ws in words for x in ws]
    words = [[(w, i, k) for k, w in enumerate(c[2].split())] for i, c in enumerate(head)]
    b = [x for ws in words for x in ws]
    na, nb = [norm(x[0]) for x in a], [norm(x[0]) for x in b]
    m = difflib.SequenceMatcher(None, na, nb, autojunk=False).find_longest_match(0, len(na), 0, len(nb))
    if m.size >= (2 if min(len(a), len(b)) < SEAM_SHORT else SEAM_WORDS):
        _, ai, ak = a[m.a]
        _, bi, bk = b[m.b]
        cut = head[bi]
        n = len(cut[2].split())
        begin = min(max(at(cut, bk, n), cut[0]), cut[1])
        out = tail[:ai]
        if ak:
            ends = tail[ai]
            n = len(ends[2].split())
            stop = min(max(at(ends, ak, n), ends[0]), begin)
            out.append((ends[0], max(stop, ends[0]), " ".join(ends[2].split()[:ak])))
        out.append((begin, cut[1], " ".join(cut[2].split()[bk:])))
        return out + head[bi + 1:] + rest
    mid = (lo + hi) / 2
    return [c for c in tail if c[0] < mid] + [c for c in new if c[0] >= mid]
