"""Vendors the dock_plugin kit into the plugins listed in plugins.txt.

    python3 -I tools/vendor.py sync|check [--root REPO]

sync mirrors packages/plugin-kit/dock_plugin/ byte for byte into plugins/<name>/dock_plugin/ (create, overwrite,
delete extras) and writes a VENDORED note. check changes nothing and exits 1 with one line per file that differs,
is missing or is extra. Stdlib only.
"""

import argparse
import sys
from pathlib import Path

KIT = "packages/plugin-kit"
NOTE = (
    "This folder is a copy of packages/plugin-kit/dock_plugin. Do not edit it here:\n"
    "edit the kit and run `make plugin-kit-sync`.\n"
)
FIX = "edit the kit and run make plugin-kit-sync"


def names(root: Path) -> list[str]:
    lines = (root / KIT / "plugins.txt").read_text(encoding="utf-8").splitlines()
    return [s for s in (line.strip() for line in lines) if s and not s.startswith("#")]


def files(folder: Path) -> dict[str, Path]:
    """Relative path (posix) -> file, for every file under folder except __pycache__ and the VENDORED note."""
    found = {}
    for path in sorted(folder.rglob("*")):
        rel = path.relative_to(folder)
        if path.is_file() and "__pycache__" not in rel.parts and rel.as_posix() != "VENDORED":
            found[rel.as_posix()] = path
    return found


def differences(root: Path) -> list[str]:
    kit = root / KIT / "dock_plugin"
    want = files(kit)
    problems = []
    for name in names(root):
        plugin = root / "plugins" / name
        if not plugin.is_dir():
            problems.append(f"plugins/{name} does not exist; fix {KIT}/plugins.txt")
            continue
        copy = plugin / "dock_plugin"
        have = files(copy) if copy.is_dir() else {}
        where = f"plugins/{name}/dock_plugin"
        for rel, path in want.items():
            if rel not in have:
                problems.append(f"{where}/{rel} is missing from the plugin; {FIX}")
            elif have[rel].read_bytes() != path.read_bytes():
                problems.append(f"{where}/{rel} differs from {KIT}; {FIX}")
        problems += [f"{where}/{rel} is not in {KIT}; {FIX}" for rel in have if rel not in want]
    return problems


def sync(root: Path) -> None:
    kit = root / KIT / "dock_plugin"
    want = files(kit)
    for name in names(root):
        plugin = root / "plugins" / name
        if not plugin.is_dir():
            sys.exit(f"plugins/{name} does not exist; fix {KIT}/plugins.txt")
        copy = plugin / "dock_plugin"
        copy.mkdir(exist_ok=True)
        for rel, path in files(copy).items():
            if rel not in want:
                path.unlink()
        for rel, path in want.items():
            target = copy / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())
        (copy / "VENDORED").write_text(NOTE, encoding="utf-8")
        print(f"plugins/{name}/dock_plugin: {len(want)} files")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=["sync", "check"])
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[3], help="the repo root")
    args = parser.parse_args()
    if args.action == "sync":
        sync(args.root)
        return 0
    problems = differences(args.root)
    for line in problems:
        print(line)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
