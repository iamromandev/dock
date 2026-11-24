"""One connection to a peer: handshake, messages, request pipeline, and serving verified blocks.

Outbound only. We serve a peer only while it is interested and the choker has unchoked it, and only blocks of
pieces we have verified.
"""

import asyncio
import collections

from . import metadata, stats, wire
from .picker import BadBlock

MAX_QUEUED = 128  # requests a peer may have waiting; one more and it is dropped
MAX_META_QUEUED = 8  # metadata blocks a peer may have waiting; one more and it is dropped
MAX_EARLY_HAVES = 8192  # HAVE messages kept while the piece count is unknown; later ones are ignored
PEX_INTERVAL = 60  # seconds between two ut_pex messages taken from one peer


class PeerConn:
    def __init__(self, session, addr, number=0):
        self.s = session
        self.tag = f"peer #{number}"  # what a log says about it: never the address, which only the peers table shows
        self.addr = addr  # (host, port): also the peer's key in the picker
        self.peer_id = None  # from its handshake
        self.client = None  # the `v` of its extension handshake
        self.down = stats.Rate(session.clock)  # block bytes it sends us, per second
        self.up = stats.Rate(session.clock)  # block bytes we send it, per second
        self.task = None
        self.writer = None
        self.handshaken = False
        self.choked = True  # they choke us until they say otherwise
        self.interested = False  # we told them we want pieces
        self.last_block = None
        self.closed = False
        self.am_choking = True  # we choke them until the choker says otherwise
        self.peer_interested = False
        self.received = 0  # block bytes they sent us: what the choker ranks by
        self.mark = 0
        self.queue = collections.deque()  # (index, begin, length) they asked for
        self.work = asyncio.Event()
        self.server = None
        self.inflight = None  # the block being read for them, and whether they cancelled it meanwhile
        self.cancelled = False
        self.served = 0  # blocks sent to them
        self.epoch = 0  # counts our chokes: a read begun under an older one is stale
        self.ext = False  # both sides speak the extension protocol (BEP 10)
        self.their_ut = None  # the id they want our ut_metadata messages under
        self.ext_hs = False  # their extension handshake has arrived
        self.their_pex = None  # the id they send ut_pex under; None: they did not list it
        self.last_pex = None  # when their last ut_pex message was taken: one per PEX_INTERVAL
        self.early_bitfield = None  # raw, until the piece count is known
        self.early_haves = set()
        self.meta_queue = collections.deque()  # metadata blocks they asked us for

    # sending

    def send(self, data):
        if self.writer is not None and not self.closed:
            self.writer.write(data)

    def update_interest(self):
        if self.s.picker is None:
            return
        want = self.s.picker.interesting(self.addr)
        if want != self.interested:
            self.interested = want
            self.send(wire.msg(wire.INTERESTED if want else wire.NOT_INTERESTED))

    def set_choke(self, choke):
        if choke == self.am_choking:
            return
        self.am_choking = choke
        if choke:
            self.epoch += 1
            self.queue.clear()  # a choked peer's requests are void
        self.send(wire.msg(wire.CHOKE if choke else wire.UNCHOKE))

    def refill(self):
        """Keep the request pipeline full while they have us unchoked."""
        if self.choked or not self.handshaken or self.closed or self.s.meta is None:
            return
        room = self.s.opts.pipeline - self.s.picker.outstanding(self.addr)
        for req in self.s.picker.next_requests(self.addr, room):
            self.send(wire.request(*req))

    # the connection

    async def run(self):
        s = self.s
        opts = s.opts
        try:
            wait = opts.connect_timeout
            reader, self.writer = await asyncio.wait_for(asyncio.open_connection(*self.addr), wait)
            self.writer.write(wire.handshake(s.info_hash, s.peer_id))
            peer_id, reserved = await asyncio.wait_for(wire.read_handshake(reader, s.info_hash), wait)
            if peer_id == s.peer_id:  # we dialed ourselves
                return
            self.handshaken = True
            self.peer_id = peer_id
            s.log(f"{self.tag} connected")
            self.ext = wire.supports_extensions(reserved)
            if self.ext:
                self.send(wire.extended(0, metadata.ext_handshake(self.metadata_size(), self.s.pex_on())))
            if s.picker is not None and s.picker.verified_bytes():
                self.send(wire.bitfield(s.bitfield()))
            loop = asyncio.get_running_loop()
            self.server = asyncio.ensure_future(self.serve())
            while True:
                npieces = len(s.meta.hashes) if s.meta is not None else None
                got = await asyncio.wait_for(wire.read_message(reader, npieces), opts.idle_timeout)
                now = loop.time()
                if (self.last_block is not None and s.picker is not None and s.picker.outstanding(self.addr)
                        and now - self.last_block > opts.idle_timeout):
                    raise asyncio.TimeoutError  # keeps the line open but never sends what we asked for
                if got is not None:
                    self.on_message(*got, now)
                await asyncio.wait_for(self.writer.drain(), opts.idle_timeout)
        except asyncio.CancelledError:
            raise
        except (BadBlock, ValueError) as e:  # the peer broke the protocol: never talk to it again
            s.banned.add(self.addr)
            s.log(f"{self.tag} dropped: {type(e).__name__}")
        except (OSError, EOFError, asyncio.TimeoutError) as e:
            s.log(f"{self.tag} lost: {type(e).__name__}")
        finally:
            self.close()
            self.report_served()
            s.peer_closed(self)

    async def serve(self):
        """Answer queued requests one block at a time; the read runs off the event loop."""
        s = self.s
        loop = asyncio.get_running_loop()
        try:
            while True:
                while not self.queue and not self.meta_queue:
                    self.work.clear()
                    await self.work.wait()
                if self.meta_queue:  # a few kilobytes a block, from memory: ahead of the disk reads
                    self.send_metadata_block(self.meta_queue.popleft())
                    await asyncio.wait_for(self.writer.drain(), s.opts.idle_timeout)
                    continue
                self.inflight = self.queue.popleft()
                self.cancelled = False
                epoch = self.epoch
                index, begin, length = self.inflight
                data = await loop.run_in_executor(s.reader_pool, s.storage.read_block, index, begin, length)
                self.inflight = None
                if self.closed or self.am_choking or self.cancelled or epoch != self.epoch:
                    continue
                self.send(wire.piece(index, begin, data))
                self.served += 1
                s.sent_block(self, len(data))
                await asyncio.wait_for(self.writer.drain(), s.opts.idle_timeout)  # a peer that never reads
        except asyncio.CancelledError:
            raise
        except (OSError, ValueError, RuntimeError, asyncio.TimeoutError) as e:  # ends this peer only
            s.log(f"{self.tag} not served: {type(e).__name__}")
            self.close()
            if self.task is not None and self.task is not asyncio.current_task():
                self.task.cancel()

    def report_served(self):
        if self.served:
            self.s.log(f"{self.tag} served {self.served} blocks")

    def metadata_size(self):
        """What we advertise as metadata_size: the size once the metadata is verified, else nothing."""
        return len(self.s.metadata) if self.s.metadata is not None else None

    def send_metadata_block(self, piece):
        data = self.s.metadata
        if data is None or self.their_ut is None:
            return
        block = data[piece * metadata.BLOCK:(piece + 1) * metadata.BLOCK]
        self.send(metadata.data_msg(self.their_ut, piece, len(data), block))

    def close(self):
        self.closed = True
        self.queue.clear()
        self.meta_queue.clear()
        if self.server is not None:
            self.server.cancel()
        if self.writer is not None:
            self.writer.close()

    def request_metadata(self, now):
        """Ask for the next metadata block this peer should supply (metadata mode only)."""
        s = self.s
        if s.meta is not None or s.assembler is None or self.their_ut is None or self.closed:
            return
        piece = s.assembler.next_request(self.addr, now)
        if piece is not None:
            self.send(metadata.request_msg(self.their_ut, piece))

    def on_extended(self, payload, now):
        s = self.s
        ext_id, body = payload[0], payload[1:]
        if ext_id == 0:
            self.ext_hs = True
            try:
                info = metadata.parse_ext_handshake(body)
            except ValueError:  # an odd handshake only means no usable extension; the peer may be fine
                self.their_ut = self.their_pex = None
                return
            self.their_ut = info.ut_metadata
            self.their_pex = info.ut_pex
            self.client = info.client
            if s.meta is None and info.ut_metadata and info.metadata_size is not None:
                if not s.assembler.offer(self.addr, info.metadata_size):
                    raise ValueError("bad metadata size")
                self.request_metadata(now)
        elif ext_id == metadata.OUR_UT_METADATA:
            try:
                parsed = metadata.parse_metadata_msg(body)
            except ValueError:
                if s.meta is None:  # we are fetching metadata with it: a bad message is a protocol break
                    raise
                return  # a download that no longer needs it: whatever they say about metadata is ignored
            self.on_metadata_message(*parsed, now)
        elif ext_id == metadata.OUR_UT_PEX:
            self.on_pex(body, now)
        # other extensions are ignored

    def on_pex(self, body, now):
        """A ut_pex message: its added peers join the pending ones. We never send one. Whatever is wrong with
        it (private torrent, too early, from a peer that never listed ut_pex, too often, malformed) it is ignored
        and the peer stays. The 60 s slot is used up before parsing, on purpose: a malformed message costs the
        peer its slot, so a hostile peer cannot make us parse every message."""
        s = self.s
        if not s.pex_on() or not self.ext_hs or self.their_pex is None:
            return
        if self.last_pex is not None and now - self.last_pex < PEX_INTERVAL:
            return
        self.last_pex = now
        try:
            peers = metadata.parse_pex(body, s.opts.dht_allow_loopback, s.opts.dht_allow_private)
        except ValueError:
            return
        s.add_peers(peers, from_pex=True)

    def on_metadata_message(self, kind, piece, total, data, now):
        s = self.s
        if kind == metadata.REQUEST:
            if self.their_ut is None:
                raise ValueError("metadata request before an extension handshake")
            if s.metadata is None:  # not verified yet: nothing to give
                self.send(metadata.reject_msg(self.their_ut, piece))
            elif piece >= -(-len(s.metadata) // metadata.BLOCK):
                raise ValueError("bad metadata request")
            elif len(self.meta_queue) >= MAX_META_QUEUED:
                raise ValueError("too many metadata requests")
            else:
                self.meta_queue.append(piece)
                self.work.set()
        elif kind == metadata.DATA:
            if s.meta is not None or s.assembler.metadata is not None:
                return  # a late copy of what we already have
            whole = s.assembler.received(self.addr, piece, data, total)
            s.last_progress = now
            if self.addr in s.assembler.banned:
                s.drop_metadata_banned()
                raise ValueError("metadata did not match its hash")
            if whole is not None:
                s.metadata_complete(whole)
            else:
                s.drop_metadata_banned()
                self.request_metadata(now)
        elif kind == metadata.REJECT and s.assembler is not None:
            s.assembler.rejected(self.addr, piece)

    def enter_download(self):
        """The metadata is known: replay what the peer sent before, then behave as in a normal download."""
        s = self.s
        if not self.handshaken or self.closed:
            return
        try:
            if self.ext:  # they may have asked before; now they can
                self.send(wire.extended(0, metadata.ext_handshake(self.metadata_size(), self.s.pex_on())))
            if s.picker.verified_bytes():
                self.send(wire.bitfield(s.bitfield()))
            npieces = len(s.meta.hashes)
            if self.early_bitfield is not None:
                if len(self.early_bitfield) != -(-npieces // 8):
                    raise ValueError("bad size for bitfield")
                s.picker.peer_bitfield(self.addr, wire.parse_bitfield(self.early_bitfield, npieces))
            for index in sorted(self.early_haves):
                s.picker.peer_has(self.addr, index)
        except ValueError as e:  # the peer broke the protocol: never talk to it again
            s.banned.add(self.addr)
            s.log(f"{self.tag} dropped: {type(e).__name__}")
            self.close()
            if self.task is not None and self.task is not asyncio.current_task():
                self.task.cancel()
            return
        self.early_bitfield, self.early_haves = None, set()
        if not self.choked:
            self.last_block = asyncio.get_running_loop().time()
        if self.peer_interested:
            s.interest_changed(self)
        self.update_interest()
        self.refill()

    def on_early_message(self, mid, payload, now):
        """A message while the piece count is unknown: keep what is cheap, drop what makes no sense."""
        if mid == wire.CHOKE:
            self.choked = True
        elif mid == wire.UNCHOKE:
            self.choked = False
        elif mid == wire.HAVE:
            if len(self.early_haves) < MAX_EARLY_HAVES:
                self.early_haves.add(wire.parse_have(payload))
        elif mid == wire.BITFIELD:
            self.early_bitfield = payload
        elif mid == wire.INTERESTED:
            self.peer_interested = True
        elif mid == wire.NOT_INTERESTED:
            self.peer_interested = False
        elif mid in (wire.REQUEST, wire.PIECE, wire.CANCEL):
            raise ValueError("block message before the metadata")
        elif mid == wire.EXTENDED:
            self.on_extended(payload, now)

    def on_message(self, mid, payload, now):
        s = self.s
        picker = s.picker
        if s.meta is None:
            return self.on_early_message(mid, payload, now)
        if mid == wire.EXTENDED:
            return self.on_extended(payload, now)
        if mid == wire.CHOKE:
            self.choked = True
            picker.release(self.addr)
        elif mid == wire.UNCHOKE:
            self.choked = False
            self.last_block = now  # the clock for a peer that unchokes and then sends nothing
            self.update_interest()
            self.refill()
        elif mid == wire.HAVE:
            picker.peer_has(self.addr, wire.parse_have(payload))
            self.update_interest()
            self.refill()
        elif mid == wire.BITFIELD:
            npieces = len(s.meta.hashes)
            if len(payload) != -(-npieces // 8):  # read just before the switch, while the count was unknown
                raise ValueError("bad size for bitfield")
            picker.peer_bitfield(self.addr, wire.parse_bitfield(payload, npieces))
            self.update_interest()
            self.refill()
        elif mid == wire.PIECE:
            index, begin, data = wire.parse_piece(payload)
            self.last_block = now
            self.received += len(data)
            s.got_block(self, len(data))
            whole = picker.block_received(self.addr, index, begin, data)
            for pid, i, b, n in picker.drain_cancels():
                other = s.conns.get(pid)
                if other is not None:
                    other.send(wire.cancel(i, b, n))
            if whole is not None:
                s.piece_complete(index, whole)  # its check refills every connection when it ends
            else:
                self.refill()
        elif mid == wire.INTERESTED:
            self.peer_interested = True
            s.interest_changed(self)
        elif mid == wire.NOT_INTERESTED:
            self.peer_interested = False
            s.interest_changed(self)
        elif mid == wire.REQUEST:
            self.on_request(*wire.parse_request(payload))
        elif mid == wire.CANCEL:
            block = wire.parse_request(payload)
            try:
                self.queue.remove(block)
            except ValueError:
                if block == self.inflight:
                    self.cancelled = True
        # unknown ids are ignored

    def on_request(self, index, begin, length):
        if self.am_choking:  # asked before our choke reached it, or without an unchoke: no answer
            return
        s = self.s
        if (index >= len(s.meta.hashes) or index not in s.picker.finished or not 0 < length <= wire.MAX_BLOCK
                or begin + length > s.picker.piece_size(index)):
            raise ValueError("bad request")
        if len(self.queue) >= MAX_QUEUED:
            raise ValueError("too many requests")
        self.queue.append((index, begin, length))
        self.work.set()
