"""Announcing to HTTP(S) and UDP (BEP 15) trackers.

Errors are TrackerError with the tracker's own reason or a short cause: never the URL, which can
hold a passkey or credentials.
"""

import asyncio
import ipaddress
import random
import re
import socket
import struct
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import parse_qsl, quote_from_bytes, unquote, urlsplit

from ..target import DmError
from .bencode import BencodeError, decode

MAX_REPLY = 1 << 20
UDP_TIMEOUT = 15  # first wait; doubles on each retry, but never past the announce's budget
UDP_RETRIES = 4
BUDGET = 45  # seconds one announce to one tracker may take in all
_MAGIC = 0x41727101980
_EVENTS = {"": 0, "completed": 1, "started": 2, "stopped": 3}
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")  # C0, DEL and C1


@dataclass(frozen=True)
class Announce:
    info_hash: bytes
    peer_id: bytes
    port: int
    uploaded: int
    downloaded: int
    left: int
    event: str  # "started", "completed", "stopped" or "" for a regular update


@dataclass(frozen=True)
class AnnounceResult:
    peers: list
    interval: int


class TrackerError(Exception):
    """The message is the tracker's reason (or a short cause), never the URL."""


def in_thread(loop, fn, *args):
    """Run a blocking call on a daemon thread; a future for its result.

    Unlike the loop's executor this never keeps the process (or loop shutdown) waiting on a socket.
    """
    future = loop.create_future()

    def done(value, exc):
        if not future.done():
            if exc is not None:
                future.set_exception(exc)
            else:
                future.set_result(value)

    def run():
        try:
            value, exc = fn(*args), None
        except BaseException as e:
            value, exc = None, e
        try:
            loop.call_soon_threadsafe(done, value, exc)
        except RuntimeError:  # the loop is closed: nobody is waiting
            pass

    threading.Thread(target=run, daemon=True, name="dm-tracker").start()
    return future


def _clean_text(raw):
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    return _CONTROL.sub(" ", text).strip()[:200] or "tracker error"


def _reason(exc):
    from .. import fetch

    return fetch._reason(exc)


# HTTP

def _query(req):
    pairs = [("info_hash", quote_from_bytes(req.info_hash, safe="")),
             ("peer_id", quote_from_bytes(req.peer_id, safe="")),
             ("port", str(req.port)), ("uploaded", str(req.uploaded)), ("downloaded", str(req.downloaded)),
             ("left", str(req.left)), ("compact", "1")]
    if req.event:
        pairs.append(("event", req.event))
    return "&".join(f"{k}={v}" for k, v in pairs)


class _Call:
    """One HTTP announce in flight: lets the loop close its socket to wake a blocked read."""

    def __init__(self):
        self.lock = threading.Lock()
        self.resp = None
        self.cancelled = False

    def attach(self, resp):
        with self.lock:
            self.resp = resp
            return self.cancelled

    def cancel(self):
        with self.lock:
            self.cancelled = True
            resp = self.resp
        if resp is not None:
            try:
                resp.fp.raw._sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass


def _read_all(resp, call, deadline):
    """The body, read in chunks so a trickling reply cannot outlast the deadline."""
    body = bytearray()
    while len(body) <= MAX_REPLY:
        left = deadline - time.monotonic()
        if left <= 0 or call.cancelled:
            raise TrackerError("timed out")
        try:
            resp.fp.raw._sock.settimeout(left)
        except Exception:
            pass
        chunk = resp.read1(min(65536, MAX_REPLY + 1 - len(body)))
        if not chunk:
            break
        body += chunk
    return bytes(body)


