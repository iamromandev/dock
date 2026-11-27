"""The checker stays independent of the host: it never imports or reads apps/cli."""

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLI_PYPROJECT = ROOT.parent / "apps" / "cli" / "pyproject.toml"


def _cli_packages() -> list[str]:
    """Top-level import names of the CLI, from its hatch wheel config (today `src`)."""
    data = tomllib.loads(CLI_PYPROJECT.read_text())
    return [Path(p).parts[0] for p in data["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]]


def _sources() -> list[Path]:
    return [*ROOT.glob("conformance/**/*.py"), *ROOT.glob("tests/*.py")]


def test_conformance_does_not_use_the_host():
    # Bug caught: a shortcut import of the host's code makes the checker agree with the host by construction.
    packages = _cli_packages()
    assert packages, "could not find the CLI's package name"
    pattern = re.compile(rf"^\s*(from|import)\s+({'|'.join(map(re.escape, packages))})\b", re.M)
    for path in _sources():
        text = path.read_text()
        if path.name != Path(__file__).name:  # this file names the pattern it searches for
            assert "apps/cli" not in text, path.name
        assert not pattern.search(text), path.name
