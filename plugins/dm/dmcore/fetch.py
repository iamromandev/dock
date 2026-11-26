"""Fetching a URL to a temp file in the target folder and moving it into place."""

import base64
import http.client
import os
import re
import socket
import stat
import threading
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

from . import Cancelled
from . import resume as resume_mod
from .fmt import Throttle, display_url, human_size
from .names import name_from
from .target import DmError
from .torrent.place import place_file

CHUNK = 64 * 1024
TIMEOUT = 30
TAIL = 1 << 20  # a resume re-fetches this much of the partial: an OS crash can tear the end of the file
MAX_REDIRECTS = 10


def _unsupported(url):
    """Never echo the URL: it may carry credentials, a query or a fragment."""
    try:
        scheme = urlsplit(url).scheme
    except ValueError:
        scheme = ""
    if not scheme or scheme in ("http", "https"):
        return DmError("invalid URL")
    return DmError(f"unsupported URL scheme '{scheme}' (only http and https)")


def check_url(url):
    try:
        parts = urlsplit(url)
        ok = parts.scheme in ("http", "https") and bool(parts.hostname)
        parts.port  # raises ValueError on a bad port
    except ValueError:
        ok = False
    if not ok:
        raise _unsupported(url)


class _Redirects(urllib.request.HTTPRedirectHandler):
    max_repeats = MAX_REDIRECTS + 1  # our own counter reports loops

    def __init__(self, url):
        self.url = url
        self.count = 0

    def _follow(self, req, fp, code, msg, headers):
        location = headers.get("location") or headers.get("uri")
        if location:
            check_url(urljoin(req.full_url, location))
        self.count += 1
        if self.count > MAX_REDIRECTS:
            raise DmError(f"{display_url(self.url)}: too many redirects")
        return super().http_error_302(req, fp, code, msg, headers)

    http_error_301 = http_error_302 = http_error_303 = http_error_307 = http_error_308 = _follow


class Handle:
    """Lets another thread abort a running download. The worker decides what to keep."""

    def __init__(self):
        self.lock = threading.Lock()
        self.aborted = False
        self.placed = False
        self.response = None
        self.sock = None  # the socket of the request being made, before there is a response

    def emit(self, fn, *args):
        """Call fn unless aborted; checked under the lock abort() takes."""
        with self.lock:
            if not self.aborted:
                fn(*args)

    def abort(self):
        """Stop the download. False if the file was already moved into place.

        Nothing is deleted here: the worker's cleanup keeps the partial when it can be resumed.
        """
        with self.lock:
            if self.placed:
                return False
            self.aborted = True
        self.wake()
        return True

    def wake(self):
        """Unblock whatever waits on the socket: a request nobody answers, or a read."""
        socks = [self.sock]
        try:
            socks.append(self.response.fp.raw._sock)
        except AttributeError:
            pass
        for sock in socks:
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass


class HttpError(DmError):
    """The server answered with an error status. Retryable ones keep the partial data."""

    def __init__(self, message, status):
        super().__init__(message, keep=status in (408, 429) or status >= 500)
        self.status = status


def _reason(exc):
    """A short cause that never contains the URL."""
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "timed out"
    if isinstance(exc, http.client.RemoteDisconnected):
        return "connection closed by server"
    if isinstance(exc, http.client.InvalidURL):
        return "invalid URL"
    if isinstance(exc, OSError):
        return exc.strerror or type(exc).__name__
    if isinstance(exc, http.client.HTTPException):
        return type(exc).__name__
    return "connection failed"


def _registering(base, handle):
    """A connection class that hands its socket to the handle as soon as it exists."""

    class Connection(base):
        def connect(self):
            super().connect()
            handle.sock = self.sock
            if handle.aborted:
                handle.wake()

    return Connection


class _HttpHandler(urllib.request.HTTPHandler):
    def __init__(self, handle):
        super().__init__()
        self.handle = handle

    def http_open(self, req):
        return self.do_open(_registering(http.client.HTTPConnection, self.handle), req)


class _HttpsHandler(urllib.request.HTTPSHandler):
    def __init__(self, handle):
        super().__init__()
        self.handle = handle

    def https_open(self, req):
        return self.do_open(_registering(http.client.HTTPSConnection, self.handle), req, context=self._context)


