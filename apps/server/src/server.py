"""The listening socket: one thread per connection (websockets.sync.server)."""

from __future__ import annotations

import threading

from websockets.datastructures import Headers
from websockets.http11 import Response
from websockets.sync.server import Server
from websockets.sync.server import serve as ws_serve

from src.conn import Connection, origin_allowed


def serve(cfg, store) -> Server:
    """A server bound to cfg.host:cfg.port (port 0 picks a free one: read it from `.socket`). Call serve_forever()."""
    slots = threading.BoundedSemaphore(cfg.max_runs)

    def handler(ws) -> None:
        Connection(ws, cfg, store, slots).handle()

    def check_origin(_ws, request):
        if not origin_allowed(request.headers.get("Origin")):
            return Response(403, "Forbidden", Headers(), b"origin not allowed\n")
        return None

    return ws_serve(handler, cfg.host, cfg.port, process_request=check_origin)
