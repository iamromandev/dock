"""`dock serve`: dock as a host for another program, JSON-RPC 2.0 on stdin/stdout (protocol/SERVE.md).

The main thread reads the client's requests. Every `run` gets its own worker thread. Nothing but protocol lines
may reach stdout, so the real stdout is only ever written by the Writer, and `sys.stdout` is pointed at stderr
while serving (plugin cleanup prints through TerminalUI).
"""

from __future__ import annotations

import contextlib
import itertools
import json
import os
import signal
import sys
import threading
import time
import traceback
from collections.abc import Callable
from typing import BinaryIO

from cli import SERVE_VERSION, __version__, catalog, isolation, lifecycle
from cli.inputs import UsageError, parse_for
from cli.lifecycle import Session
from cli.manifest import ManifestError
from cli.process import PluginError
from cli.store import Store

MAX_LINE = 1024 * 1024  # a longer request line is refused (the rest of it is read and dropped)

# JSON-RPC error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class Writer:
    """The only writer of stdout: one whole line per message, under a lock, flushed at once."""

    def __init__(self, stream: BinaryIO):
        self._stream = stream
        self._lock = threading.Lock()
        self.broken = False  # the client closed its end: nothing more can be said

    def send(self, msg: dict) -> None:
        line = (json.dumps({"jsonrpc": "2.0", **msg}) + "\n").encode("utf-8")
        with self._lock:
            if self.broken:
                return
            try:
                self._stream.write(line)
                self._stream.flush()
            except (OSError, ValueError):
                self.broken = True

    def notify(self, method: str, params: dict) -> None:
        self.send({"method": method, "params": params})

    def reply(self, msg_id, result: dict) -> None:
        self.send({"id": msg_id, "result": result})

    def error(self, msg_id, code: int, message: str) -> None:
        self.send({"id": msg_id, "error": {"code": code, "message": message}})


class Slot:
    """Which call a pooled Session is serving right now: the one whose ClientUI its plugins talk to."""

    def __init__(self):
        self.ui: ClientUI | None = None


class RoutingUI:
    """The UI a pooled Session's plugin processes are started with.

    Their handlers are bound once, when the process starts, but the process outlives a call. So every message
    goes to whichever call holds the Session at that moment (its Slot). A message that arrives while no call does
    (a late `log`, say, seen while the Session is being closed) goes to stderr: it is not for any client call and
    stdout is the protocol.
    """

    def __init__(self, plugin_id: str, slot: Slot):
        self.plugin_id = plugin_id
        self.slot = slot

    def output(self, p: dict) -> None:
        ui = self.slot.ui
        if ui is not None:
            ui.output(p)
        else:
            self._stray(f"output: {p.get('text', '')!r}")

    def log(self, p: dict) -> None:
        ui = self.slot.ui
        if ui is not None:
            ui.log(p)
        else:
            self._stray(f"{p.get('level', 'info')}: {p.get('message', '')}")

    def progress(self, p: dict) -> None:
        ui = self.slot.ui
        if ui is not None:
            ui.progress(p)

    def prompt_user(self, p: dict) -> dict:
        ui = self.slot.ui
        return ui.prompt_user(p) if ui is not None else {"eof": True}  # nobody to ask

    def warn(self, message: str) -> None:
        ui = self.slot.ui
        if ui is not None:
            ui.warn(message)
        else:
            self._stray(message)

    def _stray(self, text: str) -> None:
        try:
            print(f"dock: [{self.plugin_id}] (no call) {text}", file=sys.stderr)
        except (OSError, ValueError):
            pass

    def handlers(self) -> dict:
        return {"output": self.output, "log": self.log, "progress": self.progress, "prompt_user": self.prompt_user}