def _open(url, handle, headers=None):
    check_url(url)
    parts = urlsplit(url)
    req = urllib.request.Request(parts._replace(netloc=parts.netloc.rpartition("@")[2]).geturl(),
                                 headers={"User-Agent": "dm/0.1", **(headers or {})})
    if parts.username is not None:  # unredirected: never sent to another host
        cred = f"{unquote(parts.username)}:{unquote(parts.password or '')}".encode()
        req.add_unredirected_header("Authorization", "Basic " + base64.b64encode(cred).decode())
    opener = urllib.request.build_opener(_Redirects(url), _HttpHandler(handle), _HttpsHandler(handle))
    try:
        return opener.open(req, timeout=TIMEOUT)
    except urllib.error.HTTPError as e:
        e.close()
        raise HttpError(f"{display_url(url)}: HTTP {e.code} {e.reason or ''}".rstrip(), e.code) from None
    except urllib.error.URLError as e:
        raise DmError(f"{display_url(url)}: {_reason(e.reason)}", keep=True) from None
    except http.client.HTTPException as e:
        raise DmError(f"{display_url(url)}: {_reason(e)}", keep=not isinstance(e, http.client.InvalidURL)) from None
    except OSError as e:
        raise DmError(f"{display_url(url)}: {_reason(e)}", keep=True) from None


def _write_error(folder, exc, handle):
    if isinstance(exc, FileNotFoundError) and handle.aborted:  # the partial vanished
        return Cancelled()
    return DmError(f"cannot write to {folder}: {exc.strerror or type(exc).__name__}", keep=resume_mod.is_quota_error(exc))


def _write_all(fd, data):
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


_place = place_file  # old name, kept for callers and tests that patch it


class _State:
    """What the cleanup needs to know: the record of the partial on disk, and whether a retry may succeed."""

    def __init__(self):
        self.record = None
        self.keep = False


def _size(path):
    try:
        st = os.lstat(path)
    except OSError:
        return 0
    return st.st_size if stat.S_ISREG(st.st_mode) else 0


def _is_empty_file(path):
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and st.st_size == 0


def _existing(p, key):
    """(size, record) of a partial that can be continued, else None."""
    try:
        st = os.lstat(p.part)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode) or st.st_size <= 0:
        return None
    rec = resume_mod.read_record(p.record, key)
    if rec is None or st.st_size > rec.total:
        return None
    return st.st_size, rec


_CONTENT_RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+)")


def _range_ok(resp, start, rec):
    """Is this 206 the rest of the very file the record describes, starting exactly at start?"""
    h = resp.headers
    m = _CONTENT_RANGE.fullmatch((h.get("Content-Range") or "").strip())
    if not m or tuple(map(int, m.groups())) != (start, rec.total - 1, rec.total):
        return False
    if h.get("Content-Length") is not None and h.get("Content-Length").strip() != str(rec.total - start):
        return False
    if (h.get("Content-Encoding") or "identity").strip().lower() != "identity":
        return False
    # the validator that was sent in If-Range must come back, and be the same
    name, want = ("ETag", rec.etag) if rec.etag else ("Last-Modified", rec.last_modified)
    got = h.get(name)
    return got is not None and got.strip() == want


def _content_length(resp):
    try:
        length = int(resp.headers.get("Content-Length"))
    except (TypeError, ValueError):
        return None
    return length if length >= 0 else None


def download(url, target, progress, cancelled, handle=None, throttle=None, *, resume=True, note=None):
    """Download url into target.folder. Returns (final path, size in bytes).

    An interrupted download keeps its partial and a small record next to it when a retry may succeed;
    the next run of the same download continues it. `resume=False` throws an old partial away.
    """
    handle = handle or Handle()
    throttle = throttle or Throttle()
    note = note or (lambda text: None)
    sent = []

    def emit(pct, text, final=False):
        if sent and sent[-1] == (pct, text):
            return
        if throttle.ready(final=final):
            sent.append((pct, text))
            handle.emit(progress, pct, text)

    def stop():
        return cancelled.is_set() or handle.aborted

    if stop():
        raise Cancelled()
    check_url(url)
    key = resume_mod.http_key(url, target)
    p = resume_mod.paths(target.folder, target.fixed_name or name_from({}, url), key)
    lock = resume_mod.Lock(p.lock)
    lock.acquire()
    state = _State()
    ok = False
    try:
        resume_mod.clean_temps(p.record)
        result = _download(url, target, p, key, resume, lambda text: handle.emit(note, text), emit, stop, handle, state)
        ok = True
        return result
    except Cancelled:
        state.keep = True
        raise
    except DmError as e:
        state.keep = e.keep
        raise
    finally:
        try:
            if ok:
                resume_mod.discard(p.record)
            elif not (state.keep and state.record is not None and _size(p.part) > 0):
                resume_mod.discard(p.part, p.record)
        except OSError:
            pass
        finally:
            lock.release(remove=True)


