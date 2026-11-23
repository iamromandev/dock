"""Fetching a URL to a temp file in the target folder and moving it into place."""

import base64
import http.client
import os
import secrets
import socket
import threading
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

from . import Cancelled
from .fmt import Throttle, display_url, human_size
from .names import _cut, name_from
from .target import DmError
from .torrent.place import place_file

CHUNK = 64 * 1024
TIMEOUT = 30
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
        parts.port  # noqa: B018 - raises ValueError on a bad port
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
    """Lets another thread abort a running download and clean up after it."""

    def __init__(self):
        self.lock = threading.Lock()
        self.aborted = False
        self.placed = False
        self.response = None
        self.tmp = None

    def emit(self, fn, *args):
        """Call fn unless aborted; checked under the lock abort() takes."""
        with self.lock:
            if not self.aborted:
                fn(*args)

    def abort(self):
        """Stop the download. False if the file was already moved into place."""
        with self.lock:
            if self.placed:
                return False
            self.aborted = True
            tmp, self.tmp = self.tmp, None
        if self.response is not None:
            try:  # wake a read that is blocked on the socket
                self.response.fp.raw._sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
        if tmp is not None:
            _unlink(tmp)
        return True


def _unlink(path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


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


def _open(url, handle):
    check_url(url)
    parts = urlsplit(url)
    req = urllib.request.Request(parts._replace(netloc=parts.netloc.rpartition("@")[2]).geturl(),
                                 headers={"User-Agent": "dm/0.1"})
    if parts.username is not None:  # unredirected: never sent to another host
        cred = f"{unquote(parts.username)}:{unquote(parts.password or '')}".encode()
        req.add_unredirected_header("Authorization", "Basic " + base64.b64encode(cred).decode())
    opener = urllib.request.build_opener(_Redirects(url))
    try:
        return opener.open(req, timeout=TIMEOUT)
    except urllib.error.HTTPError as e:
        e.close()
        raise DmError(f"{display_url(url)}: HTTP {e.code} {e.reason or ''}".rstrip()) from None
    except urllib.error.URLError as e:
        raise DmError(f"{display_url(url)}: {_reason(e.reason)}") from None
    except http.client.HTTPException as e:
        raise DmError(f"{display_url(url)}: {_reason(e)}") from None
    except OSError as e:
        raise DmError(f"{display_url(url)}: {_reason(e)}") from None


def _write_error(folder, exc, handle):
    if isinstance(exc, FileNotFoundError) and handle.aborted:  # the temp file vanished
        return Cancelled()
    return DmError(f"cannot write to {folder}: {exc.strerror or type(exc).__name__}")


def _write_all(fd, data):
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


_place = place_file  # old name, kept for callers and tests that patch it


def download(url, target, progress, cancelled, handle=None, throttle=None):
    """Download url into target.folder. Returns (final path, size in bytes)."""
    handle = handle or Handle()
    throttle = throttle or Throttle()
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
    resp = _open(url, handle)
    handle.response = resp
    tmp = None
    try:
        if stop():
            raise Cancelled()
        try:
            length = int(resp.headers.get("Content-Length"))
            if length < 0:
                length = None
        except (TypeError, ValueError):
            length = None
        base = target.fixed_name or name_from(resp.headers, resp.geturl())
        tmp = target.folder / f".{_cut(base, 100)}.{secrets.token_hex(8)}.dm-part"
        with handle.lock:  # an abandoned worker never creates a temp file after an abort
            if stop():
                raise Cancelled()
            try:
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
            except OSError as e:
                raise _write_error(target.folder, e, handle) from None
            handle.tmp = tmp
        done = 0
        try:
            while True:
                if stop():
                    raise Cancelled()
                try:
                    chunk = resp.read(CHUNK)
                except http.client.IncompleteRead:
                    raise DmError(f"{display_url(url)}: connection closed early") from None
                except OSError as e:
                    if stop():
                        raise Cancelled() from None
                    raise DmError(f"{display_url(url)}: {_reason(e)}") from None
                if not chunk:
                    break
                try:
                    _write_all(fd, chunk)
                except OSError as e:
                    raise DmError(f"cannot write to {target.folder}: {_reason(e)}") from None
                done += len(chunk)
                pct = None if length is None else min(100, done * 100 // length) if length else 100
                emit(pct, _progress_text(done, length))
        finally:
            os.close(fd)
        if length is not None and done < length:
            raise DmError(f"{display_url(url)}: connection closed early")
        emit(None if length is None else 100, _progress_text(done, length), final=True)
        with handle.lock:
            if stop():
                raise Cancelled()
            try:
                final = _place(tmp, target.folder, base)
            except OSError as e:
                raise _write_error(target.folder, e, handle) from None
            handle.placed = True
        return Path(final), done
    finally:
        resp.close()
        if tmp is not None:
            _unlink(tmp)


def _progress_text(done, length):
    if length is None:
        return human_size(done)
    return f"{human_size(done)} of {human_size(length)}"
