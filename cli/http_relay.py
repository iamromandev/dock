"""HTTP through the host: a plugin asks dock to make a request, dock checks it and streams the answer back.

A plugin declares the hosts it may reach in `[capabilities].http`. Every request, and every redirect hop, is
checked against those entries and against the address the name resolves to; the connection then goes to the
address that was checked (no second lookup), and the body is streamed in small chunks.
"""

from __future__ import annotations

import base64
import binascii
import difflib
import http.client
import ipaddress
import math
import os
import re
import socket
import ssl
import threading
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass

REFUSED = -32001
HTTP_PORT, HTTPS_PORT = 80, 443
KNOWN_KEYS = ("host", "private")  # of a table entry in [capabilities].http
CONFIG_KEYS = ("max_response_bytes", "total_timeout")  # of [http] in config.toml
_LABEL = re.compile(r"[a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9_])?")


METHODS = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"}
REDIRECTS = {301, 302, 303, 307, 308}
CREDENTIALS = {"authorization", "cookie", "proxy-authorization"}  # never follow a redirect to another host
OWNED = {"host", "content-length", "transfer-encoding", "connection", "upgrade", "expect", "keep-alive",
         "proxy-connection", "te", "trailer"}  # set by dock itself, whatever the plugin says
_TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_BAD_VALUE = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")

resolver = socket.getaddrinfo          # module attributes, so tests can stand in for the network
connector = socket.create_connection
tls_context = ssl.create_default_context  # the factory of the client TLS context; tests point trust at a test CA
COALESCE = 0.05                        # seconds: small reads of a body are sent together, at most this often


class Refused(Exception):
    """The request was refused; the message is shown to the plugin."""


class Cancelled(Exception):
    """The request was cancelled (Ctrl-C) while it ran."""


@dataclass(frozen=True)
class Limits:
    max_response_bytes: int = 256 * 2**20
    max_body_bytes: int = 16 * 2**20
    max_header_bytes: int = 64 * 2**10
    idle_timeout: float = 30.0
    total_timeout: float = 600.0  # connect, headers and body together
    max_redirects: int = 5
    chunk: int = 64 * 2**10


@dataclass(frozen=True)
class HttpRule:
    host: str            # lower-case, punycode, no trailing dot; "*.example.com" keeps the "*." prefix
    port: int | None     # None = the scheme's default port only
    allow_http: bool     # the entry was written with http://
    private: bool        # localhost / IP literal / private = true: may resolve to a private address
    raw: str             # as written, for `plugin info`


# rules -----------------------------------------------------------------------
def _ip(host: str):
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _name(host: str) -> str:
    """A normalised host name or IP literal; ValueError says what is wrong with it."""
    if host.endswith("."):
        host = host[:-1]
    if not host:
        raise ValueError("empty host")
    if "%" in host:
        raise ValueError("a zone or percent sign is not allowed in a host")
    ip = _ip(host)
    if ip is not None:
        return str(ip)
    try:
        ascii_host = host.lower().encode("idna").decode("ascii") if not host.isascii() else host.lower()
    except UnicodeError as e:
        raise ValueError(f"not a valid host name ({e})") from e
    labels = ascii_host.split(".")
    if not all(_LABEL.fullmatch(label) for label in labels):
        raise ValueError("not a valid host name")
    if labels[-1].isdigit():
        raise ValueError("not a valid host name or IP address")
    return ascii_host


