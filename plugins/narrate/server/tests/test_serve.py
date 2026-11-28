import base64
import http.client
import io
import json
import os
import socket
import struct
import sys
import threading
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import serve

HOST = "host.docker.internal:7861"
FAKE_PCM = struct.pack("<4h", 0, 1000, -1000, 32767)


@pytest.fixture
def server(monkeypatch):
    """The real server on a free port, with `speak` replaced; yields (httpd, calls)."""
    calls = []

    def fake_speak(req):
        calls.append(req)
        return FAKE_PCM

    monkeypatch.setattr(serve, "speak", fake_speak)
    httpd = serve.make_server("127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd, calls
    httpd.shutdown()
    httpd.server_close()


def call(httpd, method, path, body=None, headers_over=None):
    conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
    if isinstance(body, dict):
        body = json.dumps(body)
    headers = {"Host": HOST}
    if body is not None:
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=body, headers={**headers, **(headers_over or {})})
    resp = conn.getresponse()
    return resp.status, json.loads(resp.read())


def req(**over):
    return {"text": "Hello there.", "voice": "af_heart", "speed": 1.0, "model": "kokoro", **over}


def test_health_lists_models_and_voices(server):
    status, out = call(server[0], "GET", "/health")
    assert status == 200
    assert out == {"ok": True, "models": ["kokoro"], "voices": ["af_heart", "af_bella", "am_michael", "am_fenrir"]}


def test_unknown_path_is_404(server):
    assert call(server[0], "GET", "/nope")[0] == 404
    assert call(server[0], "POST", "/nope", "{}")[0] == 404


def test_speak_returns_base64_pcm_and_sample_rate(server):
    status, out = call(server[0], "POST", "/speak", req())
    assert status == 200
    assert base64.b64decode(out["audio"]) == FAKE_PCM
    assert out["sample_rate"] == 24000


def test_model_is_optional_and_defaults_to_kokoro(server):
    body = req()
    del body["model"]
    assert call(server[0], "POST", "/speak", body)[0] == 200
    assert server[1][0]["model"] == "kokoro"


@pytest.mark.parametrize(
    ("over", "word"),
    [
        ({"text": None}, "text"), ({"text": 5}, "text"), ({"text": ""}, "text"), ({"text": " \n\t"}, "text"),
        ({"text": "x" * 1001}, "text"),
        ({"voice": None}, "voice"), ({"voice": "zz_nobody"}, "voice"),
        ({"speed": None}, "speed"), ({"speed": "1"}, "speed"), ({"speed": True}, "speed"),
        ({"speed": 0.4}, "speed"), ({"speed": 2.1}, "speed"),
        ({"model": "bark"}, "model"),
    ],
)
def test_invalid_field_is_400_naming_it(server, over, word):
    status, out = call(server[0], "POST", "/speak", req(**over))
    assert status == 400
    assert word in out["error"]


@pytest.mark.parametrize("key", ["text", "voice", "speed"])
def test_missing_field_is_400(server, key):
    body = req()
    del body[key]
    status, out = call(server[0], "POST", "/speak", body)
    assert status == 400
    assert key in out["error"]


@pytest.mark.parametrize("body", ["not json", "[1]", '"x"'])
def test_bad_json_or_non_object_is_400(server, body):
    assert call(server[0], "POST", "/speak", body)[0] == 400


@pytest.mark.parametrize("over", [{"text": "x" * 1000}, {"speed": 0.5}, {"speed": 2}, {"speed": 2.0}])
def test_boundary_values_are_accepted(server, over):
    assert call(server[0], "POST", "/speak", req(**over))[0] == 200


def raw_post(httpd, header):
    sock = socket.create_connection(("127.0.0.1", httpd.server_address[1]), timeout=5)
    sock.sendall(b"POST /speak HTTP/1.1\r\nHost: " + HOST.encode() + b"\r\nContent-Type: application/json\r\n" + header + b"\r\n")
    data = sock.recv(4096)
    sock.close()
    return int(data.split()[1])


@pytest.mark.parametrize(
    ("header", "status"),
    [(b"Content-Length: 65537\r\n", 413), (b"Content-Length: abc\r\n", 400), (b"Content-Length: -5\r\n", 400), (b"", 400),
     (b"Content-Length: 1_0\r\n", 400), (b"Content-Length: +5\r\n", 400)],
)
def test_bad_content_length_is_answered_without_reading_body(server, header, status):
    assert raw_post(server[0], header) == status


def test_model_failure_is_500_with_error_and_server_keeps_serving(server, monkeypatch, capsys):
    good = serve.speak

    def boom(req):
        raise RuntimeError("out of " + "memory " * 200)

    monkeypatch.setattr(serve, "speak", boom)
    status, out = call(server[0], "POST", "/speak", req())
    assert status == 500
    assert out["error"].startswith("RuntimeError: out of memory")
    assert len(out["error"]) <= 500
    assert "Traceback" in capsys.readouterr().err
    monkeypatch.setattr(serve, "speak", good)
    assert call(server[0], "POST", "/speak", req())[0] == 200


