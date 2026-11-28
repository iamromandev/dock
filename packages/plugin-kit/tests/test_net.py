"""check_url and the cancellable request, against stand-in servers on localhost."""

import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from sample_plugin import Plugin

from dock_plugin import net
from dock_plugin.cancel import Call, Failure

ARGS = {"service": "svc", "var": "SVC_URL", "start": "run-svc", "read_timeout": 30, "max_reply": 100}


class Server:
    """Answers every POST with `reply` = dict(status, raw, block, drop); a blocking reply waits for `release`."""

    def __init__(self, **reply):
        self.release = threading.Event()
        self.seen: list[tuple[str, str | None, bytes]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                outer.seen.append((self.path, self.headers.get("Content-Type"), body))
                if reply.get("drop"):
                    return  # closes the connection without an answer
                if reply.get("block"):
                    outer.release.wait(30)
                data = reply.get("raw", b"ok")
                try:
                    self.send_response(reply.get("status", 200))
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:
                    pass

            def log_message(self, format, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def serve():
    made = []

    def make(**reply):
        made.append(Server(**reply))
        return made[-1]

    yield make
    for s in made:
        s.close()


@pytest.mark.parametrize("url", ["http://localhost", "http://127.0.0.1:7862", "http://u:p@host:1/x", "http://h:65535"])
def test_good_urls_pass(url):
    net.check_url(url, "V")


@pytest.mark.parametrize("url", ["https://h", "ftp://h", "http://", "http://h:0", "http://h:65536", "http://h:x", "h:80", ""])
def test_bad_urls_name_the_variable(url):
    with pytest.raises(Failure) as caught:
        net.check_url(url, "SVC_URL")
    assert str(caught.value) == "SVC_URL is not a valid http URL"


def test_credentials_are_not_shown():
    assert net.shown("http://user:secret@host:80/p") == "http://host:80"
    assert net.shown("host") == "host"


def test_a_request_sends_the_body_and_returns_status_and_data(serve):
    s = serve(status=201, raw=b"fine")
    assert net.request(s.url + "/base/", "/go?x=1", b"body", Call(), **ARGS) == (201, b"fine")
    assert s.seen == [("/base/go?x=1", "application/octet-stream", b"body")]


def test_an_unreachable_server_names_the_url_how_to_start_it_and_the_variable():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    with pytest.raises(Failure) as caught:
        net.request(f"http://127.0.0.1:{port}", "/go", b"", Call(), **ARGS)
    message = str(caught.value)
    assert message.startswith(f"cannot reach the svc server at http://127.0.0.1:{port}: ")
    assert message.endswith(". Start it with `run-svc`; set SVC_URL if it runs elsewhere")


def test_a_server_that_never_answers_times_out_with_its_own_message(serve):
    s = serve(block=True)
    started = time.monotonic()
    with pytest.raises(Failure) as caught:
        net.request(s.url, "/go", b"", Call(), **{**ARGS, "read_timeout": 0.3})
    assert str(caught.value) == "timed out after 0.3 s waiting for the svc server"
    assert time.monotonic() - started < 5  # the 10 s connect timeout does not apply to the answer


def test_a_server_that_hangs_up_has_stopped_answering_not_unreachable(serve):
    s = serve(drop=True)
    with pytest.raises(Failure) as caught:
        net.request(s.url, "/go", b"", Call(), **ARGS)
    assert str(caught.value) == f"the svc server stopped answering ({s.url})"


def test_an_answer_over_the_limit_is_refused(serve):
    s = serve(raw=b"x" * 101)
    with pytest.raises(Failure) as caught:
        net.request(s.url, "/go", b"", Call(), **ARGS)
    assert str(caught.value) == f"the svc server at {s.url} sent an answer that is too large (over 100 bytes)"
    assert net.request(serve(raw=b"x" * 100).url, "/go", b"", Call(), **ARGS)[0] == 200


def test_a_cancel_while_sending_19_mb_to_a_server_that_never_reads_answers_fast():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    got_bytes, conns = threading.Event(), []

    def accept():
        try:
            conn, _ = listener.accept()
            conns.append(conn)
            conn.recv(65536)
            got_bytes.set()
        except OSError:
            pass

    threading.Thread(target=accept, daemon=True).start()
    p = Plugin()
    try:
        req = p.start("post", url=f"http://127.0.0.1:{listener.getsockname()[1]}", size=19_200_000)
        assert got_bytes.wait(30)  # the body is going out and the server has stopped reading
        started = time.monotonic()
        p.send(method="cancel", params={})
        answer = p.answer(req)
        assert time.monotonic() - started < 2
        assert answer["error"]["code"] == -32800
    finally:
        p.close()
        for c in conns:
            c.close()
        listener.close()


def test_a_worker_failure_surfaces_as_exit_1_with_the_message_logged(serve):
    s = serve(drop=True)
    p = Plugin()
    try:
        answer = p.invoke("post", url=s.url, size=1)
        assert answer["result"] == {"exit_code": 1}
        assert p.seen[-1]["params"]["message"] == f"the svc server stopped answering ({s.url})".replace("svc", "sample")
    finally:
        p.close()