def _parse_entry(entry: str, private: bool) -> HttpRule:
    text = entry
    allow_http = False
    if text[:7].lower() == "http://":
        allow_http, text = True, text[7:]
    elif "://" in text:
        raise ValueError("only an http:// prefix is allowed (https is the default)")
    if not text or text.strip() != text or any(c.isspace() or ord(c) < 0x20 for c in text):
        raise ValueError("expected a host name, like api.example.com")
    if any(c in text for c in "/?#@\\"):
        raise ValueError("a host entry has no path, query or user, just host[:port]")

    port = None
    if text.startswith("["):
        end = text.find("]")
        if end < 0:
            raise ValueError("missing ']' after the IPv6 address")
        host, rest = text[1:end], text[end + 1:]
        if rest and not rest.startswith(":"):
            raise ValueError("expected ':port' after the IPv6 address")
        if _ip(host) is None or ":" not in host:
            raise ValueError("not an IPv6 address")
        port_text = rest[1:] if rest else None
        if rest and not port_text:
            raise ValueError("empty port")
    elif text.count(":") > 1:
        raise ValueError("write an IPv6 address in brackets, like [::1]:8080")
    else:
        host, colon, port_text = text.partition(":")
        port_text = port_text if colon else None
        if port_text == "":
            raise ValueError("empty port")
    if port_text is not None:
        if not port_text.isascii() or not port_text.isdigit() or not 1 <= int(port_text) <= 65535:
            raise ValueError(f"port '{port_text}' must be a number from 1 to 65535")
        port = int(port_text)

    if host.startswith("*"):
        if not host.startswith("*."):
            raise ValueError("a wildcard must be written *.example.com")
        rest = host[2:]
        if rest.count(".") < 1 or rest.startswith("*") or "*" in rest or _ip(rest) is not None:
            raise ValueError("a wildcard needs at least two labels after '*.', like *.example.com")
        name = "*." + _name(rest)
        is_private = private
    else:
        if "*" in host:
            raise ValueError("'*' is only allowed as a leading '*.' label")
        name = _name(host)
        is_private = private or name == "localhost" or _ip(name) is not None
    return HttpRule(name, port, allow_http, is_private, entry)


def parse_rules(entries) -> list[HttpRule]:
    """[capabilities].http as written -> rules. Raises ValueError("<entry>: <why>")."""
    if not isinstance(entries, list):
        raise ValueError("must be a list of hosts")
    rules = []
    for entry in entries:
        if isinstance(entry, str):
            label, private = entry, False
            try:
                rules.append(_parse_entry(entry, private))
            except ValueError as e:
                raise ValueError(f"{label}: {e}") from e
        elif isinstance(entry, dict):
            host = entry.get("host")
            label = host if isinstance(host, str) else repr(entry)
            unknown = sorted(set(entry) - set(KNOWN_KEYS))
            if unknown:
                raise ValueError(f"{label}: unknown key {', '.join(repr(k) for k in unknown)} (known: {', '.join(KNOWN_KEYS)})")
            if "host" not in entry:
                raise ValueError(f"{label}: needs a 'host'")
            if not isinstance(host, str):
                raise ValueError(f"{label}: 'host' must be a string")
            private = entry.get("private", False)
            if not isinstance(private, bool):
                raise ValueError(f"{label}: 'private' must be true or false")
            try:
                rules.append(_parse_entry(host, private))
            except ValueError as e:
                raise ValueError(f"{label}: {e}") from e
        else:
            raise ValueError(f"{entry!r}: an entry must be a host name or a table like {{ host = \"...\", private = true }}")
    return rules


def _default_port(scheme: str) -> int:
    return HTTPS_PORT if scheme == "https" else HTTP_PORT


def _host_matches(rule: HttpRule, host: str) -> bool:
    if rule.host.startswith("*."):
        return _ip(host) is None and host.endswith(rule.host[1:]) and len(host) > len(rule.host) - 1
    return host == rule.host


def _normal_host(host: str) -> str | None:
    try:
        return _name(host)
    except ValueError:
        return None


def _specificity(rule: HttpRule) -> tuple[int, int, int]:
    """Bigger is more specific: an exact host over any wildcard, a longer wildcard over a shorter, a port over none."""
    exact = not rule.host.startswith("*.")
    return (1 if exact else 0, 0 if exact else len(rule.host), 1 if rule.port is not None else 0)


def match(rules, scheme: str, host: str, port: int) -> HttpRule | None:
    """The most specific rule that allows `scheme://host:port` (the first of equals), or None."""
    name = _normal_host(host)
    if name is None:
        return None
    best = None
    for rule in rules:
        if not _host_matches(rule, name):
            continue
        if scheme == "http" and not rule.allow_http:
            continue
        if port != (rule.port if rule.port is not None else _default_port(scheme)):
            continue
        if best is None or _specificity(rule) > _specificity(best):
            best = rule
    return best


