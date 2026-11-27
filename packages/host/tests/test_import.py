"""dock_host imports on its own, without the CLI package on the path."""

import importlib
import sys


def test_dock_host_imports_without_src():
    # bug: a dock_host module still needs `src`, so importing the package alone fails
    assert "src" not in sys.modules
    for name in ("dock_host.lifecycle", "dock_host.flow", "dock_host.ui"):
        importlib.import_module(name)
    assert "src" not in sys.modules
