"""apps/cli/src keeps no copy or re-export of what moved to dock_host."""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"


def test_no_src_module_reexports_dock_host():
    # bug: a leftover shim (`from dock_host.x import *` or a module of only dock_host imports) hides the move
    problems = []
    for path in sorted(SRC.glob("*.py")):
        body = [
            n
            for n in ast.parse(path.read_text(encoding="utf-8")).body
            if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))  # docstring
        ]
        froms = [n for n in body if isinstance(n, ast.ImportFrom) and (n.module or "").split(".")[0] == "dock_host"]
        problems += [f"{path.name}: star import of {n.module}" for n in froms if any(a.name == "*" for a in n.names)]
        if body and all(isinstance(n, ast.Import | ast.ImportFrom) for n in body) and froms:
            problems.append(f"{path.name}: only imports")
    assert problems == []
