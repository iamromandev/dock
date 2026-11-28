import http.client
import json
import os
import socket
import sys
import threading
from typing import Any

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import serve

HOST = "host.docker.internal:7862"
RATE = 16000


def pcm(seconds=3.0, amp=0.3):
    """int16 LE mono PCM of a 440 Hz tone (amp 0 gives silence)."""
    t = np.arange(int(seconds * RATE)) / RATE
    return (np.sin(2 * np.pi * 440 * t) * amp * 32767).astype("<i2").tobytes()


def seg(start, end, text=" hi", nsp=0.0):
    return {"start": start, "end": end, "text": text, "no_speech_prob": nsp}


@pytest.fixture(autouse=True)
def fresh_slots(monkeypatch):
    """Every test gets its own slot semaphore, so a leak in one cannot starve the next."""
    monkeypatch.setattr(serve, "_slots", threading.BoundedSemaphore(2))


@pytest.fixture
def whisper(monkeypatch):
    """Replace the whisper seam and the cache clear; the stub's `result` is mutable."""
    state: dict[str, Any] = {
        "calls": [],
        "clears": 0,
        "result": {"language": "en", "segments": [seg(0.0, 1.0)]},
    }

    def fake(audio, language):
        state["calls"].append((audio, language))
        return state["result"]

    monkeypatch.setattr(serve, "_whisper", fake)
    monkeypatch.setattr(serve, "_clear_cache", lambda: state.__setitem__("clears", state["clears"] + 1))
    return state


