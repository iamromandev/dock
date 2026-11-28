"""Wire format v1: one JSON object per WebSocket text message, with a "type". See docs/SERVER_PROTOCOL.md."""

from __future__ import annotations

import json

VERSIONS = [1]
SERVER_VERSION = "0.1.0"

CLIENT_TYPES = {"hello", "list", "run", "answer", "cancel"}
CODES = ("auth", "version", "unknown_plugin", "bad_inputs", "busy", "capacity", "bad_message", "internal")


class ProtocolError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def parse(text) -> dict:
    """A client message as a dict with a known "type"; ProtocolError(bad_message) otherwise."""
    try:
        msg = json.loads(text)
    except (ValueError, TypeError):
        raise ProtocolError("bad_message", "not valid JSON") from None
    if not isinstance(msg, dict):
        raise ProtocolError("bad_message", "a message must be a JSON object")
    kind = msg.get("type")
    if kind not in CLIENT_TYPES:
        raise ProtocolError("bad_message", f"unknown message type {kind!r}")
    return msg


def welcome() -> dict:
    return {"type": "welcome", "version": VERSIONS[-1], "server": SERVER_VERSION}


def plugins(id, items: list[dict]) -> dict:
    return {"type": "plugins", "id": id, "plugins": items}


def ui(run: str, method: str, params: dict) -> dict:
    return {"type": "ui", "run": run, "method": method, "params": params}


def ask(run: str, ask_id: str, method: str, params: dict) -> dict:
    return {"type": "ask", "run": run, "ask": ask_id, "method": method, "params": params}


def done(run: str, code: int, error: str | None = None) -> dict:
    msg = {"type": "done", "run": run, "code": code}
    if error is not None:
        msg["error"] = error
    return msg


def error(code: str, message: str, id=None) -> dict:
    msg = {"type": "error", "code": code, "message": message}
    if id is not None:
        msg = {"type": "error", "id": id, "code": code, "message": message}
    return msg