def _http_fetch(url, req, timeout, call):
    from .. import fetch

    deadline = time.monotonic() + timeout
    parts = urlsplit(url)
    bare = parts._replace(netloc=parts.netloc.rpartition("@")[2], fragment="").geturl()
    full = bare + ("&" if parts.query else "?") + _query(req)
    opener = urllib.request.build_opener(fetch._Redirects(url))
    request = urllib.request.Request(full, headers={"User-Agent": "dm/0.2"})
    try:
        with opener.open(request, timeout=timeout) as resp:
            if call.attach(resp):
                raise TrackerError("cancelled")
            body = _read_all(resp, call, deadline)
    except TrackerError:
        raise
    except urllib.error.HTTPError as e:
        e.close()
        raise TrackerError(f"HTTP {e.code}") from None
    except urllib.error.URLError as e:
        raise TrackerError(_reason(e.reason)) from None
    except DmError:
        raise TrackerError("redirect refused") from None
    except (OSError, ValueError) as e:
        raise TrackerError(_reason(e)) from None
    except Exception as e:  # http.client errors
        raise TrackerError(_reason(e)) from None
    if len(body) > MAX_REPLY:
        raise TrackerError("reply too large")
    return body


def _compact(blob, size):
    out = []
    for i in range(0, len(blob) - size + 1, size):
        host = blob[i:i + size - 2]
        port = struct.unpack(">H", blob[i + size - 2:i + size])[0]
        if port:
            out.append((str(ipaddress.ip_address(host)), port))
    return out


def _dict_peers(items):
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        ip, port = item.get(b"ip"), item.get(b"port")
        if isinstance(ip, bytes) and isinstance(port, int) and 0 < port < 65536:
            try:
                out.append((str(ipaddress.ip_address(ip.decode("ascii"))), port))
            except ValueError:
                continue
    return out


def parse_http_reply(body):
    try:
        top, _ = decode(body)
    except BencodeError:
        raise TrackerError("invalid reply") from None
    if not isinstance(top, dict):
        raise TrackerError("invalid reply")
    if b"failure reason" in top:
        raise TrackerError(_clean_text(top[b"failure reason"]))
    interval = top.get(b"interval")
    interval = interval if isinstance(interval, int) and interval > 0 else 1800
    peers = top.get(b"peers", b"")
    found = []
    if isinstance(peers, bytes):
        found += _compact(peers, 6)
    elif isinstance(peers, list):
        found += _dict_peers(peers)
    peers6 = top.get(b"peers6", b"")
    if isinstance(peers6, bytes):
        found += _compact(peers6, 18)
    return AnnounceResult(found, interval)


# UDP

class _Udp(asyncio.DatagramProtocol):
    def __init__(self):
        self.queue = asyncio.Queue()
        self.error = None

    def datagram_received(self, data, addr):
        self.queue.put_nowait(data)

    def error_received(self, exc):
        self.error = exc
        self.queue.put_nowait(None)  # wake the waiter: an unreachable port should not cost a timeout


async def _exchange(proto, transport, packet, txid, action, limit):
    """Send, wait with doubling timeouts (none past `limit`, a loop time), retry; the reply for txid."""
    loop = asyncio.get_running_loop()
    for attempt in range(UDP_RETRIES + 1):
        transport.sendto(packet)
        deadline = min(loop.time() + UDP_TIMEOUT * 2 ** attempt, limit)
        while True:
            left = deadline - loop.time()
            try:
                data = await asyncio.wait_for(proto.queue.get(), max(left, 0.001))
            except asyncio.TimeoutError:
                if proto.error is not None:
                    raise TrackerError(_reason(proto.error)) from None
                break
            if data is None:
                raise TrackerError(_reason(proto.error))
            if len(data) < 8:
                continue
            got_action, got_tx = struct.unpack(">II", data[:8])
            if got_tx != txid:
                continue
            if got_action == 3:
                raise TrackerError(_clean_text(data[8:]))
            if got_action == action:
                return data
        if loop.time() >= limit:
            break
    raise TrackerError("timed out")


