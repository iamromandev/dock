import logging
import threading
import time

import pytest
import serverkit
from serverkit import TOKEN, Client
from websockets.exceptions import ConnectionClosed, InvalidStatus

from src import conn, server
from src.asks import Asks


@pytest.fixture(scope="module")
def store():
    return serverkit.make_store("probe", "uiprobe")


@pytest.fixture
def url(store):
    with serverkit.running(store) as (u, _srv):
        yield u


def test_binds_loopback_only(store):
    # bug: the server listens on 0.0.0.0 and is reachable from the network
    with serverkit.running(store) as (_url, srv):
        assert srv.socket.getsockname()[0] == "127.0.0.1"


@pytest.mark.parametrize("token", ["wrong", ""])
def test_wrong_token_gets_auth_error_and_close_and_is_not_logged(url, caplog, token):
    # bug: a wrong token is accepted, the connection stays open, or a token lands in the logs
    # websockets itself logs frame payloads at DEBUG, so the server must never run with that level for it
    caplog.set_level(logging.INFO)
    caplog.set_level(logging.DEBUG, logger="dock.server")
    c = Client(url)
    reply = c.hello(token=token)
    assert (reply["type"], reply["code"]) == ("error", "auth")
    with pytest.raises(ConnectionClosed):
        c.recv()
    assert TOKEN not in caplog.text and "wrong" not in caplog.text


def test_first_message_not_hello_is_auth_error(url):
    # bug: a client skips the handshake and runs commands unauthenticated
    c = Client(url)
    c.send(type="list", id="1")
    assert c.recv()["code"] == "auth"
    with pytest.raises(ConnectionClosed):
        c.recv()


@pytest.mark.parametrize("version", [1, 99, 2.0])
def test_unsupported_version_lists_supported_and_closes(url, version):
    # bug: version 99 is welcomed, or v1 or 2.0 pass; the error must list what is supported
    c = Client(url)
    reply = c.hello(version=version)
    assert reply["code"] == "version" and "[2]" in reply["message"]
    with pytest.raises(ConnectionClosed):
        c.recv()


def test_hello_gets_welcome(url):
    # bug: the handshake never answers with welcome
    assert Client(url).hello() == {"type": "welcome", "version": 2, "server": "0.1.0"}


def test_list_returns_installed_plugins_and_commands(url):
    # bug: list shows no commands, or the wrong ids
    c = Client(url)
    c.hello()
    c.send(type="list", id="L1")
    reply = c.recv()
    by_id = {p["id"]: {c["name"]: c for c in p["commands"]} for p in reply["plugins"]}
    probe = by_id["dev.dock.probe"]
    assert reply["id"] == "L1" and "show" in by_id["dev.dock.uiprobe"]
    assert probe["code"]["inputs"] is None and probe["code"]["summary"]
    assert probe["exitcode"]["inputs"] == [{"name": "code", "type": "int", "required": True, "default": None}]
    assert probe["noargs"]["inputs"] == []
    assert {"name": "retries", "type": "int", "required": False, "default": 3} in probe["fetch"]["inputs"]


@pytest.mark.parametrize("text", ["not json", '{"type": "nope"}'])
def test_bad_message_keeps_the_connection_open(url, text):
    # bug: junk from a client closes the connection or crashes the handler
    c = Client(url)
    c.hello()
    c.send_raw(text)
    assert c.recv()["code"] == "bad_message"
    c.send(type="list", id="2")
    assert c.recv()["type"] == "plugins"


@pytest.fixture(scope="module")
def built_store():
    return serverkit.make_store("probe", "uiprobe", prebuild=True)


def test_run_streams_ui_frames_in_order_then_done_zero(built_store):
    # bug: frames arrive out of order, are dropped, or done comes before the last frame
    with serverkit.running(built_store) as (u, _srv):
        c = Client(u)
        c.hello()
        c.send(type="run", id="r1", plugin="dev.dock.probe", command="echoargs", inputs={"k": "v"})
        frames = c.until_done("r1")
        assert [f["type"] for f in frames] == ["ui", "done"]
        assert frames[0]["method"] == "output" and '"k": "v"' in frames[0]["params"]["text"]
        assert frames[-1] == {"type": "done", "run": "r1", "code": 0}


def test_second_run_while_active_is_busy_and_first_completes(built_store):
    # bug: two runs share one connection and interleave, or the busy one kills the first
    with serverkit.running(built_store) as (u, _srv):
        c = Client(u)
        c.hello()
        c.send(type="run", id="r1", plugin="dev.dock.uiprobe", command="show", inputs={"script": "basic"})
        c.send(type="run", id="r2", plugin="dev.dock.probe", command="echoargs", inputs={})
        frames = c.until_done("r1")
        busy = [f for f in frames if f["type"] == "error"]
        assert [(f["id"], f["code"]) for f in busy] == [("r2", "busy")]
        assert frames[-1]["code"] == 0


