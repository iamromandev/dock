import pytest

from dock_plugin.cancel import Failure
from dock_plugin.env import positive_int


@pytest.mark.parametrize("value", [None, ""])
def test_unset_or_empty_gives_the_default(monkeypatch, value):
    monkeypatch.delenv("X_N", raising=False)
    if value is not None:
        monkeypatch.setenv("X_N", value)
    assert positive_int("X_N", 5) == 5


def test_a_positive_integer_is_used(monkeypatch):
    monkeypatch.setenv("X_N", "42")
    assert positive_int("X_N", 5) == 42


@pytest.mark.parametrize("value", ["0", "-1", "1.5", "abc", " 3"])
def test_anything_else_is_refused_naming_the_variable_and_quoting_the_value(monkeypatch, value):
    monkeypatch.setenv("X_N", value)
    with pytest.raises(Failure) as caught:
        positive_int("X_N", 5)
    assert str(caught.value) == f"X_N must be a positive integer, not {value!r}"
    assert caught.value.code is None


def test_a_long_bad_value_is_cut_to_30_characters(monkeypatch):
    monkeypatch.setenv("X_N", "z" * 100)
    with pytest.raises(Failure, match=r"not 'z{30}'$"):
        positive_int("X_N", 5)
