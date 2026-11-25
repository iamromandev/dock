"""A torrent download: tracker loop, peer pool, piece checking, stall timers, and run_torrent.

Runs one asyncio loop inside dm's worker thread. Nothing listens: every connection is outbound. While the
download runs, verified pieces are served to interested peers the choker has unchoked; when it ends, every
connection closes (no seeding).
"""

import asyncio
import collections
import hashlib
import itertools
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from .. import Cancelled, resume as resume_mod
from ..fmt import Throttle, human_size
from ..target import DmError
from . import layout, metainfo, select, stats, tracker, wire
from .handle import TorrentHandle
from .choker import choose
from .dht.client import DhtClient
from .peer import PeerConn
from .metadata import MetadataAssembler
from .picker import Picker
from .place import place_dir, place_file
from .recheck import recheck
from .storage import LayoutMismatch, PrepareError, Storage

REPORTED_PORT = 6881  # what we tell trackers, though nothing listens
MAX_PENDING = 1000
MAX_PEX_PENDING = 200  # likewise for PEX: connected peers must not starve the tracker's
MAX_DHT_PENDING = 500  # of those, the most the DHT may fill: one hostile node must not starve the rest
FINDING_PEERS = "finding peers (DHT)"
DHT_BOOTSTRAP = (("router.bittorrent.com", 6881), ("dht.transmissionbt.com", 6881),
                 ("router.utorrent.com", 6881))
RETRY_AFTER = 30  # seconds before dialing the same peer again
DHT_TICK = 5  # seconds between the checks of when the next DHT lookup is due
DHT_EVERY = 300  # seconds between DHT lookups of a download that has enough peers
DHT_RETRY = 60  # ... and the least between two when it has fewer than DHT_FEW connections
DHT_FEW = 5
DHT_CLOSE = 1  # seconds the DHT socket may take to close on the way out: a cancel must stay quick


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
    dht: bool = True
    pex: bool = True  # accept ut_pex (BEP 11) unless the torrent is private; independent of `dht`
    dht_bootstrap: tuple = DHT_BOOTSTRAP
    dht_cache: Path | None = None
    # tests only: nothing reads these from the command line, the manifest or the environment. They widen the
    # addresses the DHT will talk to, so production must never set them.
    dht_allow_private: bool = False
    dht_allow_loopback: bool = False


