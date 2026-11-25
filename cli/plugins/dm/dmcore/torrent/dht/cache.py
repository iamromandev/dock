"""The node cache: DHT nodes that answered, kept between runs in the plugin's data folder.

It holds node ids and addresses only, never an info-hash or a peer. The file is untrusted when read.
"""

import os
import socket
import stat
import struct
import tempfile
import time
from pathlib import Path

from .krpc import NODE_SIZE, Node, usable

CACHE_NAME = "dht-nodes.dat"
MAX_CACHE_BYTES = 16 * 1024
MAX_CACHE_NODES = 200
MAGIC = b"DMDHT1\n"
TEMP_PREFIX = ".dht-"
STALE_TEMP_SECONDS = 3600
MAX_SCAN = 1000  # directory entries looked at


def _remove_stale_temp_files(folder, max_age=STALE_TEMP_SECONDS):
    """Delete .dht-* files that an interrupted save() left behind, once they are older than max_age."""
    try:
        with os.scandir(folder) as entries:
            for _, entry in zip(range(MAX_SCAN), entries):
                try:
                    if (entry.name.startswith(TEMP_PREFIX) and not entry.is_dir(follow_symlinks=False)
                            and time.time() - entry.stat(follow_symlinks=False).st_mtime > max_age):
                        os.unlink(entry.path)
                except OSError:
                    pass
    except OSError:
        pass


def load(path, allow_loopback=False, allow_private=False):
    """The usable nodes in the cache file; [] when it is missing, too big, not a regular file or malformed.

    Stale temp files of an interrupted save() in the same folder are removed first.
    """
    _remove_stale_temp_files(Path(path).parent)
    flags = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
             | getattr(os, "O_CLOEXEC", 0))
    try:
        fd = os.open(path, flags)
    except OSError:
        return []
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CACHE_BYTES:
            return []
        data = b""
        while len(data) <= MAX_CACHE_BYTES:
            chunk = os.read(fd, MAX_CACHE_BYTES + 1 - len(data))
            if not chunk:
                break
            data += chunk
    except OSError:
        return []
    finally:
        os.close(fd)
    body = data[len(MAGIC):]
    if len(data) > MAX_CACHE_BYTES or not data.startswith(MAGIC) or len(body) % NODE_SIZE:
        return []
    nodes = []
    for i in range(0, min(len(body), MAX_CACHE_NODES * NODE_SIZE), NODE_SIZE):
        chunk = body[i:i + NODE_SIZE]
        addr = socket.inet_ntoa(chunk[20:24]), struct.unpack(">H", chunk[24:26])[0]
        if usable(addr, allow_loopback, allow_private):
            nodes.append(Node(chunk[:20], addr))
    return nodes


def save(path, nodes):
    """Write up to MAX_CACHE_NODES nodes atomically.

    Any OSError is swallowed: a cache never fails a download.
    """
    parts = [MAGIC]
    for node in nodes:
        if len(parts) > MAX_CACHE_NODES:
            break
        if (isinstance(node.id, bytes) and len(node.id) == 20
                and usable(node.addr, allow_loopback=True, allow_private=True)):
            parts.append(node.id + socket.inet_aton(node.addr[0]) + struct.pack(">H", node.addr[1]))
    path = Path(path)
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(prefix=TEMP_PREFIX, dir=path.parent)
        with os.fdopen(fd, "wb") as f:
            f.write(b"".join(parts))
        os.replace(tmp, path)
        tmp = None
    except OSError:
        pass
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass
