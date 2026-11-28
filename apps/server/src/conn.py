"""One client connection: the handshake, then list and run."""

from __future__ import annotations

import hmac
import itertools
import json
import logging
import re
import threading

from dock_host.inputs import UsageError, check_values
from dock_host.lifecycle import Session
from dock_host.manifest import ManifestError
from dock_host.process import PluginError
from websockets.exceptions import ConnectionClosed

from src import protocol
from src.asks import Asks
from src.protocol import ProtocolError
from src.remote_ui import RemoteUI

log = logging.getLogger("dock.server")

HANDSHAKE_TIMEOUT = 10  # seconds a new connection has to send hello
JOIN_TIMEOUT = 10  # seconds a disconnect waits for the run to stop
_LOCAL_ORIGIN = re.compile(r"https?://(127\.0\.0\.1|localhost)(:\d+)?")


def origin_allowed(origin: str | None) -> bool:
    """No Origin header (not a browser), "null", or a page served from this machine; other web pages are refused."""
    return origin is None or origin == "null" or _LOCAL_ORIGIN.fullmatch(origin) is not None


def scrub(text: str, paths) -> str:
    """`text` without absolute host paths (longest first): clients get no view of the host's folders."""
    for p in sorted({str(p) for p in paths if len(str(p)) > 1}, key=len, reverse=True):
        text = text.replace(p, "<dock>")
    return text


