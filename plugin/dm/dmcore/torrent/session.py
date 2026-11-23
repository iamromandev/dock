"""A torrent download: tracker loop, peer pool, piece checking, stall timers, and run_torrent.

Runs one asyncio loop inside dm's worker thread. Nothing listens: every connection is outbound. While the
download runs, verified pieces are served to interested peers the choker has unchoked; when it ends, every
connection closes (no seeding).
"""

import asyncio
import collections
import hashlib
import os
import random
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from .. import Cancelled, resume as resume_mod
from ..fmt import Throttle, human_size
from ..target import DmError
from . import layout, metainfo, tracker, wire
from .handle import TorrentHandle
from .choker import choose
from .peer import PeerConn
from .picker import Picker
from .place import place_dir, place_file
from .recheck import recheck
from .storage import LayoutMismatch, PrepareError, Storage

REPORTED_PORT = 6881  # what we tell trackers, though nothing listens
MAX_PENDING = 1000
RETRY_AFTER = 30  # seconds before dialing the same peer again


@dataclass(frozen=True)
class Options:
    max_peers: int = 30
    unchoke_slots: int = 4
    rotate_s: int = 10
    pipeline: int = 16
    stall_s: float = 120
    connect_timeout: float = 10
    idle_timeout: float = 120
    tracker_budget: float = tracker.BUDGET  # seconds one announce to one tracker may take