def test_unknown_plugin_or_command_is_unknown_plugin_error(url):
    # bug: a bad name crashes the handler instead of answering
    c = Client(url)
    c.hello()
    c.send(type="run", id="r1", plugin="nope", command="x", inputs={})
    assert c.recv()["code"] == "unknown_plugin"
    c.send(type="run", id="r2", plugin="dev.dock.probe", command="nope", inputs={})
    assert c.recv()["code"] == "unknown_plugin"


def run_error(c, **fields):
    c.send(type="run", id="r1", **fields)
    return c.recv()


def authed(url):
    c = Client(url)
    c.hello()
    return c


@pytest.mark.parametrize(
    "command, inputs",
    [
        ("fetch", {}),  # missing required url
        ("fetch", {"url": 5}),  # wrong type
        ("fetch", {"url": "u", "nope": "x"}),  # unknown key
        ("noargs", {"a": "b"}),  # declares no inputs, got some
    ],
)
def test_invalid_run_inputs_are_bad_inputs_with_the_request_id(url, command, inputs):
    # bug: inputs reach the plugin unchecked, or the UsageError ends as an internal error / no reply
    reply = run_error(authed(url), plugin="dev.dock.probe", command=command, inputs=inputs)
    assert (reply["type"], reply["code"], reply["id"]) == ("error", "bad_inputs", "r1")


@pytest.mark.parametrize("fields", [{"plugin": "dev.dock.probe"}, {"command": "code"}, {"plugin": 1, "command": "code"}])
def test_run_with_missing_or_non_string_fields_is_bad_message(url, fields):
    # bug: a run without plugin/command crashes the handler or is accepted
    c = authed(url)
    c.send(type="run", id="r1", **fields)
    assert c.recv()["code"] == "bad_message"
    c.send(type="list", id="2")
    assert c.recv()["type"] == "plugins"


def test_run_without_id_is_bad_message(url):
    # bug: a run with no id is started and its frames cannot be matched
    c = authed(url)
    c.send(type="run", plugin="dev.dock.probe", command="code")
    assert c.recv()["code"] == "bad_message"


def test_defaults_are_filled_and_no_input_command_gets_empty_inputs(built_store):
    # bug: declared defaults are not applied, or a no-input command is sent anything
    with serverkit.running(built_store) as (u, _srv):
        c = authed(u)
        c.send(type="run", id="r1", plugin="dev.dock.probe", command="fetch", inputs={"url": "u"})
        frames = c.until_done("r1")
        assert '"retries": 3' in frames[0]["params"]["text"] and frames[-1]["code"] == 0
        c.send(type="run", id="r2", plugin="dev.dock.probe", command="noargs", inputs={})
        frames = c.until_done("r2")
        assert '"inputs": {}' in frames[0]["params"]["text"]


def test_plugin_exit_code_is_passed_through(built_store):
    # bug: a non-zero exit code is reported as 0 or 1
    with serverkit.running(built_store) as (u, _srv):
        c = authed(u)
        c.send(type="run", id="r1", plugin="dev.dock.probe", command="exitcode", inputs={"code": 3})
        assert c.until_done("r1")[-1] == {"type": "done", "run": "r1", "code": 3}


def test_plugin_crash_is_done_code_1_with_error_and_no_host_path(built_store):
    # bug: a failed plugin ends the run without done, or its error text leaks the host's folders
    with serverkit.running(built_store) as (u, _srv):
        c = authed(u)
        c.send(type="run", id="r1", plugin="dev.dock.probe", command="crash", inputs=None)
        last = c.until_done("r1")[-1]
        assert last["code"] == 1 and last["error"] and str(built_store.home) not in last["error"]
        c.send(type="list", id="2")  # the connection survives
        assert c.recv()["type"] == "plugins"


def test_scrub_replaces_host_paths():
    # bug: absolute host paths reach the client in error text
    assert conn.scrub("cannot open /h/dock/x and /o/y", ["/h/dock", "/o"]) == "cannot open <dock>/x and <dock>/y"


def test_internal_failure_is_internal_error_and_connection_stays_open(store, monkeypatch):
    # bug: an unexpected exception in a handler kills the connection thread silently
    with serverkit.running(store) as (u, _srv):
        c = authed(u)
        monkeypatch.setattr(store, "index", lambda: 1 / 0)
        c.send(type="list", id="L")
        assert c.recv()["code"] == "internal"
        monkeypatch.undo()
        c.send(type="list", id="L2")
        assert c.recv()["type"] == "plugins"