def check_url(rules, url) -> tuple[HttpRule, str, str, int, str]:
    """(rule, scheme, host, port, path+query) for a URL these rules allow; Refused otherwise. Nothing is resolved."""
    if not isinstance(url, str):
        raise Refused("url must be a string")
    if any(ord(c) <= 0x20 or ord(c) == 0x7F for c in url):
        raise Refused("the url has spaces or control characters; encode them")
    try:
        parts = urllib.parse.urlsplit(url)
        scheme = parts.scheme.lower()
        if scheme not in ("http", "https"):
            raise Refused(f"scheme '{scheme}' is not allowed: only https, or http where an entry says http://")
        if "@" in parts.netloc:
            raise Refused("the url has userinfo (user:password@); dock does not send credentials in urls")
        host = parts.hostname
        if not host:
            raise Refused("the url has no host")
        port = parts.port if parts.port is not None else _default_port(scheme)
    except ValueError:
        raise Refused("invalid port or host in the url") from None
    if not 1 <= port <= 65535:
        raise Refused(f"invalid port {port} in the url")
    name = _normal_host(host)
    if name is None:
        raise Refused(f"host '{host}' is not a valid host name")

    named = [r for r in rules if _host_matches(r, name)]
    if not named:
        raise Refused(f"host '{name}' is not declared in [capabilities].http")
    if scheme == "http" and not any(r.allow_http for r in named):
        raise Refused(f"plain http to '{name}' is not allowed: declare it as http://{name} in [capabilities].http")
    rule = match(rules, scheme, name, port)
    if rule is None:
        raise Refused(f"port {port} of '{name}' is not declared in [capabilities].http")

    path = urllib.parse.quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~[]")
    if parts.query:
        path += "?" + urllib.parse.quote(parts.query, safe="/%:@!$&'()*+,;=-._~?[]")
    return rule, scheme, name, port, path


# config ----------------------------------------------------------------------
def config(store) -> Limits:
    """The limits: defaults, plus `[http]` of config.toml.

    An unknown key is an error, like in [isolation] and [wasm]; so is a config.toml that does not parse,
    as the setting may be the one that did not parse.
    """
    from cli.manifest import ManifestError  # manifest imports this module

    path = store.home / "config.toml"
    where = f"{path}: [http]"
    data = store.config()
    if "http" not in data:
        for key in data:
            lowered = key.strip().lower()
            if lowered in CONFIG_KEYS:
                raise ManifestError(f"{path}: '{key}' is at the top level; HTTP settings belong in an [http] table")
            if lowered == "http" or difflib.get_close_matches(lowered, ["http"], n=1, cutoff=0.75):
                raise ManifestError(f"{path}: [{key}] is not a known table; did you mean [http]?")
    table = data.get("http", {})
    if not isinstance(table, dict):
        raise ManifestError(f"{where} must be a table")
    unknown = sorted(set(table) - set(CONFIG_KEYS))
    if unknown:
        names = ", ".join(repr(k) for k in unknown)
        raise ManifestError(
            f"{where} has unknown key{'s' if len(unknown) > 1 else ''} {names} (known: {', '.join(CONFIG_KEYS)})")
    limits = Limits()
    cap = table.get("max_response_bytes", limits.max_response_bytes)
    if isinstance(cap, bool) or not isinstance(cap, int) or cap <= 0:
        raise ManifestError(f"{where} max_response_bytes must be a positive integer")
    total = table.get("total_timeout", limits.total_timeout)
    if isinstance(total, bool) or not isinstance(total, (int, float)) or not math.isfinite(total) or total <= 0:
        raise ManifestError(f"{where} total_timeout must be a positive number of seconds")
    idle = limits.idle_timeout
    try:  # tests only: a stalled server must not take 30 s. Anything but a positive number is ignored.
        override = float(os.environ.get("DOCK_TEST_HTTP_IDLE_TIMEOUT", ""))
    except ValueError:
        override = 0.0
    if math.isfinite(override) and override > 0:
        idle = override
    return Limits(max_response_bytes=cap, idle_timeout=idle, total_timeout=total)


# addresses -------------------------------------------------------------------
def _nets(*cidrs):
    return tuple(ipaddress.ip_network(c) for c in cidrs)