class Connection:
    def __init__(self, ws, cfg, store, slots: threading.BoundedSemaphore):
        self.ws = ws
        self.cfg = cfg
        self.store = store
        self.slots = slots
        self.run_id: str | None = None  # the active run, None when idle
        self.worker: threading.Thread | None = None
        self.session: Session | None = None
        self.asks: Asks | None = None  # the active run's open questions
        self._close_lock = threading.Lock()
        self._closing: set[Session] = set()  # sessions whose close() has started
        self._ask_ids = itertools.count(1)  # unique per connection, across runs

    def _hide(self, text: str) -> str:
        d = self.cfg.dirs
        return scrub(text, [self.store.home, d.in_dir, d.out_dir])

    def send(self, msg: dict) -> None:
        try:
            self.ws.send(json.dumps(msg))  # sync send is lock-protected: workers and the reader may both send
        except ConnectionClosed:
            pass

    def handle(self) -> None:
        try:
            if not self._handshake():
                return
            for text in self.ws:
                try:
                    msg = protocol.parse(text)
                    self._dispatch(msg)
                except ProtocolError as e:
                    self.send(protocol.error(e.code, e.message))
                except ConnectionClosed:
                    raise
                except Exception:
                    log.exception("handling a client message failed")
                    self.send(protocol.error("internal", "the server failed to handle this message"))
        except ConnectionClosed:
            pass
        finally:
            self._finish()

    def _finish(self) -> None:
        """The client is gone: cancel the active run, free what it waits on, and stop it, so nothing outlives it."""
        worker, session, asks = self.worker, self.session, self.asks
        if worker is None or session is None:
            return
        if worker.is_alive():
            session.cancel()
        if asks is not None:
            asks.release_all()
        worker.join(JOIN_TIMEOUT)
        if worker.is_alive():
            log.warning("run %s did not stop within %ss of the disconnect; closing its session", self.run_id, JOIN_TIMEOUT)
        self._close_session(session)  # kills the containers of a worker that did not stop in time

    def _close_session(self, session: Session) -> None:
        """Close the session exactly once: the worker (normal end) and the handler (after the join timeout) both
        call this, and a second close() racing a stuck first one must not touch the same containers."""
        with self._close_lock:
            if session in self._closing:
                return
            self._closing.add(session)
        try:
            session.close()
        except Exception:
            log.exception("closing the session of run %s", self.run_id)

    def _reject(self, code: str, message: str) -> bool:
        self.send(protocol.error(code, message))
        self.ws.close()
        return False

    def _handshake(self) -> bool:
        try:
            msg = protocol.parse(self.ws.recv(timeout=HANDSHAKE_TIMEOUT))
        except TimeoutError:
            return self._reject("auth", "no hello within the handshake time")
        except ProtocolError:
            msg = {}
        if msg.get("type") != "hello":
            return self._reject("auth", "the first message must be hello")
        token = msg.get("token")
        if not isinstance(token, str) or not hmac.compare_digest(token.encode(), self.cfg.token.encode()):
            log.info("connection rejected: bad token")
            return self._reject("auth", "wrong or missing token")
        version = msg.get("version")
        if type(version) is not int or version not in protocol.VERSIONS:
            return self._reject("version", f"unsupported version; supported: {protocol.VERSIONS}")
        self.send(protocol.welcome())
        return True

    def _dispatch(self, msg: dict) -> None:
        kind = msg["type"]
        if kind == "list":
            self._list(msg)
        elif kind == "run":
            self._run(msg)
        elif kind == "answer":
            self._answer(msg)
        elif kind == "cancel":
            self._cancel(msg)
        else:
            raise ProtocolError("bad_message", f"unexpected message type {kind!r}")

    def _answer(self, msg: dict) -> None:
        result = msg.get("result")
        if not isinstance(result, dict):
            raise ProtocolError("bad_message", "answer needs an object result")
        asks = self.asks
        if asks is None or not asks.answer(msg.get("ask"), result):
            log.warning("ignored an answer for unknown or finished ask %r", msg.get("ask"))

    def _cancel(self, msg: dict) -> None:
        run = msg.get("run")
        session, asks = self.session, self.asks
        if run is None or run != self.run_id or session is None:
            return  # idle, or another run: nothing to cancel
        session.cancel()
        if asks is not None:
            asks.release_all()

    def _list(self, msg: dict) -> None:
        items = [{"id": pid, "commands": self._describe(pid, e["commands"])} for pid, e in self.store.index().items()]
        self.send(protocol.plugins(msg.get("id"), items))

    def _describe(self, plugin: str, names: list[str]) -> list[dict]:
        """The wire form of a plugin's commands: name, summary and declared inputs (None when undeclared)."""
        out = []
        for name in names:
            manifest, _ = self.store.resolve(f"{plugin}:{name}")
            cmd = manifest.command(name)
            inputs = None if cmd.inputs is None else [
                {"name": i.name, "type": i.type, "required": i.required, "default": i.default} for i in cmd.inputs
            ]
            out.append({"name": name, "summary": cmd.summary, "inputs": inputs})
        return out

    def _run(self, msg: dict) -> None:
        req = msg.get("id")
        plugin, command, inputs = msg.get("plugin"), msg.get("command"), msg.get("inputs")
        if not isinstance(plugin, str) or not isinstance(command, str) or not isinstance(req, str):
            raise ProtocolError("bad_message", "run needs string id, plugin and command")
        if inputs is not None and not isinstance(inputs, dict):
            self.send(protocol.error("bad_inputs", "inputs must be an object", id=req))
            return
        if self.run_id is not None:
            self.send(protocol.error("busy", "this connection already has an active run", id=req))
            return
        try:
            manifest, name = self.store.resolve(f"{plugin}:{command}")
        except ManifestError as e:
            self.send(protocol.error("unknown_plugin", self._hide(str(e)), id=req))
            return
        cmd = manifest.command(name)
        if cmd is None:
            self.send(protocol.error("unknown_plugin", f"{plugin} has no command '{command}'", id=req))
            return
        try:
            checked = self._check_inputs(cmd, inputs)
        except UsageError as e:
            self.send(protocol.error("bad_inputs", str(e), id=req))
            return
        if not self.slots.acquire(blocking=False):
            self.send(protocol.error("capacity", "the server is already running its maximum number of runs", id=req))
            return
        try:
            # begin() on this thread, before the worker exists: a cancel from a disconnect can never be dropped as stale
            asks = Asks(self._ask_ids)
            session = Session(self.store, lambda _plugin: RemoteUI(self.send, req, asks))
            session.begin()
            self.session, self.asks = session, asks
            self.run_id = req
            self.worker = threading.Thread(target=self._work, args=(req, session, manifest, name, checked), daemon=True)
            self.worker.start()
        except BaseException:
            self.run_id = None
            self.slots.release()
            raise

    @staticmethod
    def _check_inputs(cmd, inputs: dict | None) -> dict | None:
        """The inputs to send: typed with defaults filled when declared; None when the command declares none."""
        if cmd.inputs is None:  # undeclared: the plugin takes what it is given
            return inputs
        checked = check_values(cmd.inputs, inputs or {})
        return checked if cmd.inputs else None

    def _work(self, run_id: str, session: Session, manifest, command: str, inputs: dict | None) -> None:
        """One run in its own Session. Always ends with exactly one `done`."""
        code, error = 0, None
        try:
            result = session.invoke_inputs(manifest, command, inputs)
            code = int(result.get("exit_code", 0))
        except PluginError as e:
            log.info("run %s failed: %s", run_id, e)
            code, error = 1, self._hide(str(e))
        except Exception as e:  # a host failure must still end the run for the client
            log.exception("run %s failed", run_id)
            code, error = 1, f"internal error: {type(e).__name__}"
        finally:
            self._close_session(session)
            self.run_id = None
            self.slots.release()
        self.send(protocol.done(run_id, code, error))