class ClientUI:
    """One call's UI: what its plugins say becomes notifications to the client, tagged with the call id."""

    PROMPT_POLL = 0.05  # seconds between looks at the call's cancel event while a prompt waits for its answer

    def __init__(self, call_id, writer: Writer, server: Server, cancel: threading.Event, verbose: bool = False):
        self.call_id = call_id
        self.writer = writer
        self.server = server
        self.cancel = cancel
        self.verbose = verbose

    def output(self, p: dict) -> None:
        text = p.get("text", "")
        self.writer.notify("output", {"call": self.call_id, "text": text if isinstance(text, str) else str(text)})

    def log(self, p: dict) -> None:
        level = p.get("level", "info")
        if level == "debug" and not self.verbose:
            return
        self.writer.notify("log", {"call": self.call_id, "level": level, "message": str(p.get("message", ""))})

    def progress(self, p: dict) -> None:
        params = {"call": self.call_id, "percent": p.get("percent"), "message": p.get("message", "")}
        self.writer.notify("progress", params)

    def prompt_user(self, p: dict) -> dict:
        """Ask the client (never dock's own stdin) and wait for its answer, or for the call to be cancelled."""
        prompt = p.get("prompt", "> ")
        slot = self.server.open_prompt()
        try:
            self.writer.send({"id": slot.id, "method": "prompt",
                              "params": {"call": self.call_id, "prompt": prompt if isinstance(prompt, str) else str(prompt)}})
            while not slot.answered.wait(self.PROMPT_POLL):
                if self.cancel.is_set():
                    return {"eof": True}  # withdrawn: the plugin is not left waiting for an answer that will not come
            return slot.answer
        finally:
            self.server.close_prompt(slot)

    def warn(self, message: str) -> None:
        self.writer.notify("log", {"call": self.call_id, "level": "warn", "message": message})

    def handlers(self) -> dict:
        return {"output": self.output, "log": self.log, "progress": self.progress, "prompt_user": self.prompt_user}


class Entry:
    """A pooled Session with the Slot its plugins' UI is routed through."""

    def __init__(self, session: Session, slot: Slot, key: tuple[str, str]):
        self.session = session
        self.slot = slot
        # (plugin id, cwd): the cwd mount is fixed when a container starts
        self.key = key
        self.last_used = 0.0