# Listed here rather than asked of ipaddress (is_global, is_private and is_reserved changed in 3.12.4 and 3.13),
# so the verdict is the same on every Python dock supports.
_NOT_PUBLIC_V4 = _nets(
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12",
    "192.0.0.0/24", "192.0.2.0/24", "192.88.99.0/24", "192.168.0.0/16", "198.18.0.0/15", "198.51.100.0/24",
    "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4")  # the last two: multicast, and reserved with 255.255.255.255
_PUBLIC_V6 = _nets("2000::/3")  # global unicast: all else (::/3 and the rest of the space) is reserved or special
_NOT_PUBLIC_V6 = _nets(
    "64:ff9b:1::/48", "2001::/23", "2001:db8::/32", "3fff::/20", "5f00::/16", "fc00::/7", "fe80::/10", "fec0::/10")
_NAT64, _SIX_TO_FOUR = ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("2002::/16")


def _within(ip, nets) -> bool:
    return any(ip in net for net in nets)


def _embedded(ip):
    """The IPv4 address an IPv6 address stands for (mapped, 6to4, NAT64), which is checked as IPv4."""
    value = int(ip)
    if value >> 32 == 0xFFFF:  # ::ffff:a.b.c.d
        return ipaddress.IPv4Address(value & 0xFFFFFFFF)
    if ip in _NAT64:
        return ipaddress.IPv4Address(value & 0xFFFFFFFF)
    if ip in _SIX_TO_FOUR:
        return ipaddress.IPv4Address((value >> 80) & 0xFFFFFFFF)
    return None


def _public(ip) -> bool:
    if ip.version == 4:
        return not _within(ip, _NOT_PUBLIC_V4)
    inner = _embedded(ip)
    if inner is not None:
        return _public(inner)
    return _within(ip, _PUBLIC_V6) and not _within(ip, _NOT_PUBLIC_V6)


def checked_addresses(host: str, port: int, rule: HttpRule) -> list[str]:
    """Look `host` up once and return the addresses dock may connect to, in the resolver's order.

    Nothing private unless the entry says so. Connecting to one of these (never to a fresh lookup) is what
    keeps a name that answers differently the second time from reaching an address that was not checked.
    """
    if _ip(host) is not None:
        found = [host]
    else:
        try:
            infos = resolver(host, port, type=socket.SOCK_STREAM)
        except (OSError, UnicodeError):
            raise Refused(f"could not resolve '{host}'") from None
        found = list(dict.fromkeys(info[4][0].split("%", 1)[0] for info in infos))
    if not found:
        raise Refused(f"could not resolve '{host}'")
    allowed = [a for a in found if rule.private or (_ip(a) is not None and _public(_ip(a)))]
    if not allowed:
        raise Refused(f"host '{host}' resolves to a private address; only an entry that says so "
                      "(localhost, an IP literal, or private = true) may reach one")
    return allowed


def resolve_checked(host: str, port: int, rule: HttpRule) -> str:
    """The address dock connects to: the first acceptable one."""
    return checked_addresses(host, port, rule)[0]


# the engine ------------------------------------------------------------------
def _clean(text: str, limit: int = 300) -> str:
    return "".join("?" if ord(c) < 0x20 or ord(c) == 0x7F else c for c in text)[:limit]


def _display(params) -> str:
    """`METHOD scheme://host[:port]/path` for the log: no query, userinfo, headers or body."""
    method = params.get("method")
    method = _clean(method, 16) if isinstance(method, str) else "?"
    url = params.get("url")
    shown = "<invalid url>"
    if isinstance(url, str):
        try:
            parts = urllib.parse.urlsplit(url)
            if parts.hostname:
                port = f":{parts.port}" if parts.port is not None else ""
                host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
                shown = _clean(f"{parts.scheme.lower()}://{host}{port}{parts.path or '/'}")
        except ValueError:
            pass
    return f"{method} {shown}"


def refusal_line(params, reason: str) -> str:
    """The log line for a request refused before a Relay existed (R10)."""
    return _clean(f"http {_display(params)} -> refused: {reason}", 1000) + "\n"


class _Pinned:
    """Mixin: connect to an address dock already checked, never to whatever the name resolves to now."""

    def _dial(self):
        return self._relay._dial(self._addrs, self.port)