@pytest.fixture
def server(whisper):
    httpd = serve.make_server("127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()


def call(httpd, method, path, body=None, headers_over=None):
    conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
    headers = {"Host": HOST}
    if body is not None:
        headers["Content-Type"] = "application/octet-stream"
    conn.request(method, path, body=body, headers={**headers, **(headers_over or {})})
    resp = conn.getresponse()
    data = resp.read()
    return resp.status, (json.loads(data) if data else None)


def raw(httpd, head, body=b""):
    sock = socket.create_connection(("127.0.0.1", httpd.server_address[1]), timeout=5)
    sock.sendall(head + b"\r\n" + body)
    data = sock.recv(4096)
    sock.close()
    return int(data.split()[1])


def test_health(server):
    assert call(server, "GET", "/health") == (200, {"ok": True, "model": "whisper-large-v3-turbo"})


def test_main_binds_loopback_on_env_port(monkeypatch):
    seen = []

    class Stub:
        def serve_forever(self):
            pass

    monkeypatch.setattr(serve, "make_server", lambda host, port: seen.append((host, port)) or Stub())
    monkeypatch.setenv("DOCK_TRANSCRIBE_PORT", "7999")
    serve.main()
    monkeypatch.delenv("DOCK_TRANSCRIBE_PORT")
    serve.main()
    assert seen == [("127.0.0.1", 7999), ("127.0.0.1", 7862)]


def test_segments_are_stripped_and_rounded_to_ms(whisper):
    whisper["result"] = {"language": "en", "segments": [seg(0.12349, 1.00051, "  hello \n")]}
    out = serve.transcribe(pcm(3), None)
    assert out == {"language": "en", "segments": [{"start": 0.123, "end": 1.001, "text": "hello"}]}


def test_audio_is_float32_scaled_from_int16(whisper):
    serve.transcribe(np.array([0, 16384, -32768], dtype="<i2").tobytes(), None)
    audio = whisper["calls"][0][0]
    assert audio.dtype == np.float32
    assert audio.tolist() == [0.0, 0.5, -1.0]


def test_hallucinated_silence_is_dropped_and_loud_audio_is_clamped(whisper):
    whisper["result"] = {"language": "en", "segments": [seg(0.0, 29.98, " Thank you.", 9.3e-11)]}
    assert serve.transcribe(pcm(3, amp=0), None)["segments"] == []
    assert serve.transcribe(pcm(3), None)["segments"] == [{"start": 0.0, "end": 3.0, "text": "Thank you."}]


def test_rms_is_measured_on_each_segments_own_slice(whisper):
    whisper["result"] = {"language": "en", "segments": [seg(0, 1, " tone"), seg(1, 3, " quiet")]}
    out = serve.transcribe(pcm(1) + pcm(2, amp=0), None)
    assert [s["text"] for s in out["segments"]] == ["tone"]


@pytest.mark.parametrize(("amp", "kept"), [(0.01, True), (0.003, False)])
def test_rms_threshold_keeps_quiet_speech_and_drops_near_silence(whisper, amp, kept):
    whisper["result"] = {"language": "en", "segments": [seg(0, 1)]}
    assert bool(serve.transcribe(pcm(1, amp=amp), None)["segments"]) is kept


@pytest.mark.parametrize(
    "bad",
    [seg(0.0, 1.0, nsp=0.9), seg(0.0, 1.0, text="  \n"), seg(5.0, 6.0), seg(2.0, 1.0)],
    ids=["no_speech", "empty_text", "starts_after_audio", "end_before_start"],
)
def test_unusable_segments_are_dropped(whisper, bad):
    whisper["result"] = {"language": "en", "segments": [bad, seg(1.0, 2.0, " keep")]}
    assert [s["text"] for s in serve.transcribe(pcm(3), None)["segments"]] == ["keep"]


def test_language_is_whispers_unless_forced(whisper):
    whisper["result"]["language"] = "de"
    assert serve.transcribe(pcm(1), None)["language"] == "de"
    assert whisper["calls"][-1][1] is None
    whisper["result"]["language"] = "xx"
    assert serve.transcribe(pcm(1), "fr")["language"] == "fr"
    assert whisper["calls"][-1][1] == "fr"


def test_post_returns_segments_and_passes_language(server, whisper):
    status, out = call(server, "POST", "/transcribe?language=en", pcm(3))
    assert status == 200
    assert out == {"language": "en", "segments": [{"start": 0.0, "end": 1.0, "text": "hi"}]}
    assert whisper["calls"][0][1] == "en"


@pytest.mark.parametrize(
    "path",
    [
        "/transcribe?language=xx",
        "/transcribe?language=en&language=de",
        "/transcribe?lang=en",
        "/transcribe?language=en&x=1",
        "/transcribe?language=",
    ],
)
def test_bad_query_is_400(server, path):
    status, out = call(server, "POST", path, pcm(1))
    assert status == 400
    assert "error" in out


@pytest.mark.parametrize("body", [b"", b"\x00\x01\x02"], ids=["empty", "odd"])
def test_empty_or_odd_body_is_400(server, body):
    assert call(server, "POST", "/transcribe", body)[0] == 400


@pytest.mark.parametrize(
    ("header", "status"),
    [
        (b"Content-Length: 23040001\r\n", 413),
        (b"Content-Length: abc\r\n", 400),
        (b"Content-Length: -5\r\n", 400),
        (b"", 400),
        (b"Content-Length: +5\r\n", 400),
    ],
)
def test_bad_content_length_is_answered_without_reading_body(server, header, status):
    head = (
        b"POST /transcribe HTTP/1.1\r\nHost: "
        + HOST.encode()
        + b"\r\nContent-Type: application/octet-stream\r\n"
    )
    assert raw(server, head + header) == status


def test_max_body_is_accepted(server):
    assert call(server, "POST", "/transcribe", b"\x00\x00" * (serve.MAX_BODY // 2))[0] == 200


@pytest.mark.parametrize("ctype", [None, "application/json", "application/octet-streamx"])
def test_wrong_content_type_is_415(server, ctype):
    ct = b"" if ctype is None else b"Content-Type: " + ctype.encode() + b"\r\n"
    head = b"POST /transcribe HTTP/1.1\r\nHost: " + HOST.encode() + b"\r\n" + ct + b"Content-Length: 2\r\n"
    assert raw(server, head, b"\x00\x00") == 415


def test_content_type_parameters_are_ignored(server):
    h = {"Content-Type": "application/octet-stream; charset=binary"}
    assert call(server, "POST", "/transcribe", pcm(1), h)[0] == 200


@pytest.mark.parametrize("host", ["evil.com", "127.0.0.1.evil.com", "[::1]:7862"])
@pytest.mark.parametrize(("method", "path"), [("GET", "/health"), ("POST", "/transcribe")])
def test_foreign_host_is_403(server, host, method, path):
    body = pcm(1) if method == "POST" else None
    assert call(server, method, path, body, {"Host": host}) == (403, {"error": "forbidden host"})


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "host.docker.internal", "127.0.0.1:7862"])
def test_allowed_hosts(server, host):
    assert call(server, "GET", "/health", headers_over={"Host": host})[0] == 200


def test_unknown_paths_are_404(server):
    assert call(server, "GET", "/nope")[0] == 404
    assert call(server, "POST", "/speak", pcm(1))[0] == 404
    assert call(server, "POST", "/transcribe/", pcm(1))[0] == 404


@pytest.mark.parametrize("method", ["PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
def test_other_methods_are_405(server, method):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    conn.request(method, "/transcribe", headers={"Host": HOST})
    resp = conn.getresponse()
    body = resp.read()
    assert resp.status == 405
    if method == "HEAD":
        assert resp.getheader("Content-Length") == "0"
    else:
        assert json.loads(body) == {"error": "method not allowed"}


def test_third_request_is_503_while_one_runs_and_one_waits(server, whisper, monkeypatch):
    gate, entered = threading.Event(), []

    def blocked(audio, language):
        entered.append(1)
        gate.wait(5)
        return whisper["result"]

    monkeypatch.setattr(serve, "_whisper", blocked)
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(call(server, "POST", "/transcribe", pcm(1))[0]))
        for _ in range(2)
    ]
    for t in threads:
        t.start()
    for _ in range(100):  # the second request holds a slot while it waits for the gpu lock
        if len(entered) == 1 and serve._slots._value == 0:
            break
        threading.Event().wait(0.05)
    assert len(entered) == 1
    assert call(server, "POST", "/transcribe", pcm(1)) == (
        503,
        {"error": "busy: a request is already waiting"},
    )
    gate.set()
    for t in threads:
        t.join(5)
    assert results == [200, 200]


def test_read_timeout_closes_a_stalled_connection(server):
    serve.Handler.timeout = 0.5
    try:
        sock = socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=5)
        sock.sendall(
            b"POST /transcribe HTTP/1.1\r\nHost: %s\r\nContent-Type: application/octet-stream\r\n"
            b"Content-Length: 100\r\n\r\nab" % HOST.encode()
        )
        try:
            assert sock.recv(4096) == b""
        except ConnectionResetError:
            pass
        sock.close()
    finally:
        serve.Handler.timeout = serve.READ_TIMEOUT
    assert call(server, "POST", "/transcribe", pcm(1))[0] == 200


