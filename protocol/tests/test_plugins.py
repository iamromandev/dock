"""Every plugin in plugins/ passes the lifecycle check in its own container."""

from pathlib import Path

import pytest

from conformance import checks
from conformance.runtime import require_runtime
from conformance.session import PluginSession

PLUGINS = Path(__file__).resolve().parent.parent.parent / "plugins"
DIRS = sorted(p.parent for p in PLUGINS.glob("*/plugin.toml"))


def test_expected_plugins_are_discovered():
    # Bug caught: a wrong PLUGINS path makes the glob empty, so the parametrized lifecycle test silently runs nothing.
    assert {"brain", "dm", "extract", "image", "narrate", "story", "summarize", "transcribe"} <= {p.name for p in DIRS}


@pytest.mark.parametrize("plugin_dir", DIRS, ids=lambda p: p.name)
def test_plugin_passes_lifecycle(plugin_dir):
    # Bug caught: a plugin that breaks initialize, unknown-command errors, idle cancel or shutdown.
    with PluginSession(plugin_dir, require_runtime()) as s:
        checks.run_lifecycle(s)