class Pool:
    """Warm Sessions, keyed by (plugin id, cwd), for calls to lease.

    A leased Session serves one call at a time and is pumped only by the thread of that call; a Session in the
    pool is touched only here, under the lock. `session.close()` runs outside the lock, because it waits for
    processes to stop. A Session that is being closed, by whichever thread, is on `_closers` from the moment it
    leaves the pool's books until it is closed, so that `close()` can wait for it.
    """

    CLOSE_WAIT = 20  # seconds close() waits for closes that other threads are in

    def __init__(self, store: Store, verbose: bool, idle_timeout: float = 300, max_idle: int = 8,
                 reap_interval: float = 5.0, clock=time.monotonic):
        self.store = store
        self.verbose = verbose
        self.idle_timeout = idle_timeout  # seconds; 0 = never reap
        self.max_idle = max_idle
        self._clock = clock
        self._lock = threading.Lock()
        self._changed = threading.Condition(self._lock)  # a Session left `_closers`
        self._idle: list[Entry] = []  # oldest first
        self._leased: dict[Session, Entry] = {}
        self._closers: list[Session] = []  # being closed right now, by some thread
        self._to_close: list[Session] = []  # taken over by close(), not closed yet
        self._to_kill: list[Session] = []  # leased when close() came: killed, not asked
        self._closing = False
        self._stop = threading.Event()
        self._reaper: threading.Thread | None = None
        if idle_timeout > 0:
            self._reaper = threading.Thread(target=self._reap_loop, args=(reap_interval,), name="reaper", daemon=True)
            self._reaper.start()

    def _expired(self, entry: Entry, now: float) -> bool:
        return self.idle_timeout > 0 and now - entry.last_used > self.idle_timeout

    @staticmethod
    def _healthy(entry: Entry) -> bool:
        proc = entry.session.commands.get(entry.key[0])
        return proc is not None and proc.alive()

    def lease(self, plugin_id: str, cwd: str, ui: ClientUI) -> Session:
        """A warm Session for this plugin and cwd, or a new one; `ui` becomes where its plugins'
        messages go."""
        key = (plugin_id, cwd)
        stale: list[Entry] = []
        with self._lock:
            if self._closing:
                raise PluginError("dock serve is shutting down")
            now = self._clock()
            entry = None
            for candidate in reversed(self._idle):  # the most recently used first
                if candidate.key != key:
                    continue
                if self._expired(candidate, now) or not self._healthy(candidate):
                    stale.append(candidate)
                    continue
                entry = candidate
                break
            for gone in stale + ([entry] if entry else []):
                self._idle.remove(gone)
            if entry is None:
                slot = Slot()
                session = Session(self.store, self.verbose, ui_factory=lambda pid: RoutingUI(pid, slot), cwd=cwd)
                entry = Entry(session, slot, key)
            entry.slot.ui = ui
            self._leased[entry.session] = entry
            self._closers.extend(old.session for old in stale)
        for old in stale:
            self._close(old.session)
        return entry.session

    def release(self, session: Session, reusable: bool, before_close: Callable[[], None] | None = None) -> None:
        """A call is done with its Session: back to the pool if it is still good, else closed.

        `before_close` runs after the Session has left (or gone back into) the pool and before anything is closed,
        and only if something is to be: closing takes long, and the caller's reply must not wait for it.
        """
        closing: list[Session] = []
        with self._lock:
            entry = self._leased.pop(session, None)
            if entry is not None:
                entry.slot.ui = None
            if reusable and entry is not None and not self._closing:
                entry.last_used = self._clock()
                self._idle.append(entry)
                while len(self._idle) > self.max_idle:  # too many warm ones: the oldest goes
                    closing.append(self._idle.pop(0).session)
            else:
                closing.append(session)
            self._closers.extend(closing)
        if closing and before_close is not None:
            try:
                before_close()
            finally:
                for victim in closing:
                    self._close(victim)
            return
        for victim in closing:
            self._close(victim)

    def reap(self) -> None:
        """Close the Sessions that have been idle longer than the idle timeout."""
        with self._lock:
            now = self._clock()
            expired = [e for e in self._idle if self._expired(e, now)]
            for entry in expired:
                self._idle.remove(entry)
            self._closers.extend(e.session for e in expired)
        for entry in expired:
            self._close(entry.session)

    def _reap_loop(self, interval: float) -> None:
        while not self._stop.wait(interval):
            try:
                self.reap()
            except Exception:  # noqa: BLE001 - the reaper must keep running
                traceback.print_exc()

    def close(self) -> None:
        """Stop every Session, newest first, and wait for the ones other threads are closing.

        A call still holding a Session by now is stuck: its plugins are killed. Can be called again after an
        interrupt (SIGTERM): it carries on with what is left.
        """
        with self._lock:
            if not self._closing:
                self._closing = True
                self._to_close = [e.session for e in self._idle]  # closed from the end: newest first
                self._to_kill = [e.session for e in self._leased.values()]
                self._idle = []
                self._leased.clear()
                self._closers.extend(self._to_close)
        self._stop.set()
        while True:
            with self._lock:
                session = self._to_close.pop() if self._to_close else None
            if session is None:
                break
            self._close(session)
        while True:
            with self._lock:
                session = self._to_kill.pop() if self._to_kill else None
            if session is None:
                break
            self._kill(session)
        self._wait_for_closers()

    def _wait_for_closers(self) -> None:
        deadline = time.monotonic() + self.CLOSE_WAIT
        with self._changed:
            while self._closers and time.monotonic() < deadline:
                self._changed.wait(0.1)
            stuck = list(self._closers)
        for session in stuck:  # a close that does not end: its plugins are killed, which ends it
            self._kill(session)

    @staticmethod
    def _kill(session: Session) -> None:
        for proc in list(session.procs):
            try:
                session.stop_proc(proc, force=True)
            except Exception:  # noqa: BLE001
                traceback.print_exc()

    def _close(self, session: Session) -> None:
        """Close one Session that is on `_closers`, and take it off again, however that goes."""
        try:
            session.close()
        except Exception:  # noqa: BLE001 - one Session failing to stop must not leave the others running
            traceback.print_exc()
        finally:
            with self._changed:
                if session in self._closers:
                    self._closers.remove(session)
                self._changed.notify_all()


class PromptSlot:
    """A `prompt` request waiting for the client's reply."""

    def __init__(self, prompt_id: str):
        self.id = prompt_id
        self.answered = threading.Event()
        self.answer: dict = {"eof": True}


class Call:
    """A `run` that is in progress: its thread and the event that cancels it."""

    def __init__(self, call_id):
        self.id = call_id
        self.cancel = threading.Event()
        self.thread: threading.Thread | None = None
        self.answered = False  # its reply is on its way: the id may be used again