def test_cache_is_cleared_after_success_and_after_failure(server, whisper, monkeypatch):
    assert call(server, "POST", "/transcribe", pcm(1))[0] == 200
    assert whisper["clears"] == 1

    def boom(audio, language):
        raise RuntimeError("x")

    monkeypatch.setattr(serve, "_whisper", boom)
    assert call(server, "POST", "/transcribe", pcm(1))[0] == 500
    assert whisper["clears"] == 2
    monkeypatch.setattr(serve, "_whisper", lambda a, language: whisper["result"])
    assert call(server, "POST", "/transcribe", pcm(1))[0] == 200


def test_failure_is_500_with_capped_message_and_traceback(server, monkeypatch, capsys):
    def boom(audio, language):
        raise RuntimeError("out of " + "memory " * 200)

    monkeypatch.setattr(serve, "_whisper", boom)
    status, out = call(server, "POST", "/transcribe", pcm(1))
    assert status == 500
    assert out["error"].startswith("RuntimeError: out of memory")
    assert len(out["error"]) == 500
    assert "Traceback" in capsys.readouterr().err
    assert serve._slots._value == 2  # the failed request gave its slot back


def test_busy_is_answered_before_the_body_is_read(server):
    assert serve._slots.acquire(blocking=False) and serve._slots.acquire(blocking=False)
    head = (
        b"POST /transcribe HTTP/1.1\r\nHost: "
        + HOST.encode()
        + b"\r\nContent-Type: application/octet-stream\r\nContent-Length: 1000\r\n"
    )
    assert raw(server, head) == 503  # no body is ever sent
