"""Test HTTP server for the dm end-to-end tests (stdlib only; runs in a python container)."""

import hashlib
import json
import os
import re
import sys
import threading
import time
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

BIG = 256 * 1024 * 1024
CHUNK = 64 * 1024
REQUESTS = []  # every path requested, for /_requests
LOG = []  # one dict per request with its Range and If-Range headers, for /_log
SLOW_SEEN = set()  # path+query of requests that were already served slowly
OVERRIDE = {}  # path -> query flags that replace the request's own (in-process tests change the server's mode)


def content(size, version=0):
    """Deterministic bytes: the sha256 of a counter, repeated (another sequence for each version)."""
    out, i, have = [], 0, 0
    while have < size:
        block = hashlib.sha256((str(i) if version == 0 else f"v{version}-{i}").encode()).digest()
        out.append(block)
        have += len(block)
        i += 1
    return b"".join(out)[:size]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def head(self, code, headers, length=None):
        self.send_response(code)
        for k, v in headers:
            self.send_header(k, v)
        if length is not None:
            self.send_header("Content-Length", str(length))
        else:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()

    def body(self, data, headers=(), code=200):
        self.head(code, [("Content-Type", "application/octet-stream"), *headers], len(data))
        self.wfile.write(data)

    def range_body(self, path, query, q, size):
        """/range/<name>?size=N with Range and If-Range support.

        etag=strong|weak|none (default strong), lm=1 adds Last-Modified, version=N changes the content and
        validators, ignore_range=1 always answers 200, drop_at=N closes the connection once byte N was sent
        (only for requests that start at 0), slow=S spreads the body over S seconds (first request only),
        fail_resume=CODE answers that status to any request with a Range header, stall=S accepts a request
        with a Range header and stays silent for S seconds, extra=N makes a 206 chunked (no Content-Length)
        with N bytes more than the range holds.
        """
        q = {**q, **OVERRIDE.get(path, {})}
        version = int(q.get("version", 0))
        data = content(size, version)
        etag = q.get("etag", "strong")
        validators = []
        if etag != "none":
            validators.append(("ETag", ('W/' if etag == "weak" else "") + f'"v{version}-{size}"'))
        if q.get("lm"):
            validators.append(("Last-Modified", formatdate(1_700_000_000 + version * 1000, usegmt=True)))
        have = dict(validators)
        rng = self.headers.get("Range")
        if rng and q.get("fail_resume"):
            self.body(b"fail", code=int(q["fail_resume"]))
            return
        if rng and q.get("stall"):
            time.sleep(float(q["stall"]))
            self.close_connection = True
            return
        start = 0
        match = re.fullmatch(r"bytes=(\d+)-(\d*)", rng or "")
        cond = self.headers.get("If-Range")
        if match and not q.get("ignore_range") and (cond is None or cond in have.values()):
            start = int(match.group(1))
            if start >= size:
                self.body(b"", [("Content-Range", f"bytes */{size}")], 416)
                return
        extra = [("Accept-Ranges", "bytes"), *validators]
        if start:
            extra.append(("Content-Range", f"bytes {start}-{size - 1}/{size}"))
        end = size
        if q.get("drop_at") and start == 0 and int(q["drop_at"]) < size:
            end = int(q["drop_at"])
        if start and q.get("extra"):
            self.send_response(206)
            for k, v in [("Content-Type", "application/octet-stream"), *extra, ("Transfer-Encoding", "chunked")]:
                self.send_header(k, v)
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            try:
                payload = data[start:] + b"x" * int(q["extra"])
                self.wfile.write(f"{len(payload):x}\r\n".encode() + payload + b"\r\n0\r\n\r\n")
            except OSError:
                pass
            return
        self.head(206 if start else 200, [("Content-Type", "application/octet-stream"), *extra], size - start)
        slow = float(q.get("slow", 0))
        key = f"{path}?{query}"
        if key in SLOW_SEEN:
            slow = 0
        SLOW_SEEN.add(key)
        step = max(1, size // 40) if slow else CHUNK
        try:
            for i in range(start, end, step):
                self.wfile.write(data[i:min(i + step, end)])
                self.wfile.flush()
                if slow:
                    time.sleep(slow / 40)
        except OSError:
            pass
        if end < size:
            self.close_connection = True

    def do_GET(self):
        parts = urlsplit(self.path)
        path, q = parts.path, {k: v[0] for k, v in parse_qs(parts.query).items()}
        size = int(q.get("size", 0))
        if path == "/_requests":
            self.body("\n".join(REQUESTS).encode())
            return
        if path == "/_log":
            self.body(json.dumps(LOG).encode())
            return
        REQUESTS.append(path)
        LOG.append({"path": path, "range": self.headers.get("Range"), "if_range": self.headers.get("If-Range")})
        if path.startswith("/file/"):
            self.body(content(size))
        elif path == "/cd":
            self.body(content(size), [("Content-Disposition", q.get("v", ""))])
        elif path.startswith("/redirect/"):
            n = int(path.rsplit("/", 1)[1])
            self.body(b"", [("Location", f"/redirect/{n - 1}" if n else "/final/data.csv")], 302)
        elif path == "/final/data.csv":
            self.body(b"a,b\n1,2\n")
        elif path == "/to-file":
            self.body(b"", [("Location", "file:///etc/passwd")], 302)
        elif path.startswith("/status/"):
            self.body(b"nope", code=int(path.rsplit("/", 1)[1]))
        elif path.startswith("/nolength/"):
            self.head(200, [("Content-Type", "application/octet-stream")])
            self.wfile.write(content(size))
        elif path.startswith("/truncate/"):
            self.head(200, [("Content-Type", "application/octet-stream")], size)
            self.wfile.write(content(size)[: size // 2])
            self.wfile.flush()
            self.close_connection = True
        elif path.startswith("/slow/"):
            seconds = float(q.get("seconds", 1))
            data = content(size)
            step = max(1, size // 20)
            self.head(200, [("Content-Type", "application/octet-stream")], size)
            try:
                for i in range(0, size, step):
                    self.wfile.write(data[i:i + step])
                    self.wfile.flush()
                    time.sleep(seconds / 20)
            except OSError:
                pass
        elif path.startswith("/range/"):
            self.range_body(path, parts.query, q, size)
        elif path == "/big":
            self.head(200, [("Content-Type", "application/octet-stream")], BIG)
            block = content(CHUNK)
            try:
                for _ in range(BIG // CHUNK):
                    self.wfile.write(block)
            except OSError:
                pass
        else:
            self.body(b"not found", code=404)


if __name__ == "__main__":
    ThreadingHTTPServer.daemon_threads = True
    # exit by itself so a stray container does not live on: --max-seconds N (default 900)
    limit = float(sys.argv[sys.argv.index("--max-seconds") + 1]) if "--max-seconds" in sys.argv else 900.0
    timer = threading.Timer(limit, lambda: os._exit(0))
    timer.daemon = True
    timer.start()
    srv = ThreadingHTTPServer(("0.0.0.0", 8000), Handler)
    print("ready", flush=True)
    srv.serve_forever()
