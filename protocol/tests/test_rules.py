"""Rules a JSON Schema cannot express, checked on messages that already passed their schema."""

from conformance.rules import Areas, check_form


def _form(*fields):
    return {"fields": list(fields)}


def _text(name):
    return {"name": name, "label": "L", "type": "text"}


def test_repeated_field_name_is_reported_case_sensitively():
    # Bug caught: duplicate names pass (or "A" and "a" are wrongly treated as the same name).
    assert check_form(_form(_text("x"), _text("x"))) == ["form field name 'x' repeated"]
    assert check_form(_form(_text("x"), _text("X"))) == []


def test_choice_default_must_be_in_choices():
    # Bug caught: a default outside the choices is accepted.
    bad = {"name": "c", "label": "C", "type": "choice", "choices": ["a", "b"], "default": "z"}
    assert check_form(_form(bad)) == ["choice field 'c': default 'z' not in choices"]
    assert check_form(_form({**bad, "default": "a"})) == []


def _note(kind, id_):
    return {"jsonrpc": "2.0", "method": kind, "params": {"id": id_}}


def _feed(areas, msgs):
    return [e for m in msgs if (e := areas.see(m))]


def test_the_area_limit_holds_at_the_33rd_and_clear_does_not_free_a_slot():
    # Bug caught: the 32-area limit is not enforced or is off by one, or clear lowers the count so a plugin
    # cycling areas slips past the limit.
    areas = Areas()
    assert _feed(areas, [_note("bar", f"b{i}") for i in range(32)]) == []
    assert "32" in str(areas.see(_note("bar", "b32")))
    areas.see({"jsonrpc": "2.0", "method": "clear", "params": {"kind": "bar", "id": "b0"}})
    assert "32" in str(areas.see(_note("bar", "b0")))  # reopening a cleared id creates a new area, so it is the 33rd


def test_same_id_in_two_kinds_counts_twice_and_reset_forgets():
    # Bug caught: ids are counted across kinds, so 16 bars plus 16 panels fail; reset leaks between invokes.
    areas = Areas()
    msgs = [_note(k, f"i{i}") for i in range(16) for k in ("bar", "panel")]
    assert _feed(areas, msgs) == []
    assert areas.see(_note("table", "i0")) is not None
    areas.reset()
    assert areas.see(_note("table", "i0")) is None
