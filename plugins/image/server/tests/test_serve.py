import base64
import http.client
import json
import os
import socket
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import serve

FAKE_PNG = b"\x89PNG fake bytes"


@pytest.fixture
def server(monkeypatch):
    """The real server on a free port, with `generate` replaced; yields (port, calls)."""
    calls = []

    def fake_generate(req, on_step):
        calls.append(req)
        for n in range(1, req["steps"] + 1):
            on_step(n, req["steps"])
        return FAKE_PNG, 99 if req["seed"] is None else req["seed"]

    monkeypatch.setattr(serve, "generate", fake_generate)
    httpd = serve.make_server("127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd, calls
    httpd.shutdown()
    httpd.server_close()


def post(httpd, body):
    conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
    conn.request("POST", "/generate", body=body if isinstance(body, bytes) else json.dumps(body))
    resp = conn.getresponse()
    return resp.status, resp.read().decode()


def req(**over):
    return {"prompt": "a cat", "width": 256, "height": 256, "steps": None, "seed": None, "model": "z-image-turbo", **over}


def lines(text):
    return [json.loads(line) for line in text.splitlines()]


def test_streams_steps_then_png_with_seed(server):
    httpd, _ = server
    status, text = post(httpd, req(steps=2, seed=7))
    assert status == 200
    out = lines(text)
    assert out[:2] == [{"step": 1, "steps": 2}, {"step": 2, "steps": 2}]
    assert out[2]["seed"] == 7
    assert base64.b64decode(out[2]["png"]) == FAKE_PNG
    assert len(out) == 3


def test_model_and_steps_default_when_omitted(server):
    httpd, calls = server
    body = req()
    del body["model"], body["steps"]
    assert post(httpd, body)[0] == 200
    assert calls[0]["model"] == "z-image-turbo"
    assert calls[0]["steps"] == 9


def test_generate_failure_is_an_error_line(server, monkeypatch):
    def boom(req, on_step):
        raise RuntimeError("out of memory")

    monkeypatch.setattr(serve, "generate", boom)
    status, text = post(server[0], req())
    assert status == 200
    assert lines(text) == [{"error": "RuntimeError: out of memory"}]


def test_oserror_in_generate_is_an_error_line_not_a_disconnect(server, monkeypatch):
    # Bug: HF download errors (GatedRepoError) are OSErrors and used to close the stream silently.
    def gated(req, on_step):
        raise OSError("gated repo 401")

    monkeypatch.setattr(serve, "generate", gated)
    out = lines(post(server[0], req())[1])
    assert len(out) == 1
    assert "gated repo 401" in out[0]["error"]
    assert out[0]["error"].startswith("OSError")


def test_failure_after_a_step_gives_step_then_error(server, monkeypatch):
    def fail_late(req, on_step):
        on_step(1, 4)
        raise OSError("disk full")

    monkeypatch.setattr(serve, "generate", fail_late)
    out = lines(post(server[0], req())[1])
    assert out[0] == {"step": 1, "steps": 4}
    assert "disk full" in out[1]["error"]
    assert len(out) == 2


@pytest.mark.parametrize("body", [b"not json", json.dumps(req(model="schnell")), json.dumps({"prompt": "x"})])
def test_bad_request_is_400_with_error(server, body):
    status, text = post(server[0], body.encode() if isinstance(body, str) else body)
    assert status == 400
    assert "error" in json.loads(text)


def test_health_lists_models(server):
    conn = http.client.HTTPConnection("127.0.0.1", server[0].server_address[1], timeout=5)
    conn.request("GET", "/health")
    resp = conn.getresponse()
    assert json.loads(resp.read()) == {"ok": True, "models": ["z-image-turbo"]}


def test_binds_loopback_only(server):
    assert server[0].server_address[0] == "127.0.0.1"


def test_client_disconnect_stops_generation(server, monkeypatch):
    seen = []
    done = threading.Event()

    def long_generate(req, on_step):
        try:
            for n in range(1, 100000):
                seen.append(n)
                on_step(n, 100000)
            return FAKE_PNG, 1
        finally:
            done.set()

    monkeypatch.setattr(serve, "generate", long_generate)
    body = json.dumps(req()).encode()
    sock = socket.create_connection(("127.0.0.1", server[0].server_address[1]), timeout=5)
    sock.sendall(b"POST /generate HTTP/1.1\r\nContent-Length: %d\r\n\r\n%s" % (len(body), body))
    sock.recv(1)  # the response has started
    sock.close()
    assert done.wait(5), "generation kept running after the client left"
    assert len(seen) < 100000


@pytest.mark.parametrize(
    "over",
    [
        {"width": 240}, {"height": 2064}, {"width": 1000}, {"width": True},
        {"steps": 0}, {"steps": 51}, {"prompt": "x" * 2001},
    ],
)
def test_out_of_bounds_field_is_400_naming_it(server, over):
    status, text = post(server[0], req(**over))
    assert status == 400
    assert next(iter(over)) in json.loads(text)["error"]


@pytest.mark.parametrize("over", [{"width": 256, "height": 2048}, {"steps": 1}, {"steps": 50}, {"prompt": "x" * 2000}])
def test_boundary_values_are_accepted(server, over):
    assert post(server[0], req(**over))[0] == 200


def raw_post(httpd, header_value, body=b""):
    sock = socket.create_connection(("127.0.0.1", httpd.server_address[1]), timeout=5)
    sock.sendall(b"POST /generate HTTP/1.1\r\nContent-Length: " + header_value + b"\r\n\r\n" + body)
    data = b""
    while b"\r\n\r\n" not in data or not data.endswith(b"}"):
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
    sock.close()
    return int(data.split()[1]), data


@pytest.mark.parametrize(("value", "status"), [(b"-5", 400), (b"abc", 400), (b"65537", 413)])
def test_bad_content_length_is_answered_without_reading_body(server, value, status):
    assert raw_post(server[0], value)[0] == status


def test_missing_content_length_is_400(server):
    sock = socket.create_connection(("127.0.0.1", server[0].server_address[1]), timeout=5)
    sock.sendall(b"POST /generate HTTP/1.1\r\n\r\n")
    assert sock.recv(4096).split()[1] == b"400"
    sock.close()


class _FakeModel:
    """Stands in for mflux's ZImage: records nothing, renders a tiny image or raises."""

    def __init__(self, fail=False):
        self.fail = fail
        self.callbacks = type("C", (), {"in_loop": [], "register": lambda s, cb: s.in_loop.append(cb)})()

    def generate_image(self, **_):
        if self.fail:
            raise RuntimeError("render failed")
        from PIL import Image

        return type("G", (), {"image": Image.new("RGB", (16, 16))})()


@pytest.mark.parametrize("fail", [False, True])
def test_mlx_cache_is_freed_after_every_render(monkeypatch, fail):
    # A render leaves ~4 GB of MLX buffer cache; kept, it swaps the next render on a 16 GB Mac (2x slower).
    cleared = []
    monkeypatch.setattr(serve, "_load", lambda name: _FakeModel(fail))
    monkeypatch.setattr(serve, "_clear_cache", lambda: cleared.append(True))
    req = {"prompt": "x", "width": 256, "height": 256, "steps": 1, "seed": 1, "model": "z-image-turbo"}
    if fail:
        with pytest.raises(RuntimeError):
            serve.generate(req, lambda n, total: None)
    else:
        serve.generate(req, lambda n, total: None)
    assert cleared == [True]