def _span(seconds):
    if seconds % 60 == 0:
        minutes = int(seconds // 60)
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    return f"{seconds:g} seconds"


class Session:
    """One download. With `meta=None` it starts in metadata mode (a magnet link): it finds peers by the
    info-hash, fetches the info dictionary from them, calls `on_metadata(info_bytes)` in an executor to get
    `(meta, storage, have)` and then carries on as a normal download over the same connections."""

    def __init__(self, meta, storage, progress, handle, opts, log=None, on_complete=None, have=(),
                 *, info_hash=None, extra_peers=(), trackers=None, on_metadata=None, metadata=None,
                 on_stats=None, clock=time.monotonic):
        self.meta = meta
        self.storage = storage
        self._info_hash = info_hash
        self._tiers = ((tuple(trackers),) if trackers else ())
        self.extra_peers = tuple(extra_peers)
        self.on_metadata = on_metadata
        self.metadata = metadata  # the verified info dictionary: set once known
        self.assembler = MetadataAssembler(self.info_hash) if meta is None else None
        self.setup = None  # the task that turns fetched metadata into a download
        self.report = progress
        self.handle = handle
        self.opts = opts
        self._log = log
        self.on_complete = on_complete  # places the finished download, before the tracker is told
        self.peer_id = b"-DM0800-" + os.urandom(12)
        self.picker = None
        self._file_progress = None
        self.selected_total = meta.total if meta is not None else None  # bytes of the picked files
        self._show_files = True
        if meta is not None:
            self._start_picker(meta, have)  # resumed: these pieces are ours before any peer connects
        self.conns = {}  # (host, port) -> PeerConn, connecting or connected
        self.banned = set()
        self.pending = collections.deque()
        self.queued = set()
        self.from_dht = set()  # queued peers only the DHT named: dropped if private
        self.from_pex = set()  # likewise for PEX
        self.private = meta is not None and meta.private
        self.dht_on = opts.dht and not self.private
        self.dht = None  # the DhtClient, while there is one
        self.dht_task = None
        self.dht_lookups = 0
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
        self.clock = clock
        self._view_failed = False
        self._peer_numbers = itertools.count(1)  # a log names a peer by the order it was dialed
        self.on_stats = on_stats  # called with stats() on each progress tick, if the caller wants a live view
        self.down = stats.Rate(clock)
        self.up = stats.Rate(clock)
        self.downloaded = 0  # block bytes received and sent this run: the ratio
        self.uploaded = 0

    def _start_picker(self, meta, have):
        """The picker for meta, with the per-file counting wired in before the pieces on disk are marked."""
        chosen = self.storage.wanted if self.storage is not None else None  # None: every file
        specs = None
        wanted = None
        if chosen is not None:
            specs = self.storage.files
            wanted = select.wanted_pieces(specs, chosen, meta.piece_length)
            self.selected_total = sum(specs[i].length for i in chosen)
        else:
            self.selected_total = meta.total
        self.picker = Picker(len(meta.hashes), meta.piece_length, meta.total, wanted=wanted)
        if len(meta.files) > 1:  # one file: the total bar says it all
            # the plan, once
            specs = specs or (self.storage.files if self.storage is not None else layout.plan_files(meta))
            self._file_progress = stats.FileProgress(specs, meta.piece_length, meta.total, chosen)
            self.picker.on_verified = self._file_progress.piece_done
            self._show_files = chosen is None or len(chosen) > 1
        self.picker.mark_verified(have)

    @property
    def info_hash(self):
        return self.meta.info_hash if self.meta is not None else self._info_hash

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

    # metadata

    def metadata_complete(self, info_bytes):
        """The assembled metadata hashed right: build the download in an executor, then switch to it."""
        if self.setup is None:
            self.setup = asyncio.ensure_future(self._setup(info_bytes))
            self.background.add(self.setup)
            self.setup.add_done_callback(self.background.discard)

    async def _setup(self, info_bytes):
        loop = asyncio.get_running_loop()
        try:
            meta, storage, have = await loop.run_in_executor(None, self.on_metadata, info_bytes)
        except asyncio.CancelledError:
            raise
        except BaseException as e:  # a bad torrent or a write error: the whole download fails
            self.fail(e)
            return
        self.metadata = info_bytes
        self.storage = storage
        self._start_picker(meta, have)
        self.meta = meta  # last: from here every connection is in download mode
        if meta.private:
            self._go_private()
        self.last_progress = loop.time()
        for conn in list(self.conns.values()):
            conn.enter_download()
        if self.picker.done():
            self.finished.set()
        self.progress()
        for conn in list(self.conns.values()):
            conn.refill()

    def drop_metadata_banned(self):
        """Close every connection whose blocks were part of a set that failed the hash."""
        for addr in self.assembler.banned:
            self.banned.add(addr)
            conn = self.conns.get(addr)
            if conn is not None and not conn.closed:
                conn.close()
                if conn.task is not None and conn.task is not asyncio.current_task():
                    conn.task.cancel()

    # stats

    def got_block(self, conn, n):
        self.downloaded += n
        self.down.add(n)
        conn.down.add(n)

    def sent_block(self, conn, n):
        self.uploaded += n
        self.up.add(n)
        conn.up.add(n)

    def stats(self):
        """The numbers behind the live view; every peer address in it is for the host's screen only."""
        live = [c for c in self.conns.values() if c.handshaken and not c.closed]
        down_rate = self.down.value()
        done = self.selected_done() if self.picker is not None else 0
        left = self.selected_total - done if self.meta is not None else None
        peers = sorted(((c.addr, stats.client_name(c.peer_id, c.client), c.down.value(), c.up.value())
                        for c in live), key=lambda p: p[2], reverse=True)
        return {
            "files": (self._file_progress.report()
                      if self._file_progress is not None and self._show_files else []),
            "down_rate": down_rate,
            "up_rate": self.up.value(),
            "peers_connected": len(live),
            "peers_known": max(len(live), len(self.conns) + len(self.pending)),
            "eta_s": left / down_rate if left and down_rate >= 1 else None,
            "ratio": self.uploaded / self.downloaded if self.downloaded else None,  # per run
            "peers": peers,
        }

    # progress

    def selected_done(self):
        """Verified bytes of the picked files: a piece shared with an unpicked file counts only its picked
        part."""
        if self._file_progress is not None:
            return self._file_progress.done()
        return self.picker.verified_bytes()

    def connected(self):
        return sum(1 for c in self.conns.values() if c.handshaken)

    def _emit(self, fn, *args):
        """Report to the caller's view; a failure there (formatting, the host's pipe) must not stop the
        download: it would end the piece that is being finished before its refill. Only the class is
        logged, and once."""
        try:
            self.handle.emit(fn, *args)
        except Exception as e:
            if not self._view_failed:
                self._view_failed = True
                self.log(f"live view failed: {type(e).__name__}")

    def progress(self, final=False):
        self._progress_bar(final)
        if self.on_stats is not None and self.meta is not None and self.picker is not None:
            self._emit(self.on_stats, self.stats, final)  # the receiver decides whether an update is due

    def _finding_peers(self):
        """No peer yet, no tracker has answered, and the DHT is looking: say so."""
        if self.picker is not None and self.picker.done():
            return False
        return self.dht_on and self.tracker_url is None and not self.connected()

    def _progress_bar(self, final):
        if self.meta is None:
            n = self.connected()
            text = f"fetching metadata · {n} peer{'s' if n != 1 else ''}"
            if self._finding_peers():
                text = FINDING_PEERS
            if self.sent != (None, text) and self.throttle.ready():
                self.sent = (None, text)
                self._emit(self.report, None, text)
            return
        done, total = self.selected_done(), self.selected_total
        pct = 100 if final else min(99, done * 100 // total) if total else 0
        n = self.connected()
        text = f"{human_size(done)} of {human_size(total)} · {n} peer{'s' if n != 1 else ''}"
        if self._finding_peers() and not final:
            text = FINDING_PEERS
        if self.sent == (pct, text):
            return
        if self.throttle.ready(final=final):
            self.sent = (pct, text)
            self._emit(self.report, pct, text)

    # peers

    def peer_closed(self, conn):
        if self.conns.get(conn.addr) is conn:
            del self.conns[conn.addr]
        if self.assembler is not None:
            self.assembler.peer_gone(conn.addr)
        if self.picker is not None:
            self.picker.peer_gone(conn.addr)
        self.wake.set()
        for other in list(self.conns.values()):
            other.refill()

    def add_peers(self, peers, from_dht=False, from_pex=False):
        shuffled = list(peers)
        random.shuffle(shuffled)
        for addr in shuffled:
            if (len(self.pending) >= MAX_PENDING or (from_dht and len(self.from_dht) >= MAX_DHT_PENDING)
                    or (from_pex and len(self.from_pex) >= MAX_PEX_PENDING)):
                break
            if addr in self.banned or addr in self.conns:
                continue
            if addr in self.queued:
                if not (from_dht or from_pex):
                    self.from_dht.discard(addr)  # the tracker names it too: it stays if the DHT or PEX goes
                    self.from_pex.discard(addr)
                continue
            self.queued.add(addr)
            self.pending.append(addr)
            if from_dht:
                self.from_dht.add(addr)
            if from_pex:
                self.from_pex.add(addr)
        self.wake.set()

    def _dht_peers(self, peers):
        if not self.dht_on:
            return  # a reply that came in while the DHT was being stopped
        self.add_peers(peers, from_dht=True)

    def pex_on(self):
        return self.opts.pex and not self.private

    def _go_private(self):
        """The metadata says the torrent is private: no DHT or PEX from here, and their pending peers are dropped."""
        self.private = True
        self.dht_on = False
        if self.dht is not None:
            self.dht.on_peers = None  # at once: no late reply may add a peer while the task unwinds
        if self.dht_task is not None:
            self.dht_task.cancel()  # its own cleanup closes the socket
            self.dht_task = None
        self.dht = None  # nothing of this run is kept: a private torrent leaves no trace in the node cache
        named = self.from_dht | self.from_pex
        if named:
            self.pending = collections.deque(a for a in self.pending if a not in named)
            self.queued -= named
            self.from_dht = set()
            self.from_pex = set()

    async def connector(self):
        loop = asyncio.get_running_loop()
        while True:
            while len(self.conns) < self.opts.max_peers and self.pending:
                addr = self.pending.popleft()
                self.queued.discard(addr)
                self.from_dht.discard(addr)
                self.from_pex.discard(addr)
                if addr in self.banned or addr in self.conns:
                    continue
                if loop.time() - self.last_try.get(addr, -RETRY_AFTER) < RETRY_AFTER:
                    continue
                self.last_try[addr] = loop.time()
                conn = PeerConn(self, addr, next(self._peer_numbers))
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
        if self.meta is None:
            return  # nothing to serve before the metadata is known
        held = sum(1 for c in self.conns.values() if c.handshaken and not c.am_choking)  # interested or not
        if conn.peer_interested and conn.am_choking and held < self.opts.unchoke_slots + 1:
            conn.set_choke(False)

    def rechoke(self, rng=random):
        """Unchoke the best uploaders to us and one optimistic peer; choke everyone else."""
        if self.meta is None:
            return
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
        if self.meta is None:  # a leecher with something left, as far as the tracker can tell
            return tracker.Announce(self.info_hash, self.peer_id, REPORTED_PORT, 0, 0, 1, event)
        done = self.picker.verified_bytes()
        left = self.selected_total - self.selected_done()  # only the picked bytes are left
        return tracker.Announce(self.info_hash, self.peer_id, REPORTED_PORT, 0, done, left, event)

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
        source = self.meta.trackers if self.meta is not None else self._tiers
        tiers = [random.sample(list(t), len(t)) for t in source]
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
                if first and not self.extra_peers and not self.dht_on:
                    # every tracker failed on the first try: nothing else finds peers
                    self.fail(DmError(f"tracker error: {reason}", keep=True))
                    return
                self.log(f"tracker: {reason}")  # the same reason an error would show: no address in it
                wait = 60
            first = False
            await asyncio.sleep(wait)

    async def dht_loop(self):
        """Look for peers in the DHT now, then again every DHT_EVERY seconds, or DHT_RETRY when there are
        few connections, until the download ends. Nothing here may fail the download."""
        opts = self.opts
        client = self.dht = DhtClient(self.info_hash, opts.dht_bootstrap, opts.dht_cache, self.log,
                                      allow_loopback=opts.dht_allow_loopback,
                                      allow_private=opts.dht_allow_private)
        client.on_peers = self._dht_peers
        try:
            while True:
                if not (self.picker is not None and self.picker.done()):
                    found = []
                    try:
                        found = await client.lookup()
                    except Exception as e:
                        self.log(f"dht: lookup failed: {type(e).__name__}")
                    self.dht_lookups += 1
                    self.log(f"dht: lookup {self.dht_lookups}, {client.stats['nodes']} nodes, "
                             f"{len(found)} peers")
                last = self.clock()
                while True:
                    await asyncio.sleep(DHT_TICK)
                    wait = DHT_RETRY if self.connected() < DHT_FEW else DHT_EVERY
                    if self.clock() - last >= wait:
                        break
        finally:
            await client.close()

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
            if self.setup is not None and not self.setup.done():
                # the metadata is being turned into a download: the check of the partial reports itself
                continue
            now = loop.time()
            if self.meta is None:
                for conn in list(self.conns.values()):
                    conn.request_metadata(now)
            idle = now - self.last_progress
            if self.connected():
                if idle >= 2.5 * self.opts.stall_s:
                    span = _span(2.5 * self.opts.stall_s)
                    self.fail(DmError(f"stalled: no progress for {span}", keep=True))
                    return
            elif idle >= self.opts.stall_s:
                self.fail(DmError("no peers found", keep=True))
                return
            self.progress()

    # the whole thing

    async def run(self):
        loop = asyncio.get_running_loop()
        self.last_progress = loop.time()
        loops = [self.tracker_loop(), self.connector(), self.monitor(), self.choker()]
        dht_task = None
        if self.dht_on and not (self.picker is not None and self.picker.done()):
            dht_task = self.dht_task = asyncio.ensure_future(self.dht_loop())
        workers = [asyncio.ensure_future(c) for c in loops] + ([dht_task] if dht_task else [])
        for worker in workers:
            worker.add_done_callback(self._worker_done)
        completed = False
        try:
            if self.picker is not None and self.picker.done():  # everything was already on disk
                self.finished.set()
            self.add_peers(self.extra_peers)
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

    async def _close_dht(self):
        """Keep the nodes the DHT found for the next run, and free the socket, without ever failing or
        waiting long: the cache is written only when the DHT found peers."""
        client = self.dht
        if client is None:
            return
        try:
            if client.stats["peers"]:
                client.save_cache()
        except Exception:
            pass
        try:
            await asyncio.wait_for(client.close(), DHT_CLOSE)
        except Exception:
            pass

    async def shutdown(self, workers, completed):
        """Stop every task and connection, then tell the tracker (briefly: the user is waiting)."""
        everything = workers + list(self.background) + [c.task for c in self.conns.values() if c.task]
        everything += [c.server for c in self.conns.values() if c.server]
        for task in everything:
            task.cancel()
        for conn in list(self.conns.values()):
            conn.close()
        await asyncio.gather(*everything, return_exceptions=True)
        await self._close_dht()
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


async def run_session(meta, storage, progress, handle, opts, log=None, on_complete=None, have=(), out=None,
                      **kw):
    """Run one session; `out["session"]` is set at the start so the caller can ask what was verified."""
    s = Session(meta, storage, progress, handle, opts, log, on_complete, have, **kw)
    if out is not None:
        out["session"] = s
    await s.run()


def _drive(meta, storage, progress, handle, opts, log, on_complete, have=(), out=None, **kw):
    loop = asyncio.new_event_loop()
    try:
        task = loop.create_task(run_session(meta, storage, progress, handle, opts, log, on_complete, have,
                                            out, **kw))
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


def _open_storage(meta, specs, tmp, target, handle, resume, note, chosen=None):
    """(storage, resumed): the partial left by an earlier run when it is exactly right, else a new one."""
    def new():
        return Storage(tmp, specs, meta.piece_length, meta.total, single=not meta.multi, wanted=chosen)

    if not resume:
        resume_mod.discard(tmp)
    elif resume_mod.exists(tmp):
        # whatever the earlier run had picked, its files move to where this selection wants them
        storage = new()
        try:
            storage.open_existing()
            storage.reconcile()
        except LayoutMismatch:
            storage.close()
            resume_mod.discard(tmp)
            note("cannot resume: starting over")
        except OSError as e:  # not a mismatch: the data may be fine, so it stays
            storage.close()
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


class _Work:
    """What a run has put on disk, shared with the thread that sets up a magnet's download."""

    def __init__(self):
        self.lock = threading.Lock()  # held while that thread works
        self.closed = False  # the run is over: a late setup must not create anything
        self.meta = None
        self.base = None
        self.tmp = None
        self.storage = None
        self.opened = False  # the setup got as far as touching the partial
        self.resumed = False  # ... and found one from an earlier run


def _prepare(w, meta, specs, target, key, handle, resume, progress, note, chosen=None):
    """Open or create the partial for meta and re-check it. Returns (storage, pieces already on disk)."""
    specs = specs or layout.plan_files(meta)
    w.meta = meta
    w.base = target.fixed_name or layout.item_name(meta)
    w.tmp = resume_mod.paths(target.folder, w.base, key).part
    w.opened = True
    try:
        storage, resumed = _open_storage(meta, specs, w.tmp, target, handle, resume, note, chosen)
    except DmError as e:
        if e.keep:
            w.resumed = True  # refused or unreadable: the partial was there before this run, it stays
        raise
    w.storage = storage
    w.resumed = resumed
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
    return storage, have


def run_torrent(path, target, progress, handle=None, opts=None, log=None, *, resume=True, note=None,
                on_stats=None, selection=None):
    """Download the torrent at path into target.folder. Returns (final path, bytes, number of files).

    `selection` (select.Picks) limits it to the picked files; the bytes and the count are then theirs.

    An interrupted download keeps its partial (`.<name>.<key>.dm-part`); the next run of the same torrent
    re-checks every piece on disk and fetches the rest. `resume=False` throws an old partial away.
    """
    return _run(metainfo.load(path), None, target, progress, handle, opts, log, resume, note, on_stats,
                selection)[:3]


def run_magnet(magnet, target, progress, handle=None, opts=None, log=None, *, resume=True, note=None,
               on_stats=None, selection=None):
    """Download a magnet link (a magnet.Magnet) into target.folder.

    Returns (final path, bytes, number of files, the info dictionary bytes). The metadata is fetched from
    peers; everything else is as for run_torrent, and a partial from `.torrent` of the same info-hash resumes.
    """
    return _run(None, magnet, target, progress, handle, opts, log, resume, note, on_stats, selection)


class _Listed(Exception):
    """Raised by the metadata hook of list_magnet to end the session once the metadata is known."""

    def __init__(self, meta):
        super().__init__()
        self.meta = meta


def list_magnet(magnet, target, handle=None, opts=None, log=None, progress=None):
    """The Meta of a magnet link, for --list: nothing is downloaded, written or locked.

    A valid `.dm-meta` of an earlier run is used without contacting anyone; otherwise the session runs only
    until the metadata is verified (trackers, DHT and PEX as usual) and then stops. Nothing is stored, and a
    stored file that is not valid is left alone (never deleted). `progress(None, text)` reports the search."""
    handle = handle or TorrentHandle()
    opts = opts or Options()
    key = resume_mod.torrent_key(magnet.info_hash, target)
    mpath = resume_mod.meta_path(target.folder, key)
    stored = resume_mod.read_meta(mpath, magnet.info_hash, discard_bad=False)
    if stored is not None:
        try:
            return metainfo.from_info(stored, magnet.trackers)
        except DmError:  # hashes right but is no torrent we accept: ask the peers
            pass

    def on_metadata(info_bytes):  # in an executor
        raise _Listed(metainfo.from_info(info_bytes, magnet.trackers))

    try:
        _drive(None, None, progress or (lambda pct, text: None), handle, opts, log, None, (), None,
               info_hash=magnet.info_hash, extra_peers=magnet.peers, trackers=magnet.trackers,
               on_metadata=on_metadata)
    except _Listed as e:
        return e.meta
    except DmError:
        if handle.aborted:
            raise Cancelled() from None
        raise
    raise DmError("no metadata")  # unreachable: the session only ends through _Listed or an error


def _keep_after(e, meta, w, out):
    """Whether a retryable failure leaves the partial: only when it holds something, a verified piece (this
    run or before) or, for a magnet, a partial that was there before the run. A magnet run that never opened
    a partial (no metadata arrived) returns True whatever e.keep says: it created nothing, so there is
    nothing to delete, and what is in the folder is from an earlier run."""
    s = out.get("session")
    if meta is None and not w.opened:
        return True  # nothing created by this run, whatever e.keep says: leave an earlier run's files alone
    started = s is None or s.picker is None  # nothing was downloaded into it
    if meta is None and started and not w.resumed:
        return False  # this run's setup made it just now: it is empty
    return e.keep and (started or s.picker.verified_bytes() > 0 or s.picker.done())


def _stored_meta(mpath, magnet, target, key, resume):
    """(Meta, info bytes) from the metadata an earlier run of this magnet stored, else None. Without resume
    the stored metadata and the partial it belongs to are removed here, before anything else happens."""
    stored = resume_mod.read_meta(mpath, magnet.info_hash)
    if stored is None:
        resume_mod.discard(mpath)
        return None
    try:
        found = metainfo.from_info(stored, magnet.trackers)
        base = target.fixed_name or layout.item_name(found)
    except DmError:  # hashes right but is no torrent we accept: fetching it again would give the same
        resume_mod.discard(mpath)
        return None
    if not resume:
        resume_mod.discard(resume_mod.paths(target.folder, base, key).part, mpath)
        return None
    return found, stored


def _choose(meta, specs, selection):
    """The indexes of the picked files, as a set that is None when every file is picked."""
    chosen = select.resolve(specs, selection)
    return None if len(chosen) == len(specs) else chosen


def _run(meta, magnet, target, progress, handle, opts, log, resume, note, on_stats=None, selection=None):
    handle = handle or TorrentHandle()
    opts = opts or Options()
    say = note or (lambda text: None)
    note = lambda text: handle.emit(say, text)  # noqa: E731 - silent once aborted, like progress
    info_hash = meta.info_hash if meta is not None else magnet.info_hash
    specs = chosen = None
    if meta is not None:  # a bad pick fails here, before the folder or the network is touched
        specs = layout.plan_files(meta)
        chosen = _choose(meta, specs, selection)
    key = resume_mod.torrent_key(info_hash, target)
    lock = resume_mod.Lock(resume_mod.torrent_lock(target.folder, key))
    lock.acquire()
    mpath = resume_mod.meta_path(target.folder, key)  # a .torrent run never reads it, only cleans it up
    resume_mod.clean_temps(mpath)  # what a killed write of it left
    w = _Work()
    keep = False
    failure = None
    out = {}
    kw = {"on_stats": on_stats}
    try:
        try:
            have = ()
            if magnet is None and not resume:
                resume_mod.discard(mpath)  # with the partial: the same key may hold a magnet run's
            if magnet is not None:
                known = _stored_meta(mpath, magnet, target, key, resume)
                if known is not None:  # no second fetch: this is a plain torrent download from here on
                    meta, kw["metadata"] = known
                    specs = layout.plan_files(meta)
                    try:
                        chosen = _choose(meta, specs, selection)
                    except DmError as e:  # the stored metadata and the partial stay for the next try
                        raise DmError(str(e), keep=True) from None
            if meta is not None:
                _, have = _prepare(w, meta, specs, target, key, handle, resume, progress, note, chosen)
                if magnet is not None:
                    kw["extra_peers"] = magnet.peers
            else:
                def on_metadata(info_bytes):  # in an executor: the event loop keeps serving peers meanwhile
                    with w.lock:
                        if w.closed or handle.aborted:
                            raise Cancelled()
                        found = metainfo.from_info(info_bytes, magnet.trackers)
                        found_specs = layout.plan_files(found)
                        def store():  # a rerun reads it instead of asking peers; losing it costs only that
                            try:
                                resume_mod.write_meta(mpath, info_bytes)
                            except OSError as e:
                                if log is not None:
                                    log(f"could not store the metadata: {type(e).__name__}")

                        try:  # before anything is created
                            picked = _choose(found, found_specs, selection)
                        except DmError as e:
                            store()  # the metadata stays for the next try; no partial exists
                            raise DmError(str(e), keep=True) from None
                        storage, have = _prepare(w, found, found_specs, target, key, handle, resume, progress,
                                                 note, picked)
                        store()
                        return found, storage, have

                kw.update(info_hash=info_hash, extra_peers=magnet.peers, trackers=magnet.trackers,
                          on_metadata=on_metadata)
            done = []

            def place():
                with handle.lock:
                    if handle.aborted:  # before finish_layout: a kept partial must keep its side data
                        raise Cancelled()
                    try:  # under the lock: a cancel cannot land between the check and the change
                        w.storage.finish_layout()  # no side area, no folder without a picked file
                    except OSError as e:
                        raise DmError(f"cannot write to {target.folder}: {e.strerror or type(e).__name__}",
                                      keep=True) from None
                w.storage.close()
                with handle.lock:
                    if handle.aborted:
                        raise Cancelled()
                    # a cancel landing after finish_layout, while the storage closes (or a failing
                    # move), keeps a partial with no side data any more: a resume must tolerate that, a
                    # recheck already skips absent spans
                    move = place_dir if w.meta.multi else place_file
                    try:
                        done.append(move(w.tmp, target.folder, w.base))
                    except DmError as e:  # every piece is on disk: a rerun can place it
                        raise DmError(str(e), keep=True) from None
                    except OSError as e:
                        raise DmError(f"cannot write to {target.folder}: {e.strerror or type(e).__name__}",
                                      keep=True) from None
                    handle.placed = True

            _drive(meta, w.storage, progress, handle, opts, log, place, have, out, **kw)
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
        failure = e
        keep = _keep_after(e, meta, w, out)
        raise
    finally:
        with w.lock:  # a magnet's setup thread may still be running; after this it creates nothing
            w.closed = True
            if failure is not None and meta is None:
                keep = _keep_after(failure, meta, w, out)  # the setup may have opened a partial meanwhile
        if w.storage is not None:
            w.storage.close()
        try:
            if not handle.placed and not keep and w.tmp is not None:
                resume_mod.discard(w.tmp)
            if handle.placed or not keep:  # it goes with the partial, never outlives it
                resume_mod.discard(mpath)
        finally:
            lock.release(remove=True)
    session = out.get("session")
    if session is not None and session.storage is not None and session.storage.wanted is not None:
        wanted = session.storage.wanted
        picked_bytes, picked_files = session.selected_total, len(wanted)
    else:
        picked_bytes, picked_files = w.meta.total, len(w.meta.files)
    return final, picked_bytes, picked_files, (session.metadata if session is not None else None)
