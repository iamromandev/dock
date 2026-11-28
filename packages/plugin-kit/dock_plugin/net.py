"""A cancellable HTTP request to a local service, with errors in words the user can act on."""

import http.client
import urllib.parse

from .cancel import Cancelled, Failure, open_socket


def shown(base):
    """The server address without any user:password part, safe to log."""
    parts = urllib.parse.urlsplit(base)
    return f"{parts.scheme}://{parts.netloc.rpartition('@')[2]}" if parts.netloc else base.rpartition("@")[2]


def check_url(base, var):
    """Raises Failure unless base is an http URL with a host and a port in 1-65535 (if given). `var` names the
    environment variable it came from."""
    try:
        parts = urllib.parse.urlsplit(base)
        port = parts.port
        ok = parts.scheme == "http" and bool(parts.hostname) and (port is None or 1 <= port <= 65535)
    except ValueError:
        ok = False
    if not ok:
        raise Failure(f"{var} is not a valid http URL")


def request(base, path, body, call, *, service, var, start, read_timeout, max_reply, connect_timeout=10,
            content_type="application/octet-stream", method="POST"):
    """One request to base + path; returns (status, data) with at most max_reply bytes of data. Raises Failure
    with a message to show, Cancelled after a cancel. `service` names the server, `var` the environment variable
    holding its URL and `start` the command that starts it. The call's lock is never held while sending or
    waiting: a cancel closes the registered socket, which ends the send or the read at once. A request is never
    sent once the call is stopped."""
    where = shown(base)
    parts = urllib.parse.urlsplit(base)
    conn = http.client.HTTPConnection(parts.hostname or "", parts.port)
    conn.auto_open = 0
    reaching = True  # still making the connection
    try:
        conn.sock = open_socket(parts.hostname, parts.port or 80, call, connect_timeout, read_timeout)
        with call.lock:
            if call.cancelled():
                raise Cancelled()
        reaching = False
        conn.request(method, f"{parts.path.rstrip('/')}{path}", body, {"Content-Type": content_type})
        resp = conn.getresponse()
        data = resp.read(max_reply + 1)
        status = resp.status
    except (OSError, http.client.HTTPException) as e:
        if call.cancelled():
            raise Cancelled() from None
        if reaching:
            raise Failure(f"cannot reach the {service} server at {where}: {getattr(e, 'reason', e) or type(e).__name__}. "
                          f"Start it with `{start}`; "
                          f"set {var} if it runs elsewhere") from None
        if isinstance(e, TimeoutError):
            raise Failure(f"timed out after {read_timeout:g} s waiting for the {service} server") from None
        raise Failure(f"the {service} server stopped answering ({where})") from None
    finally:
        conn.close()
    if len(data) > max_reply:
        raise Failure(f"the {service} server at {where} sent an answer that is too large (over {max_reply} bytes)")
    return status, data
