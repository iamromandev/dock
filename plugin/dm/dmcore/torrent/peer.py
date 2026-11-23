"""One connection to a peer: handshake, messages, request pipeline, and serving verified blocks.

Outbound only. We serve a peer only while it is interested and the choker has unchoked it, and only blocks of
pieces we have verified.
"""

import asyncio
import collections

from . import wire
from .picker import BadBlock

MAX_QUEUED = 128  # requests a peer may have waiting; one more and it is dropped


class PeerConn:
    def __init__(self, session, addr):
        self.s = session
        self.addr = addr  # (host, port): also the peer's key in the picker
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

    # sending

    def send(self, data):
        if self.writer is not None and not self.closed:
            self.writer.write(data)

    def update_interest(self):
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
        if self.choked or not self.handshaken or self.closed:
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
            self.writer.write(wire.handshake(s.meta.info_hash, s.peer_id))
            peer_id = await asyncio.wait_for(wire.read_handshake(reader, s.meta.info_hash), wait)
            if peer_id == s.peer_id:  # we dialed ourselves
                return
            self.handshaken = True
            s.log(f"peer {self.addr[0]}:{self.addr[1]} connected")
            if s.picker.verified_bytes():
                self.send(wire.bitfield(s.bitfield()))
            npieces = len(s.meta.hashes)
            loop = asyncio.get_running_loop()
            self.server = asyncio.ensure_future(self.serve())
            while True:
                got = await asyncio.wait_for(wire.read_message(reader, npieces), opts.idle_timeout)
                now = loop.time()
                if (self.last_block is not None and s.picker.outstanding(self.addr)
                        and now - self.last_block > opts.idle_timeout):
                    raise asyncio.TimeoutError  # keeps the line open but never sends what we asked for
                if got is not None:
                    self.on_message(*got, now)
                await asyncio.wait_for(self.writer.drain(), opts.idle_timeout)
        except asyncio.CancelledError:
            raise
        except (BadBlock, ValueError) as e:  # the peer broke the protocol: never talk to it again
            s.banned.add(self.addr)
            s.log(f"peer {self.addr[0]}:{self.addr[1]} dropped: {e}")
        except (OSError, EOFError, asyncio.TimeoutError) as e:
            s.log(f"peer {self.addr[0]}:{self.addr[1]} lost: {type(e).__name__}")
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
                while not self.queue:
                    self.work.clear()
                    await self.work.wait()
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
                await asyncio.wait_for(self.writer.drain(), s.opts.idle_timeout)  # a peer that never reads
        except asyncio.CancelledError:
            raise
        except (OSError, ValueError, RuntimeError, asyncio.TimeoutError) as e:  # ends this peer only
            s.log(f"peer {self.addr[0]}:{self.addr[1]} not served: {type(e).__name__}")
            self.close()
            if self.task is not None and self.task is not asyncio.current_task():
                self.task.cancel()

    def report_served(self):
        if self.served:
            self.s.log(f"peer {self.addr[0]}:{self.addr[1]} served {self.served} blocks")

    def close(self):
        self.closed = True
        self.queue.clear()
        if self.server is not None:
            self.server.cancel()
        if self.writer is not None:
            self.writer.close()

    def on_message(self, mid, payload, now):
        s = self.s
        picker = s.picker
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
            picker.peer_bitfield(self.addr, wire.parse_bitfield(payload, len(s.meta.hashes)))
            self.update_interest()
            self.refill()
        elif mid == wire.PIECE:
            index, begin, data = wire.parse_piece(payload)
            self.last_block = now
            self.received += len(data)
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