class Server:
    def __init__(self, store: Store, verbose: bool, writer: Writer, pool: Pool):
        self.store = store
        self.verbose = verbose
        self.writer = writer
        self.pool = pool
        self.greeted = False
        self.calls: dict = {}  # run id -> Call, for the calls that are running
        self.pending_prompts: dict[str, PromptSlot] = {}  # "p<n>" -> the call thread waiting for the answer
        self._prompt_ids = itertools.count(1)
        self._lock = threading.Lock()
        self._closing = False

    # requests ----------------------------------------------------------------
    def handle(self, msg) -> bool:
        """Act on one parsed message from the client. False means the client asked to shut down.

        A bug while serving one message must not end the server (and strand the plugins it started): it is logged,
        and a request is answered with an internal error.
        """
        try:
            return self._handle(msg)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            if isinstance(msg, dict) and isinstance(msg.get("method"), str) and "id" in msg:
                self.writer.error(msg["id"], INTERNAL_ERROR, f"internal error: {e}")
            return True

    def _handle(self, msg) -> bool:
        if not isinstance(msg, dict):
            self.writer.error(None, INVALID_REQUEST, "a request must be a JSON object")
            return True
        method, msg_id = msg.get("method"), msg.get("id")
        if not isinstance(method, str):
            self._answer_prompt(msg)  # a reply to one of our requests, or noise
            return True
        params = msg.get("params")
        params = params if isinstance(params, dict) else {}
        is_request = "id" in msg
        if method != "hello" and not self.greeted:
            if is_request:
                self.writer.error(msg_id, INVALID_REQUEST, "hello first")
            return True
        if method == "hello":
            return self._hello(msg_id, is_request)
        if method == "commands":
            self._commands(msg_id, is_request)
        elif method == "search":
            self._search(msg_id, params, is_request)
        elif method == "run":
            self._run(msg_id, params, is_request)
        elif method == "cancel":
            self.cancel(params.get("id"))
        elif method == "shutdown":
            return False
        elif is_request:
            self.writer.error(msg_id, METHOD_NOT_FOUND, f"unknown method '{method}'")
        return True

    def _hello(self, msg_id, is_request: bool) -> bool:
        self.greeted = True
        if is_request:
            self.writer.reply(msg_id, {"serve_version": SERVE_VERSION, "dock_version": __version__})
        return True

    def _commands(self, msg_id, is_request: bool) -> None:
        if not is_request:
            return
        commands = []
        for plugin_id in sorted(self.store.index()):
            try:
                manifest = self.store.get(plugin_id)
            except Exception as e:  # noqa: BLE001 - one broken plugin must not hide the others
                print(f"dock: {plugin_id}: {e}", file=sys.stderr)
                continue
            for command in manifest.commands:
                commands.append({
                    "name": command.name, "plugin": manifest.id, "summary": command.summary,
                    "inputs": [_input_json(spec) for spec in command.inputs or []],
                })
        self.writer.reply(msg_id, {"commands": commands})

    def _search(self, msg_id, params: dict, is_request: bool) -> None:
        if not is_request:
            return
        words = params.get("words")
        if not isinstance(words, list) or not words \
                or not all(isinstance(w, str) and w.strip() for w in words):
            self.writer.error(msg_id, INVALID_PARAMS, "words must be a non-empty list of non-empty strings")
            return
        try:
            found = catalog.search(self.store, words)
        except ManifestError as e:  # e.g. a bad [catalog] in config.toml
            self.writer.error(msg_id, -32000, str(e))
            return
        matches = []
        for m in found:
            try:
                manifest = self.store.get(m.plugin) if m.installed else m.manifest
                command = next(c for c in manifest.commands if c.name == m.name)
                inputs = [_input_json(spec) for spec in command.inputs or []]
            except Exception as e:  # noqa: BLE001 - one broken plugin must not hide the others
                print(f"dock: {m.plugin}: {e}", file=sys.stderr)
                continue
            item = {"name": m.name, "plugin": m.plugin, "version": m.version, "summary": m.summary,
                    "keywords": list(m.keywords), "inputs": inputs, "installed": m.installed}
            if not m.installed and m.folder is not None:
                item["path"] = str(m.folder)
            if m.installed and m.catalog_version:
                item["catalog_version"] = m.catalog_version
            if m.installed_version:
                item["installed_version"] = m.installed_version
            matches.append(item)
        self.writer.reply(msg_id, {"matches": matches})

    # run ---------------------------------------------------------------------------
    def _run(self, msg_id, params: dict, is_request: bool) -> None:
        if not is_request:
            return  # a run needs an id to be answered under
        if isinstance(msg_id, bool) or not isinstance(msg_id, (int, str)):
            self.writer.error(None, INVALID_REQUEST, "a request id must be an integer or a string")
            return
        running = self.calls.get(msg_id)
        if running is not None and not running.answered:
            self.writer.error(msg_id, INVALID_REQUEST, f"a call with id {msg_id!r} is already running")
            return
        command, args, cwd = params.get("command"), params.get("args", []), params.get("cwd")
        if "inputs" in params:
            self.writer.error(msg_id, INVALID_PARAMS, "inputs are not supported in serve version 1; send args")
            return
        if not isinstance(command, str) or not command:
            self.writer.error(msg_id, INVALID_PARAMS, "command must be a non-empty string")
            return
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            self.writer.error(msg_id, INVALID_PARAMS, "args must be a list of strings")
            return
        if not isinstance(cwd, str) or not os.path.isabs(cwd) or not os.path.isdir(cwd):
            self.writer.error(msg_id, INVALID_PARAMS, "cwd must be the absolute path of a folder that exists")
            return
        try:
            manifest, name = self.store.resolve(command)
        except ManifestError as e:
            self.writer.error(msg_id, METHOD_NOT_FOUND, str(e))
            return
        try:
            parse_for(manifest, name, args)  # a bad invocation starts no process, as on the CLI
        except UsageError as e:
            self.writer.error(msg_id, INVALID_PARAMS, str(e))
            return
        call = Call(msg_id)
        with self._lock:
            if self._closing:
                self.writer.error(msg_id, -32000, "dock serve is shutting down")
                return
            self.calls[msg_id] = call
        call.thread = threading.Thread(target=self.run_call, args=(call, manifest, name, args, cwd),
                                       name=f"call-{msg_id}", daemon=True)
        call.thread.start()

    def run_call(self, call: Call, manifest, command: str, args: list[str], cwd: str) -> None:
        """One call, on its own thread: lease a Session, run, give the Session back, and answer."""
        session = None
        reusable = False
        reply: dict
        try:
            ui = ClientUI(call.id, self.writer, self, call.cancel, self.verbose)
            session = self.pool.lease(manifest.id, cwd, ui)
            result = session.invoke_result(manifest, command, args, cancel=call.cancel)
            proc = session.commands.get(manifest.id)
            reusable = proc is not None and proc.alive()
            reply = {"result": result}
        except PluginError as e:
            reply = {"error": {"code": e.code, "message": str(e)}}
        except UsageError as e:
            reply = {"error": {"code": INVALID_PARAMS, "message": str(e)}}
        except ManifestError as e:
            reply = {"error": {"code": -32000, "message": str(e)}}
        except Exception as e:  # noqa: BLE001 - a bug in one call must not take the server down
            traceback.print_exc()
            reply = {"error": {"code": INTERNAL_ERROR, "message": f"internal error: {e}"}}
        answered = False

        def answer() -> None:
            nonlocal answered
            if answered:
                return
            answered = True
            call.answered = True  # before the reply: the client may reuse the id the moment it has read it
            self.writer.send({"id": call.id, **reply})
            with self._lock:
                if self.calls.get(call.id) is call:
                    del self.calls[call.id]

        try:
            if session is not None:
                # A reusable Session is back in the pool before the reply (the client may send its next call at
                # once and should get the warm process); one that is to be closed is closed after it: stopping
                # plugins takes long, and the client is waiting.
                self.pool.release(session, reusable, before_close=answer)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        finally:
            answer()

    # prompts -------------------------------------------------------------------------
    def open_prompt(self) -> PromptSlot:
        with self._lock:
            slot = PromptSlot(f"p{next(self._prompt_ids)}")
            self.pending_prompts[slot.id] = slot
        return slot

    def close_prompt(self, slot: PromptSlot) -> None:
        with self._lock:
            self.pending_prompts.pop(slot.id, None)

    def _answer_prompt(self, msg: dict) -> None:
        """The client's reply to a `prompt` request: `{text}`, or `{eof: true}`; an error or anything else is eof."""
        with self._lock:
            slot = self.pending_prompts.get(msg.get("id")) if isinstance(msg.get("id"), str) else None
        if slot is None:
            return  # unknown, or withdrawn already
        result = msg.get("result")
        if isinstance(result, dict) and isinstance(result.get("text"), str) and not result.get("eof"):
            slot.answer = {"text": result["text"]}
        else:
            slot.answer = {"eof": True}
        slot.answered.set()

    def cancel(self, call_id) -> None:
        with self._lock:
            call = self.calls.get(call_id) if isinstance(call_id, (int, str)) and not isinstance(call_id, bool) \
                else None
        if call is not None:
            call.cancel.set()

    def close(self) -> None:
        """Cancel every call, wait for them to end (the cancel grace period, plus a little), stop everything."""
        with self._lock:
            self._closing = True
            calls = list(self.calls.values())
        for call in calls:
            call.cancel.set()
        deadline = time.monotonic() + lifecycle.CANCEL_GRACE + 2
        for call in calls:
            if call.thread is not None:
                call.thread.join(max(0.0, deadline - time.monotonic()))
        self.pool.close()