class _HTTP(_Pinned, http.client.HTTPConnection):
    def __init__(self, host, port, addrs, relay):
        super().__init__(host, port, timeout=relay.limits.idle_timeout)
        self._addrs, self._relay = addrs, relay

    def connect(self):
        self.sock = self._dial()


class _HTTPS(_Pinned, http.client.HTTPSConnection):
    def __init__(self, host, port, addrs, relay):
        super().__init__(host, port, context=relay.tls_context(), timeout=relay.limits.idle_timeout)
        self._addrs, self._relay = addrs, relay

    def connect(self):
        raw = self._dial()
        try:
            # SNI and verification use the name. The handshake is done below, on the socket the relay can close:
            # wrapping takes over the raw socket's descriptor, and a handshake inside wrap_socket could not be cancelled.
            sock = self._context.wrap_socket(raw, server_hostname=self.host, do_handshake_on_connect=False)
        except BaseException:
            raw.close()
            raise
        self.sock = sock
        try:
            self._relay._track(sock)
            self._relay.check_stopped()  # a cancel that came before the socket was tracked has shut it down already
            try:
                sock.do_handshake()
            except AttributeError:  # the peer was gone before there was a handshake to do: ssl had no connection to wrap
                raise ConnectionResetError("the connection closed before the TLS handshake") from None
        except BaseException:
            sock.close()
            raw.close()  # a no-op once wrapped; the socket must not outlive a failed handshake whatever happened above
            self.sock = None
            raise


