"""Protocol rules that JSON Schema cannot express. Each works on a message that already passed its schema."""

from __future__ import annotations


def check_form(params: dict) -> list[str]:
    """Problems with a form request's params: repeated field names, a choice default not among its choices."""
    errors: list[str] = []
    seen: set[str] = set()
    for field in params.get("fields", []):
        name = field["name"]
        if name in seen:
            errors.append(f"form field name {name!r} repeated")
        seen.add(name)
        if field["type"] == "choice" and "default" in field and field["default"] not in field["choices"]:
            errors.append(f"choice field {name!r}: default {field['default']!r} not in choices")
    return errors


class Areas:
    """Counts the areas (bar, table, panel) a plugin opens during one invoke; the limit is 32.

    A (kind, id) pair opens an area the first time it is seen, and again after a clear for it. A clear never
    lowers the count.
    """

    LIMIT = 32
    KINDS = ("bar", "table", "panel")

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._open: set[tuple[str, str]] = set()
        self._opened = 0

    def see(self, msg: dict) -> str | None:
        """Feed one incoming notification; an error message when it opens an area past the limit."""
        method, params = msg.get("method"), msg.get("params", {})
        if method == "clear":
            self._open.discard((params["kind"], params["id"]))
            return None
        if method not in self.KINDS or (method, params["id"]) in self._open:
            return None
        self._open.add((method, params["id"]))
        self._opened += 1
        if self._opened > self.LIMIT:
            return f"{self._opened} areas opened in one invoke; the limit is {self.LIMIT} areas"
        return None