def _input_json(spec) -> dict:
    out = {"name": spec.name, "type": spec.type, "required": spec.required}
    if spec.flag is not None:
        out["flag"] = spec.flag
    if spec.short is not None:
        out["short"] = spec.short
    return out


def read_line(stdin: BinaryIO) -> bytes | None:
    """The next request line (without its newline), b"" for a line that was too long, None at end of input."""
    line = stdin.readline(MAX_LINE + 1)
    if not line:
        return None
    if len(line) > MAX_LINE and not line.endswith(b"\n"):
        while line and not line.endswith(b"\n"):  # drop the rest of it
            line = stdin.readline(MAX_LINE)
        return b""
    return line.rstrip(b"\r\n")


def serve(store: Store, verbose: bool = False, stdin: BinaryIO | None = None, stdout: BinaryIO | None = None) -> int:
    stdin = stdin if stdin is not None else sys.stdin.buffer
    stdout = stdout if stdout is not None else sys.stdout.buffer
    writer = Writer(stdout)
    try:
        idle_timeout = store.serve_idle_timeout()
    except ManifestError as e:  # config.toml does not parse
        print(f"dock: {e}", file=sys.stderr)
        return 1
    server = Server(store, verbose, writer, Pool(store, verbose, idle_timeout))
    shutdown: dict | None = None  # the client's shutdown message: a request is answered once all is stopped
    with contextlib.redirect_stdout(sys.stderr):  # a stray print must not corrupt the protocol
        try:
            isolation.sweep(store)
            while True:
                line = read_line(stdin)
                if line is None:
                    break
                if line == b"":
                    writer.error(None, PARSE_ERROR, f"line too long (over {MAX_LINE} bytes)")
                    continue
                if not line.strip():
                    continue
                try:
                    msg = json.loads(line)
                except (ValueError, RecursionError):  # not JSON, or nested too deep to parse
                    writer.error(None, PARSE_ERROR, "parse error: not JSON")
                    continue
                if not server.handle(msg):
                    shutdown = msg
                    break
        except KeyboardInterrupt:  # SIGTERM: leave like end of input
            pass
        finally:  # whatever ended the loop, nothing a call started may be left running
            _close_shielded(server)
        if shutdown is not None and "id" in shutdown:
            writer.reply(shutdown["id"], {})
    return 0


def _close_shielded(server: Server) -> None:
    """Close the server with SIGTERM and SIGINT ignored: a signal now must not leave plugins running.

    A signal that came in just before this point is the one chance left of an interrupt, so it is retried.
    """
    saved = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            saved[sig] = signal.signal(sig, lambda *_: None)
        except (ValueError, OSError):
            pass  # not the main thread (a test); nothing to shield from
    try:
        while True:
            try:
                server.close()
                return
            except KeyboardInterrupt:
                continue  # Server.close and Pool.close carry on with what is left
    finally:
        for sig, handler in saved.items():
            if handler is not None:
                signal.signal(sig, handler)
