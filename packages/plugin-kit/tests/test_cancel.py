"""Call, sockets and the worker thread."""

import socket
import subprocess
import sys
import threading
import time
from unittest import mock

import pytest

from dock_plugin import cancel, net
from dock_plugin.cancel import Call, Cancelled, Failure


@pytest.fixture(autouse=True)
def clean_cancel():
    cancel.cancelled.clear()
    yield
    cancel.cancelled.clear()


def test_a_stopped_call_refuses_sockets_and_closes_them():
    call = Call()
    call.stop()
    sock = socket.socket()
    with pytest.raises(Cancelled):
        call.register(sock)
    assert sock.fileno() == -1


def test_stop_shuts_the_socket_down_not_only_closes_it():
    a, b = socket.socketpair()
    keeper = a.dup()  # another holder of the connection: close() alone would leave it open, shutdown ends it
    call = Call()
    call.register(a)
    call.stop()
    b.settimeout(5)
    assert b.recv(1) == b""
    keeper.close()
    b.close()


def test_stop_kills_tracked_processes_and_a_process_tracked_later():
    call = Call()
    sleeper = [sys.executable, "-c", "import time; time.sleep(60)"]
    first = subprocess.Popen(sleeper)
    call.track(first)
    call.stop()
    late = subprocess.Popen(sleeper)
    call.track(late)
    assert first.wait(10) != 0
    assert late.wait(10) != 0


def test_a_cancel_while_connecting_closes_the_connecting_socket():
    call = Call()
    entered, freed = threading.Event(), threading.Event()

    def hung_connect(sock, addr):  # stands in for a connect to a black hole; only a close ends it
        entered.set()
        freed.wait(5)
        raise OSError("closed")

    def watch():
        entered.wait(5)
        call.stop()
        freed.set()

    threading.Thread(target=watch, daemon=True).start()
    started = time.monotonic()
    with mock.patch.object(socket.socket, "connect", hung_connect), pytest.raises(Cancelled):
        net.request("http://127.0.0.1:9", "/x", b"", call, service="s", var="V", start="go", read_timeout=5,
                    max_reply=10)
    assert time.monotonic() - started < 2
    assert call.sock is not None
    assert call.sock.fileno() == -1


def test_open_socket_reads_with_the_read_timeout_after_connecting():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    sock = cancel.open_socket("127.0.0.1", listener.getsockname()[1], Call(), 3, 7)
    assert sock.gettimeout() == 7
    sock.close()
    listener.close()


def test_run_worker_returns_the_result():
    assert cancel.run_worker(lambda a, b: {"sum": a + b}, (1, 2), Call()) == {"sum": 3}


def test_a_failure_in_the_worker_is_raised_as_it_was():
    def target():
        raise Failure("nope", -32602)

    with pytest.raises(Failure, match="nope") as caught:
        cancel.run_worker(target, (), Call())
    assert caught.value.code == -32602


def test_any_other_exception_in_the_worker_names_the_plugin():
    def target():
        raise ValueError("x")

    with pytest.raises(Failure, match=r"sample failed: ValueError\('x'\)"):
        cancel.run_worker(target, (), Call(), "sample")


def test_a_cancel_stops_the_call_and_returns_fast_even_if_the_worker_is_stuck():
    call, release = Call(), threading.Event()
    threading.Timer(0.05, cancel.cancelled.set).start()
    started = time.monotonic()
    with pytest.raises(Cancelled):
        cancel.run_worker(lambda: release.wait(30), (), call)
    release.set()
    assert call.stopped
    assert time.monotonic() - started < 2


def test_a_cancel_that_arrives_with_the_answer_wins():
    call = Call()

    def target():
        cancel.cancelled.set()
        return {"done": True}

    with pytest.raises(Cancelled):
        cancel.run_worker(target, (), call)
    assert call.stopped
