"""The server tests keep copies of some host test assets, so each project's tests stay self-contained. These checks
fail when a copy drifts from the host original, instead of the two quietly testing different things."""

from pathlib import Path

SERVER_TESTS = Path(__file__).resolve().parent
HOST_TESTS = SERVER_TESTS.parent.parent.parent / "packages" / "host" / "tests"


def test_copied_fixtures_match_the_host_ones():
    # bug: a probe plugin is fixed in one project only, and the other keeps testing the old behaviour
    for folder in sorted(p for p in (SERVER_TESTS / "fixtures").iterdir() if p.is_dir()):
        original = HOST_TESTS / "fixtures" / folder.name
        assert original.is_dir(), f"fixtures/{folder.name} has no host original"
        for f in sorted(p for p in folder.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
            rel = f.relative_to(folder)
            assert (original / rel).read_bytes() == f.read_bytes(), f"fixtures/{folder.name}/{rel} drifted"
        for f in sorted(p for p in original.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
            rel = f.relative_to(original)
            assert (folder / rel).is_file(), f"fixtures/{folder.name}/{rel} exists in the host original but not here"


def test_dockenv_is_a_byte_copy_of_the_host_one():
    # bug: require_runtime / make_home change in one copy only
    assert (SERVER_TESTS / "dockenv.py").read_bytes() == (HOST_TESTS / "dockenv.py").read_bytes()


def test_paths_is_a_byte_copy_of_the_cli_one():
    # bug: src/paths.py is fixed in the CLI only (or here only) and the two resolve folders differently
    cli = SERVER_TESTS.parent.parent / "cli" / "src" / "paths.py"
    assert (SERVER_TESTS.parent / "src" / "paths.py").read_bytes() == cli.read_bytes()