def test_handshake_timeout_is_auth_error_and_close(store, monkeypatch):
    # bug: a client that connects and says nothing holds a thread forever
    monkeypatch.setattr(conn, "HANDSHAKE_TIMEOUT", 0.3)
    with serverkit.running(store) as (u, _srv):
        c = Client(u)
        assert c.recv()["code"] == "auth"
        with pytest.raises(ConnectionClosed):
            c.recv()


@pytest.mark.parametrize("origin, ok", [(None, True), ("http://localhost:3000", True), ("http://127.0.0.1", True), ("https://evil.example", False), ("http://localhost.evil.example", False)])
def test_origin_is_checked_at_the_handshake(url, origin, ok):
    # bug: any web page can open a socket to the local server (cross-site WebSocket hijacking)
    if ok:
        assert authed_origin(url, origin).hello()["type"] == "welcome"
    else:
        with pytest.raises(InvalidStatus):
            Client(url, origin=origin)


def authed_origin(url, origin):
    return Client(url, origin=origin)


def start_slow(c, run="r1"):
    c.send(type="run", id=run, plugin="dev.dock.uiprobe", command="show", inputs={"script": "slow"})
    while True:
        f = c.recv()
        if f["type"] == "ui" and "slow started" in str(f["params"]):
            return


def test_capacity_error_when_max_runs_are_busy(built_store):
    # bug: runs beyond --max-runs are started anyway, or the slot is taken after the worker starts (race)
    with serverkit.running(built_store, max_runs=1) as (u, _srv):
        a, b = authed(u), authed(u)
        start_slow(a)
        b.send(type="run", id="b1", plugin="dev.dock.probe", command="exitcode", inputs={"code": 0})
        reply = b.recv()
        assert (reply["code"], reply["id"]) == ("capacity", "b1")
        a.close()


def test_disconnect_mid_run_cancels_it_and_frees_the_slot(built_store):
    # bug: closing the socket leaves the worker (and its container) running, or the slot is never released
    with serverkit.running(built_store, max_runs=1) as (u, _srv):
        a = authed(u)
        start_slow(a)
        a.close()
        end = time.monotonic() + 30
        while True:
            b = authed(u)
            b.send(type="run", id="b1", plugin="dev.dock.probe", command="exitcode", inputs={"code": 0})
            reply = b.recv()
            if reply["type"] == "done" or time.monotonic() > end:
                break
            b.close()
            time.sleep(0.3)
        assert reply == {"type": "done", "run": "b1", "code": 0}


def test_two_connections_run_at_once(built_store):
    # bug: connections share state, so one run's frames or done reach the other client
    with serverkit.running(built_store) as (u, _srv):
        a, b = authed(u), authed(u)
        for c, r in ((a, "ra"), (b, "rb")):
            c.send(type="run", id=r, plugin="dev.dock.uiprobe", command="show", inputs={"script": "basic"})
        for c, r in ((a, "ra"), (b, "rb")):
            frames = c.until_done(r)
            assert {f["run"] for f in frames} == {r} and frames[-1]["code"] == 0


def test_list_during_a_run_is_answered_between_frames(built_store):
    # bug: concurrent sends from the worker and the reader corrupt the stream or block the list
    with serverkit.running(built_store) as (u, _srv):
        c = authed(u)
        c.send(type="run", id="r1", plugin="dev.dock.uiprobe", command="show", inputs={"script": "basic"})
        c.send(type="list", id="L")
        frames = c.until_done("r1")
        assert [f["id"] for f in frames if f["type"] == "plugins"] == ["L"] and frames[-1]["code"] == 0


def start_ask(c, command, run="r1"):
    """Start uiprobe `command` and return its ask frame."""
    c.send(type="run", id=run, plugin="dev.dock.uiprobe", command=command, inputs={})
    while True:
        f = c.recv()
        if f["type"] == "ask":
            return f


def test_prompt_user_round_trip_reaches_the_plugin(built_store):
    # bug: the ask is never sent, or the answer does not reach the plugin, so the run hangs or sees eof
    with serverkit.running(built_store) as (u, _srv):
        c = authed(u)
        ask = start_ask(c, "prompt")
        assert (ask["run"], ask["method"], ask["params"]) == ("r1", "prompt_user", {"prompt": "Name?"})
        c.send(type="answer", ask=ask["ask"], result={"text": "Ada"})
        frames = c.until_done("r1")
        assert '"text": "Ada"' in frames[0]["params"]["text"] and frames[-1]["code"] == 0