def test_client_gone_during_write_does_not_stop_the_server(server, monkeypatch):
    entered, release, written = threading.Event(), threading.Event(), threading.Event()
    orig_json = serve.Handler._json

    def slow_speak(r):
        entered.set()
        release.wait(5)
        return FAKE_PCM

    def json_then_signal(self, status, obj):
        orig_json(self, status, obj)
        written.set()

    monkeypatch.setattr(serve, "speak", slow_speak)
    monkeypatch.setattr(serve.Handler, "_json", json_then_signal)
    sock = socket.create_connection(("127.0.0.1", server[0].server_address[1]), timeout=5)
    body = json.dumps(req()).encode()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    sock.sendall(b"POST /speak HTTP/1.1\r\nHost: %s\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n%s"
                 % (HOST.encode(), len(body), body))
    assert entered.wait(5)
    sock.close()  # RST
    release.set()
    assert written.wait(5)
    monkeypatch.setattr(serve, "speak", lambda r: FAKE_PCM)
    assert call(server[0], "POST", "/speak", req())[0] == 200


def test_json_survives_a_broken_pipe_and_closes_the_connection():
    class Gone(io.BufferedIOBase):
        def write(self, data):
            raise BrokenPipeError

    class Quiet(serve.Handler):
        def send_response(self, code, message=None):
            pass

        def send_header(self, keyword, value):
            pass

        def end_headers(self):
            pass

    h = Quiet.__new__(Quiet)
    h.wfile = Gone()
    h.close_connection = False
    h._json(200, {})
    assert h.close_connection is True


def test_third_request_is_503_while_one_runs_and_one_waits(server, monkeypatch):
    gate, entered = threading.Event(), []

    def blocked(r):
        entered.append(r)
        gate.wait(5)
        return FAKE_PCM

    monkeypatch.setattr(serve, "speak", blocked)
    results = []
    threads = [threading.Thread(target=lambda: results.append(call(server[0], "POST", "/speak", req())[0])) for _ in range(2)]
    for t in threads:
        t.start()
    for _ in range(100):
        if len(entered) == 2:
            break
        threading.Event().wait(0.05)
    assert len(entered) == 2
    status, out = call(server[0], "POST", "/speak", req())
    assert (status, out) == (503, {"error": "busy: a request is already waiting"})
    assert len(entered) == 2
    gate.set()
    for t in threads:
        t.join(5)
    assert results == [200, 200]
    assert call(server[0], "POST", "/speak", req())[0] == 200


def test_read_timeout_closes_a_stalled_connection(server, monkeypatch):
    assert serve.Handler.timeout == serve.READ_TIMEOUT == 30
    monkeypatch.setattr(serve.Handler, "timeout", 0.5)
    sock = socket.create_connection(("127.0.0.1", server[0].server_address[1]), timeout=5)
    sock.sendall(b"POST /speak HTTP/1.1\r\nHost: %s\r\nContent-Type: application/json\r\nContent-Length: 100\r\n\r\nab" % HOST.encode())
    try:
        assert sock.recv(4096) == b""
    except ConnectionResetError:
        pass
    sock.close()
    assert call(server[0], "POST", "/speak", req())[0] == 200


@pytest.mark.parametrize("ctype", [None, "text/plain", "application/jsonx"])
def test_post_without_json_content_type_is_415(server, ctype):
    sock = socket.create_connection(("127.0.0.1", server[0].server_address[1]), timeout=5)
    ct = b"" if ctype is None else b"Content-Type: " + ctype.encode() + b"\r\n"
    sock.sendall(b"POST /speak HTTP/1.1\r\nHost: " + HOST.encode() + b"\r\n" + ct + b"Content-Length: 2\r\n\r\n{}")
    data = sock.recv(4096)
    sock.close()
    assert data.split()[1] == b"415"
    assert b"Content-Type must be application/json" in data


@pytest.mark.parametrize("ctype", ["application/json; charset=utf-8", "Application/JSON"])
def test_json_content_type_parameters_and_case_are_accepted(server, ctype):
    assert call(server[0], "POST", "/speak", req(), {"Content-Type": ctype})[0] == 200


@pytest.mark.parametrize("host", ["evil.com", "127.0.0.1.evil.com", "localhost.evil.com:7861", "[::1]:7861"])
@pytest.mark.parametrize(("method", "path"), [("GET", "/health"), ("GET", "/nope"), ("POST", "/speak")])
def test_foreign_host_is_403(server, host, method, path):
    status, out = call(server[0], method, path, req() if method == "POST" else None, {"Host": host})
    assert (status, out) == (403, {"error": "forbidden host"})


def test_missing_host_is_403(server):
    sock = socket.create_connection(("127.0.0.1", server[0].server_address[1]), timeout=5)
    sock.sendall(b"GET /health HTTP/1.0\r\n\r\n")
    assert sock.recv(4096).split()[1] == b"403"
    sock.close()


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "host.docker.internal", "LOCALHOST:80", "127.0.0.1:7861"])
def test_allowed_hosts(server, host):
    assert call(server[0], "GET", "/health", headers_over={"Host": host})[0] == 200


