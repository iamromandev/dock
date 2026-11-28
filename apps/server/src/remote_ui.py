"""The dock_host UI for a remote client: plugin messages become `ui` frames.

Params go through the same dock_host.uimsg helpers as the CLI, so every client gets cleaned, normalised params. An
invalid message raises uimsg.Invalid, which the host turns into -32602 (requests) or a log line (notifications).
"""

from __future__ import annotations

from collections.abc import Callable

from dock_host import uimsg

from src import protocol
from src.asks import Asks


class RemoteUI:
    def __init__(self, send: Callable[[dict], None], run_id: str, asks: Asks | None = None):
        self._send = send
        self.run_id = run_id
        self.asks = asks

    def _ui(self, method: str, params: dict) -> None:
        self._send(protocol.ui(self.run_id, method, params))

    def begin(self) -> None:
        pass

    def end(self) -> None:
        pass

    def output(self, p: dict) -> None:
        self._ui("output", {"text": uimsg.sanitize(str(p.get("text", "")), newlines=True, tabs=True)})

    def log(self, p: dict) -> None:
        level = uimsg.sanitize(str(p.get("level", "info")))
        self._ui("log", {"level": level, "message": uimsg.sanitize(str(p.get("message", "")), newlines=True)})

    def bar(self, p: dict) -> None:
        self._ui("bar", uimsg.bar(p))

    def table(self, p: dict) -> None:
        self._ui("table", uimsg.table(p))

    def panel(self, p: dict) -> None:
        self._ui("panel", uimsg.panel(p))

    def clear(self, p: dict) -> None:
        self._ui("clear", uimsg.clear(p))

    def warn(self, message: str) -> None:
        self._ui("warn", {"message": message})

    def _ask(self, method: str, params: dict) -> dict:
        if self.asks is None:
            return {"eof": True}
        return self.asks.ask(self._send, lambda n: protocol.ask(self.run_id, n, method, params))

    def prompt_user(self, p: dict) -> dict:
        return self._ask("prompt_user", {"prompt": uimsg.sanitize(str(p.get("prompt", "> ")))})

    def form(self, p: dict) -> dict:
        """The client sees display text only (`choices`, `default`) and answers a choice with its displayed text; the
        plugin gets the raw value back, mapped here."""
        data = uimsg.form(p)
        fields = {f["name"]: f for f in data["fields"]}
        shown = {**data, "fields": [{k: v for k, v in f.items() if k not in ("values", "raw_default")}
                                    for f in data["fields"]]}
        result = self._ask("form", shown)
        values = result.get("values")
        if isinstance(values, dict):
            for name, v in values.items():
                f = fields.get(name)
                if f is not None and f["type"] == "choice":
                    if v not in f["choices"]:
                        raise uimsg.Invalid(f"no valid answer for {name}")
                    values[name] = f["values"][f["choices"].index(v)]
        return result

    def handlers(self) -> dict:
        return {
            "output": self.output,
            "log": self.log,
            "bar": self.bar,
            "table": self.table,
            "panel": self.panel,
            "clear": self.clear,
            "prompt_user": self.prompt_user,
            "form": self.form,
        }
