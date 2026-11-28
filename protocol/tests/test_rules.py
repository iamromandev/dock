"""Rules a JSON Schema cannot express, checked on messages that already passed their schema."""

from conformance.rules import Areas, check_form


def _form(*fields):
    return {"fields": list(fields)}


def _text(name):
    return {"name": name, "label": "L", "type": "text"}


def _note(kind, id_):
    return {"jsonrpc": "2.0", "method": kind, "params": {"id": id_}}


def _feed(areas, msgs):
    return [e for m in msgs if (e := areas.see(m))]


def test_form_and_area_rules_hold():
    # Bug caught (form): duplicate names pass (or "A" and "a" are wrongly treated as the same name), or a choice
    # default outside the choices is accepted.
    # Bug caught (areas): the 32-area limit is not enforced or is off by one, clear lowers the count so a plugin
    # cycling areas slips past the limit, ids are counted across kinds (16 bars plus 16 panels fail), or reset leaks
    # between invokes.
    assert check_form(_form(_text("x"), _text("x"))) == ["form field name 'x' repeated"]
    assert check_form(_form(_text("x"), _text("X"))) == []
    bad = {"name": "c", "label": "C", "type": "choice", "choices": ["a", "b"], "default": "z"}
    assert check_form(_form(bad)) == ["choice field 'c': default 'z' not in choices"]
    assert check_form(_form({**bad, "default": "a"})) == []
    areas = Areas()
    assert _feed(areas, [_note("bar", f"b{i}") for i in range(32)]) == []
    assert "32" in str(areas.see(_note("bar", "b32")))
    areas.see({"jsonrpc": "2.0", "method": "clear", "params": {"kind": "bar", "id": "b0"}})
    assert "32" in str(areas.see(_note("bar", "b0")))  # reopening a cleared id creates a new area, so it is the 33rd
    areas = Areas()
    msgs = [_note(k, f"i{i}") for i in range(16) for k in ("bar", "panel")]
    assert _feed(areas, msgs) == []
    assert areas.see(_note("table", "i0")) is not None
    areas.reset()
    assert areas.see(_note("table", "i0")) is None