def _span(seconds):
    if seconds % 60 == 0:
        minutes = int(seconds // 60)
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    return f"{seconds:g} seconds"


class Session:
    def __init__(self, meta, storage, progress, handle, opts, log=None, on_complete=None, have=()):
        self.meta = meta
        self.storage = storage
        self.report = progress
        self.handle = handle
        self.opts = opts
        self._log = log
        self.on_complete = on_complete  # places the finished download, before the tracker is told
        self.peer_id = b"-DM0300-" + os.urandom(12)
        self.picker = Picker(len(meta.hashes), meta.piece_length, meta.total)
        self.picker.mark_verified(have)  # a resumed download: these pieces are ours before any peer connects
        self.conns = {}  # (host, port) -> PeerConn, connecting or connected
        self.banned = set()
        self.pending = collections.deque()
        self.queued = set()
        self.last_try = {}
        self.wake = asyncio.Event()
        self.finished = asyncio.Event()
        self.fatal = None
        self.pool = ThreadPoolExecutor(2, thread_name_prefix="dm-hash")
        self.reader_pool = ThreadPoolExecutor(2, thread_name_prefix="dm-read")
        self.optimistic = None
        self.throttle = Throttle()
        self.sent = None
        self.tracker_url = None
        self.last_progress = None
        self.background = set()

    def log(self, message):
        if self._log is not None:
            self._log(message)

    def fail(self, exc):
        if self.fatal is None:
            self.fatal = exc
        self.finished.set()

    # pieces

    def bitfield(self):
        bits = bytearray(-(-len(self.meta.hashes) // 8))
        for i in self.picker.finished:
            bits[i // 8] |= 0x80 >> (i % 8)
        return bytes(bits)

    def piece_complete(self, index, data):
        task = asyncio.ensure_future(self._finish_piece(index, data))
        self.background.add(task)
        task.add_done_callback(self.background.discard)

    def _check_and_store(self, index, data):
        if hashlib.sha1(data).digest() != self.meta.hashes[index]:
            return False
        self.storage.write_piece(index, data)
        return True

    async def _finish_piece(self, index, data):
        loop = asyncio.get_running_loop()
        try:
            ok = await loop.run_in_executor(self.pool, self._check_and_store, index, data)
        except asyncio.CancelledError:
            raise
        except BaseException as e:  # a write error: the whole download fails
            self.fail(e)
            return
        if ok:
            self.picker.piece_done(index)
            self.last_progress = loop.time()
            for conn in list(self.conns.values()):
                if conn.handshaken:
                    conn.send(wire.have(index))
            self.progress()
            if self.picker.done():
                self.finished.set()
        else:
            for addr in self.picker.piece_failed(index):
                self.banned.add(addr)
                conn = self.conns.get(addr)
                if conn is not None:
                    conn.close()  # at once, so the refill below skips it
                    if conn.task is not None:
                        conn.task.cancel()
            self.log(f"piece {index} failed its hash; its senders are dropped")
        for conn in list(self.conns.values()):
            conn.refill()

    # progress

    def connected(self):
        return sum(1 for c in self.conns.values() if c.handshaken)

    def progress(self, final=False):
        done, total = self.picker.verified_bytes(), self.meta.total
        pct = 100 if final else min(99, done * 100 // total)
        n = self.connected()
        text = f"{human_size(done)} of {human_size(total)} · {n} peer{'s' if n != 1 else ''}"
        if self.sent == (pct, text):
            return
        if self.throttle.ready(final=final):
            self.sent = (pct, text)
            self.handle.emit(self.report, pct, text)

    # peers

    def peer_closed(self, conn):
        if self.conns.get(conn.addr) is conn:
            del self.conns[conn.addr]
        self.picker.peer_gone(conn.addr)
        self.wake.set()
        for other in list(self.conns.values()):
            other.refill()

    def add_peers(self, peers):
        shuffled = list(peers)
        random.shuffle(shuffled)
        for addr in shuffled:
            if len(self.pending) >= MAX_PENDING:
                break
            if addr in self.banned or addr in self.conns or addr in self.queued:
                continue
            self.queued.add(addr)
            self.pending.append(addr)
        self.wake.set()

    async def connector(self):
        loop = asyncio.get_running_loop()
        while True:
            while len(self.conns) < self.opts.max_peers and self.pending:
                addr = self.pending.popleft()
                self.queued.discard(addr)
                if addr in self.banned or addr in self.conns:
                    continue
                if loop.time() - self.last_try.get(addr, -RETRY_AFTER) < RETRY_AFTER:
                    continue
                self.last_try[addr] = loop.time()
                conn = PeerConn(self, addr)
                self.conns[addr] = conn
                conn.task = asyncio.ensure_future(conn.run())
            self.wake.clear()
            try:
                await asyncio.wait_for(self.wake.wait(), 1)
            except asyncio.TimeoutError:
                pass

    # uploads

    def interest_changed(self, conn):
        """A peer became interested: give it a slot at once if one is free; the next rechoke settles it."""
        held = sum(1 for c in self.conns.values() if c.handshaken and not c.am_choking)  # interested or not
        if conn.peer_interested and conn.am_choking and held < self.opts.unchoke_slots + 1:
            conn.set_choke(False)

    def rechoke(self, rng=random):
        """Unchoke the best uploaders to us and one optimistic peer; choke everyone else."""
        live = {c.addr: c for c in self.conns.values() if c.handshaken and not c.closed}
        rates = {}
        for addr, c in live.items():
            rates[addr] = c.received - c.mark
            c.mark = c.received
        interested = [a for a, c in live.items() if c.peer_interested]
        regular, self.optimistic = choose(interested, rates, self.opts.unchoke_slots, rng)
        keep = regular | ({self.optimistic} if self.optimistic is not None else set())
        for addr, c in live.items():
            c.set_choke(addr not in keep)

    async def choker(self):
        while True:
            await asyncio.sleep(self.opts.rotate_s)
            self.rechoke()

    # trackers

    def announce_request(self, event):
        done = self.picker.verified_bytes()
        return tracker.Announce(self.meta.info_hash, self.peer_id, REPORTED_PORT, 0, done,
                                self.meta.total - done, event)

    async def announce_tier(self, tier, event):
        """Ask every tracker of the tier at once. (url, result) from the first to answer, with the
        tier's own order breaking a tie; (None, reason) when all fail."""
        req = self.announce_request(event)
        budget = self.opts.tracker_budget
        tasks = {url: asyncio.ensure_future(tracker.announce(url, req, budget)) for url in tier}
        try:
            pending = set(tasks.values())
            while pending:
                _, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for url in tier:
                    task = tasks[url]
                    if task.done() and not task.cancelled() and task.exception() is None:
                        return url, task.result()
            reason = "no tracker answered"
            for url in tier:  # all failed: report the first in tier order
                exc = tasks[url].exception()
                if isinstance(exc, tracker.TrackerError):
                    return None, str(exc)
                if exc is not None:
                    raise exc
            return None, reason
        finally:
            for task in tasks.values():
                task.cancel()
            await asyncio.gather(*tasks.values(), return_exceptions=True)

    async def tracker_loop(self):
        tiers = [random.sample(list(t), len(t)) for t in self.meta.trackers]
        if not tiers:
            return
        event, first = "started", True
        while True:
            result, reason = None, "no tracker answered"
            for tier in tiers:
                url, got = await self.announce_tier(list(tier), event)
                if url is None:
                    reason = got
                    continue
                result = got
                tier.remove(url)  # BEP 12: the tracker that answered goes first
                tier.insert(0, url)
                self.tracker_url = url
                break
            if result is not None:
                self.add_peers(result.peers)
                wait = max(60, result.interval)
                event = ""
            else:
                if first:  # every tracker failed on the first try: nothing else will find peers
                    self.fail(DmError(f"tracker error: {reason}", keep=True))
                    return
                wait = 60
            first = False
            await asyncio.sleep(wait)

    async def farewell(self, completed):
        if self.tracker_url is None:
            return
        for event in (("completed",) if completed else ()) + ("stopped",):
            try:
                await tracker.announce(self.tracker_url, self.announce_request(event))
            except tracker.TrackerError:
                pass

    # stalls

    async def monitor(self):
        loop = asyncio.get_running_loop()
        tick = min(0.25, self.opts.stall_s / 4)
        while True:
            await asyncio.sleep(tick)
            idle = loop.time() - self.last_progress
            if self.connected():
                if idle >= 2.5 * self.opts.stall_s:
                    self.fail(DmError(f"stalled: no progress for {_span(2.5 * self.opts.stall_s)}", keep=True))
                    return
            elif idle >= self.opts.stall_s:
                self.fail(DmError("no peers found", keep=True))
                return
            self.progress()

    # the whole thing

    async def run(self):
        loop = asyncio.get_running_loop()
        self.last_progress = loop.time()
        loops = (self.tracker_loop(), self.connector(), self.monitor(), self.choker())
        workers = [asyncio.ensure_future(c) for c in loops]
        for worker in workers:
            worker.add_done_callback(self._worker_done)
        completed = False
        try:
            if self.picker.done():  # everything was already on disk
                self.finished.set()
            self.progress()
            await self.finished.wait()
            if self.fatal is not None:
                raise self.fatal
            completed = True
            self.progress(final=True)
        finally:
            await self.shutdown(workers, completed)

    def _worker_done(self, task):
        """A worker that dies on an unexpected error fails the download instead of leaving it to stall."""
        if task.cancelled() or task.exception() is None:
            return
        self.log(f"internal error: {type(task.exception()).__name__}")
        self.fail(DmError("internal error while downloading"))

    async def shutdown(self, workers, completed):
        """Stop every task and connection, then tell the tracker (briefly: the user is waiting)."""
        everything = workers + list(self.background) + [c.task for c in self.conns.values() if c.task]
        everything += [c.server for c in self.conns.values() if c.server]
        for task in everything:
            task.cancel()
        for conn in list(self.conns.values()):
            conn.close()
        await asyncio.gather(*everything, return_exceptions=True)
        try:
            placed = False
            if completed and self.on_complete is not None:
                self.on_complete()  # the file is final from here: nothing below may undo it
                placed = True
            try:
                await asyncio.wait_for(self.farewell(completed), 5 if completed else 1)
            except asyncio.CancelledError:
                if not placed:
                    raise
            except Exception:
                pass
        finally:
            self.pool.shutdown(wait=False, cancel_futures=True)
            self.reader_pool.shutdown(wait=False, cancel_futures=True)


async def run_session(meta, storage, progress, handle, opts, log=None, on_complete=None, have=(), out=None):
    """Run one session; `out["session"]` is set at the start so the caller can ask what was verified."""
    s = Session(meta, storage, progress, handle, opts, log, on_complete, have)
    if out is not None:
        out["session"] = s
    await s.run()


def _drive(meta, storage, progress, handle, opts, log, on_complete, have=(), out=None):
    loop = asyncio.new_event_loop()
    try:
        task = loop.create_task(run_session(meta, storage, progress, handle, opts, log, on_complete, have, out))
        handle.attach(loop, task)
        try:
            loop.run_until_complete(task)
        except asyncio.CancelledError:
            raise Cancelled() from None
    finally:
        left = [t for t in asyncio.all_tasks(loop) if not t.done()]
        for t in left:
            t.cancel()
        if left:
            loop.run_until_complete(asyncio.gather(*left, return_exceptions=True))
        loop.close()  # no shutdown_default_executor(): a blocked lookup must not delay the exit


def _cannot_read(exc):
    """Keep the partial: no torrent-chosen path in the text."""
    return DmError(f"cannot read the partial download: {exc.strerror or type(exc).__name__}", keep=True)


def _open_storage(meta, specs, tmp, target, handle, resume, note):
    """(storage, resumed): the partial left by an earlier run when it is exactly right, else a new one."""
    def new():
        return Storage(tmp, specs, meta.piece_length, meta.total, single=not meta.multi)

    if not resume:
        resume_mod.discard(tmp)
    elif resume_mod.exists(tmp):
        storage = new()
        try:
            storage.open_existing()
        except LayoutMismatch:
            resume_mod.discard(tmp)
            note("cannot resume: starting over")
        except OSError as e:  # not a mismatch: the data may be fine, so it stays
            raise _cannot_read(e) from None
        else:
            return storage, True
    with handle.lock:  # an abandoned worker never creates the temp item after an abort
        if handle.aborted:
            raise Cancelled()
        if meta.multi:
            os.mkdir(tmp)  # the folder becomes the final one: normal permissions, not 0700
    storage = new()
    try:
        storage.prepare()
    except PrepareError as e:  # same wording as a folder we cannot create
        raise DmError(f"cannot write to {target.folder}: {e.reason}") from None
    return storage, False


def run_torrent(path, target, progress, handle=None, opts=None, log=None, *, resume=True, note=None):
    """Download the torrent at path into target.folder. Returns (final path, bytes, number of files).

    An interrupted download keeps its partial (`.<name>.<key>.dm-part`); the next run of the same torrent
    re-checks every piece on disk and fetches the rest. `resume=False` throws an old partial away.
    """
    handle = handle or TorrentHandle()
    opts = opts or Options()
    say = note or (lambda text: None)
    note = lambda text: handle.emit(say, text)  # noqa: E731 - silent once aborted, like progress
    meta = metainfo.load(path)
    specs = layout.plan_files(meta)
    base = target.fixed_name or layout.item_name(meta)
    p = resume_mod.paths(target.folder, base, resume_mod.torrent_key(meta.info_hash, target))
    tmp = p.part
    lock = resume_mod.Lock(p.lock)
    lock.acquire()
    storage = None
    keep = False
    out = {}
    try:
        try:
            storage, resumed = _open_storage(meta, specs, tmp, target, handle, resume, note)
            have = ()
            if resumed:
                try:
                    have = recheck(storage, meta, progress, handle)
                except OSError as e:
                    if handle.aborted:
                        raise Cancelled() from None
                    raise _cannot_read(e) from None
                done_bytes = sum(storage.piece_size(i) for i in have)
                if done_bytes:
                    note(f"resuming from {human_size(done_bytes)} of {human_size(meta.total)}")
            done = []

            def place():
                storage.close()
                with handle.lock:
                    if handle.aborted:
                        raise Cancelled()
                    move = place_dir if meta.multi else place_file
                    try:
                        done.append(move(tmp, target.folder, base))
                    except DmError as e:  # every piece is on disk: a rerun can place it
                        raise DmError(str(e), keep=True) from None
                    except OSError as e:
                        raise DmError(f"cannot write to {target.folder}: {e.strerror or type(e).__name__}",
                                      keep=True) from None
                    handle.placed = True

            _drive(meta, storage, progress, handle, opts, log, place, have, out)
            final = done[0]
        except OSError as e:  # never show the raw error: it carries paths chosen by the torrent
            if handle.aborted:
                raise Cancelled() from None
            raise DmError(f"cannot write to {target.folder}: {e.strerror or type(e).__name__}",
                          keep=resume_mod.is_quota_error(e)) from None
        except DmError:
            if handle.aborted:
                raise Cancelled() from None
            raise
    except Cancelled:
        keep = True
        raise
    except DmError as e:
        # a retryable failure keeps the partial only when it holds something: a verified piece, this run or before
        s = out.get("session")
        keep = e.keep and (s is None or s.picker.verified_bytes() > 0 or s.picker.done())
        raise
    finally:
        if storage is not None:
            storage.close()
        try:
            if not handle.placed and not keep:
                resume_mod.discard(tmp)
        finally:
            lock.release(remove=True)
    return final, meta.total, len(meta.files)
