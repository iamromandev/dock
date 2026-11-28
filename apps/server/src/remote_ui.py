"""The dock_host UI for a remote client: plugin messages become `ui` frames."""

from __future__ import annotations

from collections.abc import Callable

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
        self._ui("output", p)

    def log(self, p: dict) -> None:
        self._ui("log", p)

    def bar(self, p: dict) -> None:
        self._ui("bar", p)

    def table(self, p: dict) -> None:
        self._ui("table", p)

    def panel(self, p: dict) -> None:
        self._ui("panel", p)

    def clear(self, p: dict) -> None:
        self._ui("clear", p)

    def warn(self, message: str) -> None:
        self._ui("warn", {"message": message})

    def _ask(self, method: str, params: dict) -> dict:
        if self.asks is None:
            return {"eof": True}
        return self.asks.ask(self._send, lambda n: protocol.ask(self.run_id, n, method, params))

    def prompt_user(self, p: dict) -> dict:
        return self._ask("prompt_user", p)

    def form(self, p: dict) -> dict:
        return self._ask("form", p)

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
