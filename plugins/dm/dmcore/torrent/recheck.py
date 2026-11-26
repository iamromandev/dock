"""Re-hash the pieces of an existing partial: only what passes its SHA-1 counts as downloaded."""

import hashlib

from .. import Cancelled
from ..fmt import Throttle

CHECKING = "checking existing data"


def recheck(storage, meta, report, handle):
    """The set of piece indexes on disk that match the torrent. Runs in the worker thread, before the loop.

    A piece with a span in a file that is not there (an unpicked one never written) is not good.
    Cancelled when the handle is aborted (the data stays). `report(percent, text)` is throttled.
    """
    n = len(meta.hashes)
    good = set()
    throttle = Throttle()
    handle.emit(report, 0, CHECKING)
    for index in range(n):
        if handle.aborted:
            raise Cancelled()
        try:
            data = storage.read_block(index, 0, storage.piece_size(index))
        except FileNotFoundError:  # a file of an earlier selection that was never written: not on disk
            data = None
        if data is not None and hashlib.sha1(data).digest() == meta.hashes[index]:
            good.add(index)
        if throttle.ready():
            handle.emit(report, min(99, (index + 1) * 100 // n), CHECKING)
    return good