async def _udp_try(info, req, limit):
    """One announce to one resolved address."""
    loop = asyncio.get_running_loop()
    family, addr = info[0], info[4][:2]  # asyncio wants (host, port) even for IPv6
    try:
        transport, proto = await loop.create_datagram_endpoint(_Udp, remote_addr=addr, family=family)
    except OSError as e:
        raise TrackerError(_reason(e)) from None
    try:
        try:
            tx = random.getrandbits(32)
            reply = await _exchange(proto, transport, struct.pack(">QII", _MAGIC, 0, tx), tx, 0, limit)
            if len(reply) < 16:
                raise TrackerError("invalid reply")
            conn = struct.unpack(">Q", reply[8:16])[0]
            tx = random.getrandbits(32)
            packet = struct.pack(">QII20s20sQQQIIIiH", conn, 1, tx, req.info_hash, req.peer_id,
                                 req.downloaded, req.left, req.uploaded, _EVENTS.get(req.event, 0), 0,
                                 random.getrandbits(32), -1, req.port)
            reply = await _exchange(proto, transport, packet, tx, 1, limit)
        except OSError as e:
            raise TrackerError(_reason(e)) from None
        if len(reply) < 20:
            raise TrackerError("invalid reply")
        interval = struct.unpack(">I", reply[8:12])[0] or 1800
        size = 18 if family == socket.AF_INET6 else 6
        return AnnounceResult(_compact(reply[20:MAX_REPLY], size), interval)
    finally:
        transport.close()


async def _udp_announce(url, req, limit):
    loop = asyncio.get_running_loop()
    parts = urlsplit(url)
    try:
        host, port = parts.hostname, parts.port
    except ValueError:
        raise TrackerError("invalid tracker address") from None
    if not host or not port:
        raise TrackerError("invalid tracker address")
    try:
        infos = await in_thread(loop, socket.getaddrinfo, host, port, 0, socket.SOCK_DGRAM)
    except OSError:
        raise TrackerError("cannot resolve host") from None
    infos = sorted(infos, key=lambda i: i[0] != socket.AF_INET)  # IPv4 first; the sort is stable
    error = TrackerError("cannot resolve host")
    for n, info in enumerate(infos):
        # each address gets an equal share of what is left, so a silent first one cannot use it all
        share = loop.time() + max(limit - loop.time(), 0) / (len(infos) - n)
        try:
            return await _udp_try(info, req, share)
        except TrackerError as e:
            error = e
    raise error


def _secrets(url):
    """The parts of a tracker URL that may be a passkey: userinfo, query values, long path segments."""
    try:
        parts = urlsplit(url)
        found = [parts.username, parts.password] + [v for _, v in parse_qsl(parts.query)]
        found += [unquote(seg) for seg in parts.path.split("/") if len(seg) > 8]
    except ValueError:
        return []
    return sorted({f for f in found if f and len(f) >= 3}, key=len, reverse=True)


async def announce(url, req, timeout=BUDGET):
    """Announce once, taking at most `timeout` seconds in all (UDP retries follow BEP 15 within it).

    A TrackerError's text never holds a piece of the URL a passkey could be in."""
    try:
        return await _announce(url, req, timeout)
    except TrackerError as e:
        text = str(e)
        for secret in _secrets(url):
            text = text.replace(secret, "***")
        if text != str(e):
            raise TrackerError(text) from None
        raise


async def _announce(url, req, timeout):
    try:
        scheme = urlsplit(url).scheme
    except ValueError:
        raise TrackerError("invalid tracker URL") from None
    loop = asyncio.get_running_loop()
    if scheme in ("http", "https"):
        call = _Call()
        try:
            body = await asyncio.wait_for(in_thread(loop, _http_fetch, url, req, timeout, call), timeout)
        except asyncio.TimeoutError:
            raise TrackerError("timed out") from None
        finally:
            call.cancel()
        return parse_http_reply(body)
    if scheme == "udp":
        try:
            return await asyncio.wait_for(_udp_announce(url, req, loop.time() + timeout), timeout)
        except asyncio.TimeoutError:
            raise TrackerError("timed out") from None
    raise TrackerError("unsupported tracker")
