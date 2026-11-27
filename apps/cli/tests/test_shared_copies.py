"""The CLI tests keep copies of some host test assets, so each project's tests stay self-contained. These checks
fail when a copy drifts from the host original, instead of the two quietly testing different things."""

import ast
from pathlib import Path

CLI_TESTS = Path(__file__).resolve().parent
HOST_TESTS = CLI_TESTS.parent.parent.parent / "packages" / "host" / "tests"

# The parts that differ on purpose: the CLI builds images through `python -m src`, and its CliCase runs dock.
MEANT_TO_DIFFER = {"dockenv.py": {"prebuild"}, "catalogkit.py": {"CliCase"}}
COPIES = {"dockenv.py": "dockenv.py", "catalogkit.py": "test_catalog.py", "flowkit.py": "test_flow.py"}


def _top_level(path: Path) -> dict[str, str]:
    """Source of each top-level function, class and constant assignment, by name."""
    src = path.read_text(encoding="utf-8")
    out: dict[str, str] = {}
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef | ast.ClassDef):
            out[node.name] = ast.get_source_segment(src, node) or ""
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    out[t.id] = ast.get_source_segment(src, node) or ""
    return out


def test_copied_fixtures_match_the_host_ones():
    # bug: a probe plugin is fixed in one project only, and the other keeps testing the old behaviour
    for folder in sorted(p for p in (CLI_TESTS / "fixtures").iterdir() if p.is_dir()):
        original = HOST_TESTS / "fixtures" / folder.name
        assert original.is_dir(), f"fixtures/{folder.name} has no host original"
        for f in sorted(p for p in folder.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
            rel = f.relative_to(folder)
            assert (original / rel).read_bytes() == f.read_bytes(), f"fixtures/{folder.name}/{rel} drifted"


def test_copied_helpers_match_the_host_ones():
    # bug: a shared helper (require_runtime, make_plugin, FakeStore, ...) changes in one copy only
    drift = []
    for cli_name, host_name in COPIES.items():
        mine, theirs = _top_level(CLI_TESTS / cli_name), _top_level(HOST_TESTS / host_name)
        for name, body in mine.items():
            if name in MEANT_TO_DIFFER.get(cli_name, set()) or name in {"ROOT"}:
                continue
            if name in theirs and theirs[name] != body:
                drift.append(f"{cli_name}:{name} differs from {host_name}")
    assert drift == []