@pytest.mark.parametrize("method", ["PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
def test_other_methods_are_405(server, method):
    conn = http.client.HTTPConnection("127.0.0.1", server[0].server_address[1], timeout=5)
    conn.request(method, "/speak", headers={"Host": HOST})
    resp = conn.getresponse()
    body = resp.read()
    assert resp.status == 405
    if method != "HEAD":
        assert json.loads(body) == {"error": "method not allowed"}


class _Part:
    def __init__(self, audio):
        self.audio = audio


class _FakeModel:
    def __init__(self, sample_rate=24000):
        self.sample_rate = sample_rate
        self.calls = []

    def generate(self, text, voice, speed, lang_code):
        self.calls.append((text, voice, speed, lang_code))
        yield _Part([0.0, 0.5])
        yield _Part([-2.0, 2.0])  # out of range: must clip


@pytest.fixture
def fake_loader(monkeypatch):
    """Replace mlx_audio's load_model with a counting fake and reset the model cache."""
    model, loads = _FakeModel(), []

    def load_model(repo):
        loads.append(repo)
        return model

    utils = types.SimpleNamespace(load_model=load_model)
    for name, mod in (("mlx_audio", types.ModuleType("mlx_audio")), ("mlx_audio.tts", types.ModuleType("mlx_audio.tts")), ("mlx_audio.tts.utils", utils)):
        monkeypatch.setitem(sys.modules, name, mod)
    monkeypatch.setattr(serve, "_model", None, raising=False)
    return model, loads


def test_speak_loads_model_once_and_encodes_clipped_int16(fake_loader):
    model, loads = fake_loader
    r = {"text": "Hi.", "voice": "am_fenrir", "speed": 1.5, "model": "kokoro"}
    first = serve.speak(r)
    assert serve.speak(r) == first
    assert loads == ["mlx-community/Kokoro-82M-bf16"]
    assert model.calls[0] == ("Hi.", "am_fenrir", 1.5, "a")
    assert struct.unpack("<4h", first) == (0, 16383, -32767, 32767)


R = {"text": "Hi.", "voice": "af_heart", "speed": 1.0, "model": "kokoro"}


@pytest.fixture
def cache_clears(monkeypatch):
    n = []
    monkeypatch.setattr(serve, "_clear_cache", lambda: n.append(1))
    return n


def test_no_audio_is_an_error(fake_loader):
    fake_loader[0].generate = lambda **k: iter(())
    with pytest.raises(RuntimeError, match="model produced no audio"):
        serve.speak(R)


def test_nan_and_inf_samples_are_replaced(fake_loader):
    fake_loader[0].generate = lambda **k: iter([_Part([float("nan"), float("inf"), float("-inf")])])
    assert struct.unpack("<3h", serve.speak(R)) == (0, 32767, -32767)


def test_generation_is_serialized(fake_loader):
    model = fake_loader[0]
    state = {"now": 0, "max": 0}

    def generate(**k):
        state["now"] += 1
        state["max"] = max(state["max"], state["now"])
        threading.Event().wait(0.1)
        state["now"] -= 1
        yield _Part([0.1])

    model.generate = generate
    threads = [threading.Thread(target=serve.speak, args=(R,)) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert state["max"] == 1


def test_failed_generation_releases_lock_and_clears_cache(fake_loader, cache_clears):
    model = fake_loader[0]
    good = model.generate

    def boom(**k):
        raise RuntimeError("x")
        yield

    model.generate = boom
    with pytest.raises(RuntimeError):
        serve.speak(R)
    assert cache_clears == [1]
    model.generate = good
    assert serve.speak(R)
    assert cache_clears == [1, 1]


def test_loader_failure_leaves_model_unset_and_retries(fake_loader, monkeypatch):
    utils = sys.modules["mlx_audio.tts.utils"]
    good = utils.load_model

    def failing(repo):
        raise OSError("net")

    monkeypatch.setattr(utils, "load_model", failing, raising=False)
    with pytest.raises(OSError):
        serve.speak(R)
    assert serve._model is None
    monkeypatch.setattr(utils, "load_model", good, raising=False)
    assert serve.speak(R)


def test_unexpected_sample_rate_is_an_error(fake_loader):
    fake_loader[0].sample_rate = 22050
    with pytest.raises(RuntimeError, match="22050"):
        serve.speak({"text": "Hi.", "voice": "af_heart", "speed": 1.0, "model": "kokoro"})


def test_main_binds_loopback_on_env_port(monkeypatch):
    seen = []

    class Stub:
        def serve_forever(self):
            pass

    monkeypatch.setattr(serve, "make_server", lambda host, port: seen.append((host, port)) or Stub())
    monkeypatch.setenv("DOCK_NARRATE_PORT", "7999")
    serve.main()
    monkeypatch.delenv("DOCK_NARRATE_PORT")
    serve.main()
    assert seen == [("127.0.0.1", 7999), ("127.0.0.1", 7861)]
