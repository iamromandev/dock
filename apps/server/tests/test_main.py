import logging
import socket

import pytest

from src import main


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCK_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DOCK_SERVER_TOKEN", "t")
    monkeypatch.chdir(tmp_path)
    logger = logging.getLogger("websockets")
    level = logger.level
    yield
    logger.setLevel(level)


def test_port_in_use_prints_one_line_and_exits_1(env, capsys):
    # bug: a bind failure ends in a traceback instead of "dock-server: ..." and exit code 1
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        code = main.main(["--port", str(busy.getsockname()[1])])
    err = capsys.readouterr().err
    assert code == 1 and err.startswith("dock-server: ") and "Traceback" not in err and len(err.splitlines()) == 1


def test_websockets_logger_is_capped_at_warning(env):
    # bug: websockets logs frame payloads (the token) at DEBUG when the root level is lowered
    main.main(["--max-runs", "0"])
    assert logging.getLogger("websockets").level == logging.WARNING
