"""dock-server entry point: dock-server [--port N] [--max-runs N] [--in DIR] [--out DIR]"""

from __future__ import annotations

import logging
import os
import sys

from dock_host.store import Store

from src import config, server


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="dock-server: %(message)s")
    logging.getLogger("websockets").setLevel(logging.WARNING)  # its DEBUG level logs frame payloads (the token)
    store = Store()
    try:
        cfg = config.load(store, sys.argv[1:] if argv is None else argv)
    except config.ConfigError as e:
        print(f"dock-server: {e}", file=sys.stderr)
        return 1
    store.dirs = cfg.dirs
    os.chdir(cfg.dirs.out_dir)  # the folder plugins mount, as in the CLI
    try:
        srv = server.serve(cfg, store)
    except OSError as e:
        print(f"dock-server: cannot listen on {cfg.host}:{cfg.port}: {e.strerror or e}", file=sys.stderr)
        return 1
    logging.info("listening on ws://%s:%s", cfg.host, srv.socket.getsockname()[1])
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()
    return 0
