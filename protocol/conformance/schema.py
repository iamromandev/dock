"""Validate protocol v2 messages against the JSON Schemas in protocol/schema/.

Messages from a plugin are checked more strictly than a host would: a host may ignore what it does not know,
but the checker accepts only the plugin-to-host methods (`PLUGIN_METHODS`) and fails anything else, such as a
plugin sending `initialize`. The host-to-plugin schemas stay usable through `expect=`.
"""

from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.protocols import Validator
from referencing import Registry, Resource

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schema"

_registry = Registry().with_resources(
    (s["$id"], Resource.from_contents(s)) for s in (json.loads(p.read_text()) for p in SCHEMA_DIR.glob("*.json"))
)

# message name -> schema file; common.json only holds shared definitions.
MESSAGES = sorted(p.stem for p in SCHEMA_DIR.glob("*.json") if p.stem != "common")
PLUGIN_METHODS = {"output", "log", "bar", "table", "panel", "clear", "prompt_user", "form"}
_validators: dict[str, Validator] = {}


def _validator(name: str) -> Validator:
    if name not in _validators:
        schema = json.loads((SCHEMA_DIR / f"{name}.json").read_text())
        _validators[name] = Draft202012Validator(schema, registry=_registry)
    return _validators[name]


def _path(error) -> str:
    out = "$"
    for part in error.absolute_path:
        out += f"[{part}]" if isinstance(part, int) else f".{part}"
    return out


def validate(msg, *, expect: str | None = None) -> list[str]:
    """Problems with one message, as "<json-path>: <reason>"; an empty list means valid.

    `expect` names the schema (for example "initialize_result" or "error"). Without it a message with a
    `method` must be a plugin-to-host method and is checked against that method's schema, and a response only against its envelope.
    """
    if expect is None:
        if not isinstance(msg, dict):
            return ["$: message is not a JSON object"]
        method = msg.get("method")
        if method is not None:
            if method not in PLUGIN_METHODS:
                return [f"$.method: unexpected method from plugin: {method!r}"]
            expect = method
        elif "error" in msg:
            expect = "error"
        else:
            expect = "response"
    errors = sorted(_validator(expect).iter_errors(msg), key=lambda e: list(map(str, e.absolute_path)))
    return [f"{_path(e)}: {e.message}" for e in errors]
