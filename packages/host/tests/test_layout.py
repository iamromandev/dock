"""dock_host stays free of the terminal and of the CLI around it: no `src` import at all, no terminal I/O."""

import ast
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent / "dock_host"
BANNED_CALLS = ("print", "input")  # print is allowed in notice.py, with sys.stderr: the default hook
BANNED_STREAMS = ("stdin", "stdout", "stderr", "__stdin__", "__stdout__", "__stderr__")
BANNED_MODULES = ("termios", "tty", "curses", "readline", "getpass")


def imports_src(tree: ast.AST) -> bool:
    """Whether a file imports `src` or anything under it (the CLI package, which dock_host must never know)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(a.name.split(".")[0] == "src" for a in node.names):
            return True
        if isinstance(node, ast.ImportFrom) and (node.level > 0 or (node.module or "").split(".")[0] == "src"):
            return True
    return False


def violations(name: str, tree: ast.AST) -> list[str]:
    """What a dock_host module does with the terminal, found in the syntax tree (comments and docstrings never count)."""
    found = ["imports src (or a relative import)"] if imports_src(tree) else []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [f"imports {a.name}" for a in node.names if a.name.split(".")[0] in BANNED_MODULES]
        elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] in BANNED_MODULES:
            found.append(f"imports {node.module}")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in BANNED_CALLS:
            if not (name == "notice" and node.func.id == "print"):
                found.append(f"calls {node.func.id}() on line {node.lineno}")
        elif isinstance(node, ast.Attribute):
            if node.attr == "isatty":
                found.append(f"uses .isatty on line {node.lineno}")
            elif node.attr in BANNED_STREAMS and isinstance(node.value, ast.Name) and node.value.id == "sys":
                if name == "notice" and node.attr == "stderr":
                    continue  # the default hook: the one place the core's text reaches stderr
                found.append(f"uses sys.{node.attr} on line {node.lineno}")
    return found


def test_dock_host_modules_are_host_neutral():
    # bug: a dock_host module reaches for the terminal or imports the CLI package again, so the host stops being reusable
    problems = []
    modules = sorted(PKG.glob("*.py"))
    assert len(modules) > 10
    for path in modules:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        problems += [f"{path.name} {v}" for v in violations(path.stem, tree)]
    assert problems == []
