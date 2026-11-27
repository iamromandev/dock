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


MOVED = {"manifest", "inputs", "isolation", "process", "lifecycle", "store", "catalog", "uimsg", "approve", "flow",
         "notice", "threads", "uiproto"}


def test_no_moved_module_is_back_in_src():
    # bug: a moved module is copied back into src (with real code), and parts of the CLI use the stale copy
    assert sorted(m for m in MOVED if (SRC / f"{m}.py").exists()) == []


def test_nothing_imports_a_moved_module_from_src():
    # bug: an import of src.<moved> survives and only fails at run time, or a copy hides behind it
    problems = []
    for path in sorted([*SRC.glob("*.py"), *(SRC.parent / "tests").glob("*.py")]):
        for n in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = [a.name for a in n.names] if isinstance(n, ast.Import) else []
            if isinstance(n, ast.ImportFrom):
                mod = n.module or ""
                names = [mod] + ([f"src.{a.name}" for a in n.names] if mod == "src" else [])
            problems += [f"{path.name}: {x}" for x in names if x.startswith("src.") and x.split(".")[1] in MOVED]
    assert problems == []


def test_no_src_module_reexports_through_all():
    # bug: `from dock_host.store import Store` plus `__all__ = ["Store"]` turns a CLI module into a shim
    problems = []
    for path in sorted(SRC.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = {a.asname or a.name for n in tree.body if isinstance(n, ast.ImportFrom)
                    and (n.module or "").startswith("dock_host") for a in n.names}
        for n in tree.body:
            if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__all__" for t in n.targets):
                exported = {e.value for e in getattr(n.value, "elts", []) if isinstance(e, ast.Constant)}
                problems += [f"{path.name}: __all__ re-exports {x}" for x in sorted(exported & imported)]
    assert problems == []