def _download(url, target, p, key, resume, say, emit, stop, handle, state):
    opened = []

    def open_(headers=None):
        resp = _open(url, handle, headers)
        opened.append(resp)
        handle.response = resp
        if stop():
            raise Cancelled()
        return resp

    try:
        existing = None
        if not resume:
            resume_mod.discard(p.part, p.record)
        else:
            existing = _existing(p, key)
            if existing is not None and existing[0] <= TAIL:  # too little to be worth a Range request
                existing = None
                resume_mod.discard(p.part, p.record)
            elif existing is None and (_size(p.part) > 0 or os.path.lexists(p.record)
                                       or (os.path.lexists(p.part) and not _is_empty_file(p.part))):
                resume_mod.discard(p.part, p.record)
                say("cannot resume: starting over")
            elif existing is None:
                resume_mod.discard(p.part)
        resp, resumed = None, False
        if existing is not None:
            n, rec = existing
            state.record = rec
            start = n - TAIL
            try:
                resp = open_({"Range": f"bytes={start}-", "If-Range": rec.validator()})
            except HttpError as e:
                if e.keep:
                    raise
            resumed = resp is not None and resp.status == 206 and _range_ok(resp, start, rec)
            if not resumed:  # the file changed, or the server cannot or will not continue it
                if resp is not None and resp.status != 200:
                    resp.close()
                    resp = None
                resume_mod.discard(p.part, p.record)
                state.record = None
                say("cannot resume: starting over")
        if resp is None:
            resp = open_()
        if resumed:
            length = rec.total
            fd = _reopen_part(p.part, start, target.folder, handle)
            say(f"resuming from {human_size(start)} of {human_size(length)}")
            done = start
        else:
            length = _content_length(resp)
            fd = _create_part(p, key, resp, length, target.folder, stop, handle, state)
            done = 0
        try:
            if resumed:  # progress starts at the resumed size
                emit(_percent(done, length), _progress_text(done, length))
            while True:
                if stop():
                    raise Cancelled()
                try:
                    chunk = resp.read(CHUNK)
                except http.client.IncompleteRead:
                    raise DmError(f"{display_url(url)}: connection closed early", keep=True) from None
                except OSError as e:
                    if stop():
                        raise Cancelled() from None
                    raise DmError(f"{display_url(url)}: {_reason(e)}", keep=True) from None
                if not chunk:
                    break
                try:
                    _write_all(fd, chunk)
                except OSError as e:
                    raise DmError(f"cannot write to {target.folder}: {_reason(e)}",
                                  keep=resume_mod.is_quota_error(e)) from None
                done += len(chunk)
                if resumed and done > length:
                    raise DmError(f"{display_url(url)}: the server sent more data than it announced")
                emit(_percent(done, length), _progress_text(done, length))
        finally:
            os.close(fd)
        if length is not None and (done != length if resumed else done < length):
            raise DmError(f"{display_url(url)}: connection closed early", keep=True)
        emit(None if length is None else 100, _progress_text(done, length), final=True)
        base = target.fixed_name or name_from(resp.headers, resp.geturl())
        with handle.lock:
            if stop():
                raise Cancelled()
            try:
                final = _place(p.part, target.folder, base)
            except DmError as e:  # the whole file is on disk: a rerun can place it
                raise DmError(str(e), keep=True) from None
            except OSError as e:
                err = _write_error(target.folder, e, handle)
                if isinstance(err, DmError):
                    err.keep = True  # likewise: nothing is wrong with the data
                raise err from None
            handle.placed = True
        return Path(final), done
    finally:
        for resp in opened:
            resp.close()


def _percent(done, length):
    return None if length is None else min(100, done * 100 // length) if length else 100


def _reopen_part(part, start, folder, handle):
    """Open the existing partial for appending at start, never following a symlink."""
    try:
        fd = os.open(part, os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as e:
        raise _write_error(folder, e, handle) from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size < start:
            raise DmError("cannot resume: the partial file changed")
        os.ftruncate(fd, start)
        os.lseek(fd, start, os.SEEK_SET)
    except BaseException as e:
        os.close(fd)
        if isinstance(e, OSError):
            raise _write_error(folder, e, handle) from None
        raise
    return fd


def _create_part(p, key, resp, length, folder, stop, handle, state):
    """Create the partial (and its record, when the response allows resuming) before the body is read."""
    rec = resume_mod.record_from_headers(resp.headers, length)
    with handle.lock:  # an abandoned worker never creates a file after an abort
        if stop():
            raise Cancelled()
        try:
            fd = os.open(p.part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o666)
        except OSError as e:
            raise _write_error(folder, e, handle) from None
        if rec is not None:
            try:
                resume_mod.write_record(p.record, key, rec)
            except OSError as e:
                os.close(fd)
                raise _write_error(folder, e, handle) from None
            state.record = rec
    return fd


def _progress_text(done, length):
    if length is None:
        return human_size(done)
    return f"{human_size(done)} of {human_size(length)}"