class Relay:
    """One relayed request. `run` blocks and streams through `send_event`; `cancel` may come from another thread."""

    def __init__(self, rules, limits: Limits, send_event: Callable[[dict], None], log: Callable[[str], None]):
        self.rules, self.limits = rules, limits
        self._send_event, self._log = send_event, log
        self._lock = threading.Lock()
        self._cancelled = False
        self._expired = False  # the total deadline passed
        self._deadline = float("inf")
        self._sock: socket.socket | None = None
        self._context: ssl.SSLContext | None = None

    # control ---------------------------------------------------------------
    def tls_context(self) -> ssl.SSLContext:
        if self._context is None:
            self._context = tls_context()  # by default the system trust store, host names checked
        return self._context

    @property
    def stopped(self) -> bool:
        return self._cancelled or self._expired

    def cancel(self) -> None:
        """Shut the connection down from under `run`, which then raises Cancelled (and closes it)."""
        self._stop("_cancelled")

    def _expire(self) -> None:
        """The total deadline passed: shut the connection down from under `run`, which then raises Refused('timed out')."""
        self._stop("_expired")

    def _stop(self, reason: str) -> None:
        # Only shutdown(): it wakes whatever `run` is blocked on and keeps its state, so a wake-up that comes late
        # still sees it. Closing here as well would let the descriptor number be reused by another socket while
        # `run` still waits on it; `run` closes the connection itself when it ends.
        with self._lock:
            setattr(self, reason, True)
            sock = self._sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except (OSError, ValueError):
                pass

    def _track(self, sock) -> None:
        with self._lock:
            self._sock = sock
            stopped = self.stopped
        if stopped:
            self._stop("_cancelled" if self._cancelled else "_expired")

    def check_stopped(self) -> None:
        """Raise Cancelled after cancel(), Refused('timed out') after the total deadline."""
        if self._cancelled:
            raise Cancelled()
        if self._expired:
            raise Refused("timed out")

    def _dial(self, addrs, port):
        last: OSError | None = None
        for ip in addrs:
            self.check_stopped()
            left = self._deadline - time.monotonic()
            if left <= 0:
                raise Refused("timed out")
            try:
                sock = connector((ip, port), timeout=min(self.limits.idle_timeout, left))
            except OSError as e:
                last = e
                continue
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except (OSError, AttributeError):
                pass
            self._track(sock)
            return sock
        raise last or OSError("no address to connect to")

    class _Io:
        """Socket work: what goes wrong there becomes a refusal for the plugin, or Cancelled after cancel()."""

        def __init__(self, relay):
            self.relay = relay

        def __enter__(self):
            return self

        def __exit__(self, kind, error, tb):
            if error is None:
                self.relay.check_stopped()
                return False
            if isinstance(error, (Refused, Cancelled)) or not isinstance(error, (OSError, http.client.HTTPException, ValueError)):
                return False
            if self.relay._cancelled:
                raise Cancelled() from None
            if self.relay._expired or isinstance(error, TimeoutError):
                raise Refused("timed out") from None
            if isinstance(error, ssl.SSLCertVerificationError):
                raise Refused(f"TLS certificate error: {getattr(error, 'verify_message', None) or error}") from None
            if isinstance(error, ssl.SSLError):
                raise Refused(f"TLS error: {getattr(error, 'reason', None) or error}") from None
            if isinstance(error, (http.client.HTTPException, ValueError)):
                raise Refused(f"bad response: {error}") from None
            raise Refused(f"connection failed: {error.strerror or error}") from None

    # running ------------------------------------------------------------------
    def run(self, params: dict) -> dict:
        """Make the request; returns {"status", "bytes"}. Raises Refused or Cancelled; logs one line either way."""
        shown = _display(params)
        outcome = "failed"
        total = min(self.limits.total_timeout, 365 * 86400)  # a timer cannot wait for 1e300 seconds
        self._deadline = time.monotonic() + total
        timer = threading.Timer(total, self._expire)  # closes the socket, whatever it is blocked on
        timer.daemon = True
        timer.start()
        try:
            result = self._run(params)
            outcome = str(result["status"])
            return result
        except Refused as e:
            outcome = f"refused: {e}"
            raise
        except Cancelled:
            outcome = "cancelled"
            raise
        finally:
            timer.cancel()
            try:
                self._log(_clean(f"http {shown} -> {outcome}", 1000) + "\n")
            except (OSError, ValueError):
                pass  # the log is closed: the plugin is gone

    def _params(self, params):
        method = params.get("method")
        if not isinstance(method, str) or method not in METHODS:
            raise Refused(f"method must be one of {', '.join(sorted(METHODS))}")
        if not isinstance(params.get("url"), str):
            raise Refused("url must be a string")
        raw_headers = params.get("headers") or {}
        if not isinstance(raw_headers, dict):
            raise Refused("headers must be an object of strings")
        headers, size = {}, 0
        for name, value in raw_headers.items():
            if not isinstance(name, str) or not isinstance(value, str):
                raise Refused("header values must be strings")
            if not _TOKEN.fullmatch(name):
                raise Refused(f"invalid header name {_clean(name, 40)!r}")
            if _BAD_VALUE.search(value):
                raise Refused(f"invalid header value for '{name}'")
            size += len(name.encode()) + len(value.encode()) + 4
            if name.lower() not in OWNED:
                headers[name] = value.encode("utf-8")
        if size > self.limits.max_header_bytes:
            raise Refused(f"request headers larger than {self.limits.max_header_bytes} bytes")
        body = params.get("body")
        if body is not None:
            if not isinstance(body, str):
                raise Refused("body must be base64 text")
            if len(body) > self.limits.max_body_bytes // 3 * 4 + 8:
                raise Refused(f"request body larger than {self.limits.max_body_bytes} bytes")
            try:
                body = base64.b64decode(body, validate=True)
            except (binascii.Error, ValueError):
                raise Refused("body must be base64 text") from None
            if len(body) > self.limits.max_body_bytes:
                raise Refused(f"request body larger than {self.limits.max_body_bytes} bytes")
        return method, headers, body

    def _open(self, scheme, host, port, addrs):
        conn = (_HTTPS if scheme == "https" else _HTTP)(host, port, addrs, self)
        with self._Io(self):
            conn.connect()
        return conn

    def _run(self, params: dict) -> dict:
        method, headers, body = self._params(params)
        self.check_stopped()
        url, origin, redirects = params["url"], None, 0
        while True:
            rule, scheme, host, port, target = check_url(self.rules, url)
            if origin is not None and origin != (scheme, host, port):  # credentials stay with the host they were meant for
                headers = {k: v for k, v in headers.items() if k.lower() not in CREDENTIALS}
            origin = (scheme, host, port)
            addrs = checked_addresses(host, port, rule)
            self.check_stopped()
            conn = self._open(scheme, host, port, addrs)
            try:
                with self._Io(self):
                    conn.request(method, target, body=body, headers={**headers, "Connection": "close"})
                    resp = conn.getresponse()
                location = resp.getheader("location")
                if resp.status in REDIRECTS and location:
                    redirects += 1
                    if redirects > self.limits.max_redirects:
                        raise Refused(f"more than {self.limits.max_redirects} redirects")
                    url = urllib.parse.urljoin(url, location)
                    if resp.status == 303 and method != "HEAD" or resp.status in (301, 302) and method == "POST":
                        method, body = "GET", None
                        headers = {k: v for k, v in headers.items() if not k.lower().startswith("content-")}
                    continue
                return self._stream(resp, method)
            finally:
                conn.close()

    def _stream(self, resp, method: str) -> dict:
        limits = self.limits
        pairs = [[k.lower(), v] for k, v in resp.getheaders()]
        if sum(len(k) + len(v) + 4 for k, v in pairs) > limits.max_header_bytes:
            raise Refused(f"response headers larger than {limits.max_header_bytes} bytes")
        has_body = method != "HEAD" and resp.status not in (204, 304) and resp.status >= 200
        announced = resp.getheader("content-length")
        if has_body and announced and announced.isdigit() and int(announced) > limits.max_response_bytes:
            raise Refused(f"response larger than {limits.max_response_bytes} bytes")
        self._send_event({"status": resp.status, "headers": pairs})
        total = 0
        out = _Coalescer(self._send_event, limits.chunk)
        try:
            while True:
                with self._Io(self):
                    data = resp.read1(limits.chunk)
                if not data:
                    break
                room = limits.max_response_bytes - total
                if len(data) > room:
                    if room > 0:
                        out.add(data[:room])
                    out.flush()
                    raise Refused(f"response larger than {limits.max_response_bytes} bytes")
                total += len(data)
                out.add(data)
            if resp.length:  # Content-Length promised more than came
                raise Refused("response ended early")
        except Cancelled:
            out.discard()  # the plugin asked to stop: nothing more for it
            raise
        except BaseException:
            out.flush()  # what arrived before the failure is delivered, then the failure
            raise
        else:
            out.flush()
        finally:
            out.stop()
        return {"status": resp.status, "bytes": total}


