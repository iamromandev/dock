import os

import pytest
from dock_host.store import Store

from src import config


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "home")


def test_token_from_env_wins_over_config_file(store, monkeypatch):
    # bug: the file token is preferred and an operator's env override is ignored
    (store.home / "config.toml").write_text('[server]\ntoken = "from-file"\n')
    monkeypatch.setenv("DOCK_SERVER_TOKEN", "from-env")
    assert config.load(store, []).token == "from-env"


def test_token_from_config_file(store, monkeypatch):
    # bug: a token in [server] is not read
    monkeypatch.delenv("DOCK_SERVER_TOKEN", raising=False)
    (store.home / "config.toml").write_text('[server]\ntoken = "from-file"\n')
    assert config.load(store, []).token == "from-file"


def test_no_token_is_a_config_error(store, monkeypatch):
    # bug: the server starts open to anyone on localhost when no token is configured
    monkeypatch.delenv("DOCK_SERVER_TOKEN", raising=False)
    with pytest.raises(config.ConfigError, match="DOCK_SERVER_TOKEN"):
        config.load(store, [])


def test_flags_set_port_max_runs_and_folders(store, monkeypatch, tmp_path):
    # bug: flags are ignored, or --in/--out are not resolved once at start
    monkeypatch.setenv("DOCK_SERVER_TOKEN", "t")
    (tmp_path / "in").mkdir()
    cfg = config.load(store, ["--port", "0", "--max-runs", "2", "--in", str(tmp_path / "in"), "--out", str(tmp_path / "out")])
    assert (cfg.port, cfg.max_runs) == (0, 2)
    assert cfg.dirs.in_dir == (tmp_path / "in").resolve()
    assert cfg.dirs.out_dir == (tmp_path / "out").resolve() and cfg.dirs.out_dir.is_dir()


@pytest.mark.parametrize("argv", [["--host", "0.0.0.0"], ["--port", "abc"], ["--max-runs", "0"], ["--port"], ["--port", "65536"], ["--port", "-1"]])
def test_bad_flags_are_config_errors(store, monkeypatch, argv):
    # bug: a host flag or junk value is accepted (exposing the server or crashing with a traceback)
    monkeypatch.setenv("DOCK_SERVER_TOKEN", "t")
    with pytest.raises(config.ConfigError):
        config.load(store, argv)


def test_token_is_removed_from_the_environment(store, monkeypatch):
    # bug: the token stays in os.environ, where a plugin's env passthrough could hand it on
    monkeypatch.setenv("DOCK_SERVER_TOKEN", "t")
    config.load(store, [])
    assert "DOCK_SERVER_TOKEN" not in os.environ
