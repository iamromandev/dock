"""The host core (what later moves to packages/host) stays free of the terminal and of the CLI around it."""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
CORE = ("manifest", "inputs", "isolation", "process", "lifecycle", "store", "catalog", "uimsg", "approve", "flow",
        "notice", "uiproto", "threads")
FORBIDDEN = ("ui", "term", "live", "editor", "keys", "areas", "main", "shell", "flow_cli", "paths")
BANNED_CALLS = ("print", "input")  # print is allowed in notice.py, with sys.stderr: the default hook
BANNED_STREAMS = ("stdin", "stdout", "stderr", "__stdin__", "__stdout__", "__stderr__")
BANNED_MODULES = ("termios", "tty", "curses", "readline", "getpass")


def imported(tree: ast.AST) -> set[str]:
    """The src modules a file imports, as bare names (`src.term` and `from src import term` both give `term`)."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[1] for a in node.names if a.name.startswith("src.")}
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "src":
                names |= {a.name for a in node.names}
            elif node.module.startswith("src."):
                names.add(node.module.split(".")[1])
    return names


def violations(name: str, tree: ast.AST) -> list[str]:
    """What a core module does with the terminal, found in the syntax tree (comments and docstrings never count)."""
    found = [f"imports src.{m}" for m in sorted(imported(tree) & set(FORBIDDEN))]
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


def test_core_modules_are_host_neutral():
    # bug: a core module reaches for the terminal (or a CLI module) again, so it can not move to the host package
    problems = []
    for name in CORE:
        tree = ast.parse((SRC / f"{name}.py").read_text(encoding="utf-8"))
        problems += [f"{name}.py {v}" for v in violations(name, tree)]
    assert problems == []