class _Coalescer:
    """Body bytes -> `chunk` events. Small reads are sent together, so a drip feed is not one event per read.

    Bytes go out at once when nothing was sent for COALESCE seconds, or when a full chunk is ready; otherwise a
    timer sends them when the interval is over. Only one thing sends at a time, so the order is kept.
    """

    def __init__(self, send_event: Callable[[dict], None], chunk: int):
        self._send_event, self._chunk = send_event, chunk
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._last = float("-inf")
        self._error: BaseException | None = None  # from a send on the timer's thread, raised on the next call

    def _emit(self, final: bool) -> None:
        """Send full chunks, and the rest too if `final`. Called with the lock held."""
        while len(self._buffer) >= self._chunk or (final and self._buffer):
            piece = bytes(self._buffer[:self._chunk])
            del self._buffer[:self._chunk]
            self._last = time.monotonic()
            self._send_event({"chunk": base64.b64encode(piece).decode("ascii")})

    def _raise_error(self) -> None:
        if self._error is not None:
            error, self._error = self._error, None
            raise error

    def add(self, data: bytes) -> None:
        with self._lock:
            self._raise_error()
            self._buffer += data
            wait = self._last + COALESCE - time.monotonic()
            self._emit(final=wait <= 0)
            if self._buffer and self._timer is None:
                self._timer = threading.Timer(max(wait, 0.0), self._tick)
                self._timer.daemon = True
                self._timer.start()

    def _tick(self) -> None:
        with self._lock:
            self._timer = None
            try:
                self._emit(final=True)
            except BaseException as e:  # noqa: BLE001 - the reading thread raises it
                self._error = e
                self._buffer.clear()

    def flush(self) -> None:
        with self._lock:
            self._raise_error()
            self._emit(final=True)

    def discard(self) -> None:
        with self._lock:
            self._buffer.clear()
            self._error = None

    def stop(self) -> None:
        with self._lock:
            timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()
