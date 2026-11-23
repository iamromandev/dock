"""Test HTTP server for the dm end-to-end tests (stdlib only; runs in a python container)."""

import hashlib
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

BIG = 256 * 1024 * 1024
CHUNK = 64 * 1024
REQUESTS = []  # every path requested, for /_requests


def content(size):
    """Deterministic bytes: the sha256 of a counter, repeated."""
    out, i, have = [], 0, 0
    while have < size:
        block = hashlib.sha256(str(i).encode()).digest()
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

    def do_GET(self):
        parts = urlsplit(self.path)
        path, q = parts.path, {k: v[0] for k, v in parse_qs(parts.query).items()}
        size = int(q.get("size", 0))
        if path == "/_requests":
            self.body("\n".join(REQUESTS).encode())
            return
        REQUESTS.append(path)
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
