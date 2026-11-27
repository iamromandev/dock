"""AC1: every message has a schema, a valid fixture, and the invalid fixtures are rejected. No container runtime."""

import json
from pathlib import Path

import pytest

from conformance.schema import MESSAGES, validate

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def _cases(kind: str) -> list[tuple[str, Path]]:
    return [(d.name, f) for d in sorted((FIXTURES / kind).iterdir()) for f in sorted(d.glob("*.json"))]


def test_every_message_has_a_valid_fixture():
    # Bug caught: a schema added without a fixture, so nothing proves it accepts a real message.
    have = {d.name for d in (FIXTURES / "valid").iterdir() if any(d.glob("*.json"))}
    assert have == set(MESSAGES)


@pytest.mark.parametrize(("name", "path"), _cases("valid"), ids=lambda v: v.name if isinstance(v, Path) else v)
def test_valid_fixture_passes(name, path):
    # Bug caught: a schema too strict for a message the protocol allows.
    assert validate(json.loads(path.read_text()), expect=name) == []


@pytest.mark.parametrize(("name", "path"), _cases("invalid"), ids=lambda v: v.name if isinstance(v, Path) else v)
def test_invalid_fixture_fails_with_a_path(name, path):
    # Bug caught: a schema too loose (missing required field, limit not enforced); errors must name a json path.
    errors = validate(json.loads(path.read_text()), expect=name)
    assert errors
    assert all(e.startswith("$") and ": " in e for e in errors)


def test_validate_infers_notification_from_method():
    # Bug caught: dispatch by method broken, so an incoming bar is checked against nothing.
    bad = {"jsonrpc": "2.0", "method": "bar", "params": {"percent": 5}}
    assert any("id" in e for e in validate(bad))


@pytest.mark.parametrize("method", ["initialize", "invoke", "shutdown", "cancel", "bogus"])
def test_host_only_method_from_plugin_is_rejected(method):
    # Bug caught: a plugin sending a host-to-plugin method passes because every schema is looked up by method.
    errors = validate({"jsonrpc": "2.0", "method": method, "params": {}})
    assert any("unexpected method from plugin" in e for e in errors)
