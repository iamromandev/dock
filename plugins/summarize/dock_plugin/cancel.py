"""Cancelling work: the signals, a Call that can hang up sockets and kill processes, and a worker thread."""

import queue
import socket
import subprocess
import threading


class Cancelled(Exception):
    """The host cancelled the invoke (or closed stdin)."""


class Failure(Exception):
    """A problem to tell the host. With no code: the message is logged as an error and the invoke answers
    exit_code 1. With a code: the invoke answers a JSON-RPC error with that code."""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


cancelled = threading.Event()  # set by the rpc reader on a cancel or a closed stdin during an invoke


def stop_if_cancelled():
    if cancelled.is_set():
        raise Cancelled()


class Call:
    """The sockets and child processes of one invoke: lets the main thread stop them on cancel. The lock is
    only held for a flag or a field, never across I/O, so stop() is never kept waiting by a slow send."""

    def __init__(self):
        self.sock: socket.socket | None = None
        self.procs: list[subprocess.Popen] = []
        self.stopped = False
        self.lock = threading.Lock()

    def register(self, sock):
        """Makes `sock` the one stop() hangs up. Raises Cancelled (closing it) if the call is already stopped."""
        with self.lock:
            if not self.stopped:
                self.sock = sock
                return
        self.hang_up(sock)
        raise Cancelled()

    def track(self, proc):
        with self.lock:
            self.procs.append(proc)
            if self.stopped:
                proc.kill()

    def cancelled(self):
        return self.stopped or cancelled.is_set()

    @staticmethod
    def hang_up(sock):
        # close() alone leaves the socket open while the worker is blocked on it; shut it down first
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()

    def stop(self):
        with self.lock:
            self.stopped = True
            sock = self.sock
            for proc in self.procs:
                try:
                    proc.kill()
                except OSError:
                    pass
        if sock is not None:
            self.hang_up(sock)


def open_socket(host, port, call, timeout, read_timeout=None):
    """A socket connected to host:port, with `timeout` seconds to connect and then `read_timeout` (default: the
    same) for each read or write. Each socket is registered with the call before it connects, so a cancel can
    close it and end the connect."""
    last = None
    for family, kind, proto, _, addr in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
        sock = socket.socket(family, kind, proto)
        try:
            call.register(sock)
            sock.settimeout(timeout)
            sock.connect(addr)
            sock.settimeout(timeout if read_timeout is None else read_timeout)
            return sock
        except OSError as e:
            sock.close()
            last = e
    raise last or OSError("no address")


def run_worker(target, args, call, name="plugin"):
    """Runs target(*args) in a daemon thread and returns its result, checking for a cancel every 0.1 s. A Failure
    in the thread is raised here, any other exception becomes Failure(f"{name} failed: {e!r}"). A cancel (also one
    that arrives together with the answer) stops the call and raises Cancelled; the thread gets 0.5 s to end and
    after the stop it says nothing more."""
    out: queue.Queue = queue.Queue()  # one per call, so a stale worker can never feed the next one

    def put(item):
        if not call.stopped:
            out.put(item)

    def work():
        try:
            put(("done", target(*args)))
        except Cancelled:
            return
        except Failure as e:
            put(("error", e))
        except Exception as e:  # never let the thread die silently
            put(("error", Failure(f"{name} failed: {e!r}")))

    thread = threading.Thread(target=work, daemon=True)
    kind = value = None
    try:
        thread.start()
        while True:
            stop_if_cancelled()
            try:
                kind, value = out.get(timeout=0.1)
            except queue.Empty:
                continue
            if cancelled.is_set():  # a cancel that arrives with the answer wins
                kind = None
                raise Cancelled()
            break
    except BaseException:
        call.stop()  # kills child processes and hangs up on the server
        raise
    finally:
        thread.join(0.5)  # after a cancel the worker is a daemon that has been hung up on; it must not delay the answer
    if kind == "error":
        raise value
    return value
