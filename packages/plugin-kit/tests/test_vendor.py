"""tools/vendor.py on a temporary copy of the repo layout."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

KIT = Path(__file__).resolve().parent.parent
VENDOR = KIT / "tools" / "vendor.py"


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    kit = root / "packages" / "plugin-kit"
    shutil.copytree(KIT / "dock_plugin", kit / "dock_plugin", ignore=shutil.ignore_patterns("__pycache__"))
    (kit / "plugins.txt").write_text("# plugins\n\nalpha\nbeta\n", encoding="utf-8")
    for name in ("alpha", "beta"):
        (root / "plugins" / name).mkdir(parents=True)
    return root


def vendor(root, action):
    done = subprocess.run([sys.executable, "-I", str(VENDOR), action, "--root", str(root)], capture_output=True, text=True)
    return done.returncode, done.stdout + done.stderr


def test_sync_then_check_passes_and_writes_the_note(repo):
    assert vendor(repo, "sync")[0] == 0
    assert vendor(repo, "check") == (0, "")
    for name in ("alpha", "beta"):
        copy = repo / "plugins" / name / "dock_plugin"
        assert (copy / "rpc.py").read_bytes() == (KIT / "dock_plugin" / "rpc.py").read_bytes()
        assert "make plugin-kit-sync" in (copy / "VENDORED").read_text()


def test_sync_overwrites_and_deletes_extras_but_skips_pycache(repo):
    vendor(repo, "sync")
    copy = repo / "plugins" / "alpha" / "dock_plugin"
    (copy / "ui.py").write_text("edited")
    (copy / "extra.py").write_text("x")
    (copy / "__pycache__").mkdir()
    (copy / "__pycache__" / "ui.pyc").write_bytes(b"x")
    assert vendor(repo, "check")[0] == 1
    assert vendor(repo, "sync")[0] == 0
    assert vendor(repo, "check") == (0, "")
    assert not (copy / "extra.py").exists()
    assert (copy / "ui.py").read_bytes() == (KIT / "dock_plugin" / "ui.py").read_bytes()


def test_an_edited_file_fails_naming_it(repo):
    vendor(repo, "sync")
    (repo / "plugins" / "beta" / "dock_plugin" / "files.py").write_text("edited")
    code, out = vendor(repo, "check")
    assert code == 1
    assert out.splitlines() == ["plugins/beta/dock_plugin/files.py differs from packages/plugin-kit; "
                                "edit the kit and run make plugin-kit-sync"]


def test_an_extra_file_fails_naming_it(repo):
    vendor(repo, "sync")
    (repo / "plugins" / "alpha" / "dock_plugin" / "stray.py").write_text("x")
    code, out = vendor(repo, "check")
    assert code == 1
    assert out.startswith("plugins/alpha/dock_plugin/stray.py is not in packages/plugin-kit")


def test_a_missing_file_fails_naming_it(repo):
    vendor(repo, "sync")
    (repo / "plugins" / "alpha" / "dock_plugin" / "net.py").unlink()
    code, out = vendor(repo, "check")
    assert code == 1
    assert out.startswith("plugins/alpha/dock_plugin/net.py is missing from the plugin")


def test_a_plugin_that_was_never_synced_fails_for_every_file(repo):
    code, out = vendor(repo, "check")
    assert code == 1
    assert "plugins/alpha/dock_plugin/rpc.py is missing" in out
    assert "plugins/beta/dock_plugin/rpc.py is missing" in out


def test_a_listed_folder_that_does_not_exist_fails_naming_it(repo):
    shutil.rmtree(repo / "plugins" / "beta")
    code, out = vendor(repo, "check")
    assert code == 1
    assert "plugins/beta does not exist" in out
    assert vendor(repo, "sync")[0] != 0


def test_the_real_repo_passes_its_own_check():
    assert vendor(KIT.parent.parent, "check") == (0, "")