def test_form_round_trip_reaches_the_plugin(built_store):
    # bug: form asks are answered with eof, or the field values are lost
    with serverkit.running(built_store) as (u, _srv):
        c = authed(u)
        ask = start_ask(c, "ask")
        assert ask["method"] == "form" and ask["params"]["title"] == "Settings"
        c.send(type="answer", ask=ask["ask"], result={"values": {"name": "Bob"}})
        frames = c.until_done("r1")
        assert '"Bob"' in frames[0]["params"]["text"] and frames[-1]["code"] == 0


def test_answer_for_unknown_ask_is_ignored_and_bad_result_is_bad_message(built_store, caplog):
    # bug: a stale, duplicate or bool-id answer raises an error frame, kills the connection or reaches an ask; a non-object result is accepted
    with serverkit.running(built_store) as (u, _srv):
        c = authed(u)
        ask = start_ask(c, "prompt")
        with caplog.at_level(logging.WARNING, logger="dock.server"):
            for bogus in ("99", 99, True):
                c.send(type="answer", ask=bogus, result={"text": "x"})
            c.send(type="answer", ask=ask["ask"], result={"text": "Ada"})
            c.send(type="answer", ask=ask["ask"], result={"text": "again"})
            c.send(type="answer", ask=99, result="nope")
            frames = c.until_done("r1")
        assert [f["code"] for f in frames if f["type"] == "error"] == ["bad_message"]
        shown = next(f for f in frames if f["type"] == "ui")
        assert '"text": "Ada"' in shown["params"]["text"] and frames[-1]["code"] == 0
        assert caplog.text.count("ignored an answer") == 4
        c.send(type="list", id="L")
        assert c.recv()["type"] == "plugins"


def test_cancel_a_running_command_ends_with_done_130(built_store):
    # bug: cancel is ignored, or a cancelled run reports code 0
    with serverkit.running(built_store) as (u, _srv):
        c = authed(u)
        start_slow(c)
        c.send(type="cancel", run="r1")
        assert c.until_done("r1")[-1] == {"type": "done", "run": "r1", "code": 130}


def test_cancel_releases_a_run_blocked_on_an_ask(built_store):
    # bug: a run waiting for an answer stays blocked after cancel, holding its slot and container
    with serverkit.running(built_store) as (u, _srv):
        c = authed(u)
        start_ask(c, "prompt")
        c.send(type="cancel", run="r1")
        assert c.until_done("r1")[-1] == {"type": "done", "run": "r1", "code": 130}


def test_cancel_when_idle_or_for_another_run_is_a_no_op(built_store):
    # bug: a stray cancel errors, or cancels the active run of a different id
    with serverkit.running(built_store) as (u, _srv):
        c = authed(u)
        c.send(type="cancel", run="nothing")
        c.send(type="run", id="r1", plugin="dev.dock.uiprobe", command="show", inputs={"script": "basic"})
        c.send(type="cancel", run="other")
        frames = c.until_done("r1")
        assert [f["type"] for f in frames if f["type"] == "error"] == [] and frames[-1]["code"] == 0


def test_disconnect_while_blocked_on_an_ask_ends_the_worker_itself(built_store, monkeypatch):
    # bug: the ask is not released on disconnect, so the worker only ends once the handler's close() kills the container
    conns = []

    class Recording(server.Connection):
        def __init__(self, *a):
            super().__init__(*a)
            conns.append(self)

    monkeypatch.setattr(server, "Connection", Recording)
    with serverkit.running(built_store) as (u, srv):
        c = authed(u)
        start_ask(c, "ask")
        c.close()
        threads = list(srv.handler_threads)
        for t in threads:
            t.join(15)
        assert threads and not any(t.is_alive() for t in threads)
        assert conns[0].worker is not None
        conns[0].worker.join(5)
        assert not conns[0].worker.is_alive()


def test_session_close_runs_once_even_when_worker_and_handler_both_try():
    # bug: after the join timeout the handler closes the session while the worker closes it too
    class Fake:
        calls = 0

        def close(self):
            Fake.calls += 1
            time.sleep(0.2)

    conn_ = conn.Connection(None, None, None, threading.BoundedSemaphore(1))
    fake = Fake()
    ts = [threading.Thread(target=conn_._close_session, args=(fake,)) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert Fake.calls == 1


def test_ask_released_before_the_send_is_not_sent_and_answers_eof():
    # bug: a cancel between registering and sending still delivers the ask frame to the client
    asks = Asks()
    sent = []

    def frame(ask_id):
        asks.release_all()
        return {"ask": ask_id}

    assert asks.ask(sent.append, frame) == {"eof": True}
    assert sent == []
