"""dock flow run|check <file>: load, check, show, ask, then run a flow's steps in one Session."""

from __future__ import annotations

import contextlib
import json
import secrets
import shutil
import sys
import time
from collections.abc import Callable
from pathlib import Path

from src import approve, catalog, flow, isolation, paths, uimsg
from src.inputs import InputSpec, UsageError, check_values
from src.lifecycle import Session
from src.manifest import Manifest, ManifestError
from src.process import PluginError
from src.stdio import ask_stdin
from src.store import Store, staged
from src.ui import make_ui

BRAIN_ID = "dev.dock.brain"

USAGE = """\
usage: dock flow run [--yes] [--keep] <file> [name=value...]
       dock flow check <file>
       dock do [--yes] [--keep] "<request>"
"""

DO_USAGE = """\
usage: dock do [--yes] [--keep] [--] "<request>"
  a request that starts with '-' goes after --, e.g. dock do -- "-5 degrees"
"""


def _err(text: str) -> None:
    print(text, file=sys.stderr)


def parse_params(f: flow.Flow, items: list[str]) -> dict:
    """name=value args typed by the flow's params; raises UsageError."""
    given: dict = {}
    for item in items:
        name, eq, value = item.partition("=")
        if not eq:
            raise UsageError(f"'{item}' is not name=value")
        if name not in f.params:
            raise UsageError(f"unknown param '{name}'")
        if name in given:
            raise UsageError(f"param '{name}' given more than once")
        if f.params[name]["type"] == "bool":
            if value not in ("true", "false"):
                raise UsageError(f"param '{name}' must be true or false, got '{value}'")
            given[name] = value == "true"
        else:
            given[name] = value
    specs = [InputSpec(n, p["type"], p["required"]) for n, p in f.params.items()]
    return check_values(specs, given)


def _load(path: Path) -> flow.Flow:
    return flow.load_toml(path)


def _save_path(source: Path, cwd: Path) -> Path:
    stem = source.name.removesuffix(".toml").removesuffix(".flow")
    return cwd / f"{stem}.flow.toml"


def _session_runner(store: Store, session: Session) -> flow.StepRunner:
    def call(plugin_id: str, command: str, inputs: dict | None) -> dict:
        return session.invoke_inputs(store.get(plugin_id), command, inputs)

    return call


def _show(lines: list[str], out=None) -> None:
    for line in lines:
        print(line, file=out or sys.stderr)


def _confirm(f: flow.Flow, source: Path, cwd: Path, yes: bool, interactive: bool | None,
             ask: Callable[[str], str | None] | None) -> bool:
    """Ask whether to run (y / N / s to save the flow first). Declined, refused or EOF is False; Ctrl-C propagates."""
    if yes:
        return True
    if interactive is None:
        interactive = sys.stdin.isatty() and sys.stderr.isatty()
    if not interactive:
        _err("dock: flow: needs a terminal to ask on; rerun with --yes to approve")
        return False
    ask = ask or ask_stdin
    while True:
        answer = ask("Run this flow? [y/N/s] ")
        word = answer.strip().lower() if answer is not None else ""
        if word in ("y", "yes"):
            return True
        if word != "s":
            _err("flow not run")
            return False
        target = _save_path(source, cwd)
        if target.exists():
            _err(f"dock: flow: {uimsg.sanitize(str(target))} exists; not overwritten")
            continue
        try:
            text = flow.to_toml(f)
            with open(target, "x", encoding="utf-8") as fh:  # "x": never overwrite, never follow a symlink
                fh.write(text)
        except (OSError, UnicodeError) as e:
            _err(f"dock: flow: cannot save {uimsg.sanitize(str(target))}: {uimsg.sanitize(str(e))}")
            continue
        _err(f"saved {uimsg.sanitize(str(target))}")


def cmd_flow(store: Store, args: list[str], runner: flow.StepRunner | None = None, interactive: bool | None = None,
             ask: Callable[[str], str | None] | None = None, cwd: Path | None = None, verbose: bool = False) -> int:
    """`runner` replaces the real Session (tests); `interactive` and `ask` replace the terminal."""
    if not args or args[0] not in ("run", "check"):
        print(USAGE, file=sys.stderr, end="")
        return 2
    sub = args[0]
    yes = sub == "run" and any(a in ("--yes", "-y") for a in args[1:])
    keep = sub == "run" and "--keep" in args[1:]
    rest = [a for a in args[1:] if not (sub == "run" and a in ("--yes", "-y", "--keep"))]
    for a in rest:
        if a.startswith("-"):
            _err(f"dock: flow {sub}: unknown option '{a}'")
            return 2
    if not rest or (sub == "check" and len(rest) != 1):
        print(USAGE, file=sys.stderr, end="")
        return 2
    path = paths.start_of(store) / rest[0]  # against the folder dock started in, not the out folder
    try:
        f = _load(path)
    except (flow.FlowError, OSError) as e:
        _err(f"dock: flow: {uimsg.sanitize(str(e))}")
        return 1
    params: dict = {}
    if sub == "run":
        try:
            params = parse_params(f, rest[1:])
        except UsageError as e:
            _err(f"dock: flow run: {uimsg.sanitize(str(e))}")
            return 2
    errors = flow.check(f, store, params if sub == "run" else None)
    if errors:
        for e in errors:
            _err(f"dock: flow: {uimsg.sanitize(str(e))}")
        return 1
    lines = flow.render(f, store)
    if sub == "check":
        _show(lines, sys.stdout)
        return 0
    _show(lines)
    paths.warn_if_in_unused(store, [store.get(pid) for pid in dict.fromkeys(s.plugin for s in f.steps)])
    cwd = cwd or paths.start_of(store)  # a saved plan goes next to where dock was started
    try:
        if not _confirm(f, path, cwd, yes, interactive, ask):
            return 1
    except KeyboardInterrupt:
        _err("")
        return 130
    return _run(f, params, store, runner, verbose, keep)


def _new_work_folder(store: Store) -> Path:
    """A fresh private folder under <dock home>/flows, named by UTC time and a random suffix."""
    name = f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{secrets.token_hex(4)}"
    folder = store.home / "flows" / name
    folder.parent.mkdir(parents=True, exist_ok=True)
    folder.mkdir(mode=0o700)
    folder.chmod(0o700)  # whatever the umask
    return folder


def _open(store: Store, runner: flow.StepRunner | None, verbose: bool, work: Path):
    """The step runner and the Session behind it (None when `runner` is a test's replacement)."""
    if runner is not None:
        return runner, None
    isolation.sweep(store)
    session = Session(store, lambda pid: make_ui(pid, verbose), verbose=verbose, work_dir=work)
    session.begin()  # one operation for the whole flow: a cancel() before it is stale, one after it stops the flow
    return _session_runner(store, session), session


def _run(f: flow.Flow, params: dict, store: Store, runner: flow.StepRunner | None, verbose: bool,
         keep: bool = False) -> int:
    try:
        work = _new_work_folder(store)
    except OSError as e:
        _err(f"dock: flow: cannot create the work folder: {uimsg.sanitize(str(e))}")
        return 1
    _err(f"work folder: {uimsg.sanitize(str(work))}")
    session: Session | None = None
    try:
        runner, session = _open(store, runner, verbose, work)
        return _execute(f, params, store, runner, work, keep, session)
    except KeyboardInterrupt:
        _err(f"work folder kept: {uimsg.sanitize(str(work))}")
        return 130
    finally:
        if session is not None:
            session.close()


def _execute(f: flow.Flow, params: dict, store: Store, runner: flow.StepRunner, work: Path, keep: bool,
             session: Session | None = None) -> int:
    shown = uimsg.sanitize(str(work))
    result = flow.run(f, params, runner, work, lambda line: _err(uimsg.sanitize(line)), store,
                      (lambda: session.cancelled) if session is not None else None)
    if result.ok and not keep:
        shutil.rmtree(work, ignore_errors=True)
    elif not result.ok:
        _err(f"kept {shown}")
    if result.ok and keep:
        _err(f"kept {shown}")
    if result.ok:
        _err(f"flow done ({result.done}/{len(f.steps)})")
        return 0
    if result.interrupted:
        _err(f"flow interrupted at step {result.failed_step}")
        return 130
    _err(f"flow failed at step {result.failed_step}: {uimsg.sanitize(result.reason or '')}")
    return 1


# dock do ---------------------------------------------------------------------------
def _catalog_command(c) -> dict:
    inputs = None if c.inputs is None else [{"name": i.name, "type": i.type, "required": bool(i.required)} for i in c.inputs]
    return {"name": c.name, "summary": c.summary, "inputs": inputs}


class _Lookup:
    """A store for flow.check and flow.render: installed plugins first, then the available (not installed) ones."""

    def __init__(self, store: Store, available: dict[str, Manifest]):
        self.store = store
        self.available = available
        self.dirs = store.dirs  # flow.render names the out folder in the Permissions block

    def get(self, plugin_id: str) -> Manifest:
        try:
            return self.store.get(plugin_id)
        except ManifestError:
            if plugin_id in self.available:
                return self.available[plugin_id]
            raise


def _catalog_entry(m: Manifest, installed: bool) -> dict:
    return {"id": m.id, "description": m.description, "permissions": list(m.permissions), "installed": installed,
            "commands": [_catalog_command(c) for c in m.commands]}


def write_catalog(store: Store, work: Path, available: dict[str, Manifest] | None = None) -> Path:
    """<work>/catalog.json: what the brain may use. Installed plugins, then available ones (not installed), no brain."""
    index = store.index()
    plugins = [_catalog_entry(store.get(pid), True) for pid in sorted(index) if pid != BRAIN_ID]
    for pid in sorted(available or {}):
        if pid != BRAIN_ID and pid not in index:
            plugins.append(_catalog_entry((available or {})[pid], False))
    path = work / "catalog.json"
    path.write_text(json.dumps({"plugins": plugins}, indent=1), encoding="utf-8")
    return path


def _plan(store, runner: flow.StepRunner, prompt: str, catalog: Path) -> flow.Flow:
    """Ask the brain for a flow and check it; raises FlowError with the reason when it cannot be used.

    `store` is a Store or a _Lookup (so steps may use available plugins)."""
    try:
        result = runner(BRAIN_ID, "plan", {"prompt": prompt, "catalog": str(catalog)})
    except (PluginError, ManifestError) as e:
        raise flow.FlowError(None, str(e)) from e
    code = result.get("exit_code", 0)
    if code != 0:
        raise flow.FlowError(None, f"the brain failed (exit code {code})")
    if "flow" not in result:
        raise flow.FlowError(None, "the brain gave no flow")
    f = flow.from_json(result["flow"])
    if any(p["required"] for p in f.params.values()):
        raise flow.FlowError(None, "the flow declares required params, and a planned flow has none to give")
    if any(step.plugin == BRAIN_ID for step in f.steps):
        raise flow.FlowError(None, "a step may not use the brain")
    errors = flow.check(f, store, {})
    if errors:
        raise flow.FlowError(None, "; ".join(str(e) for e in errors))
    return f


def _missing(f: flow.Flow, store: Store) -> list[str]:
    """Plugin ids the flow uses that are not installed, in step order, once each."""
    index = store.index()
    return [pid for pid in dict.fromkeys(s.plugin for s in f.steps) if pid not in index]


def _show_missing(missing: list[str], manifests: dict[str, Manifest], will_install: bool, dirs=None) -> None:
    """The not-installed block; it promises an install only when a y will really do one."""
    _err("Not installed (installed if you answer y):" if will_install else "Not installed:")
    for pid in missing:
        m = manifests[pid]
        _err(f"  {uimsg.sanitize(pid)} {uimsg.sanitize(m.version)}: "
             f"{', '.join(uimsg.sanitize(g) for g in approve.grants(m, dirs)) or 'none'}")


def _stage_missing(stack: contextlib.ExitStack, missing: list[str],
                   offered: dict[str, catalog.Available]) -> dict[str, tuple[Path, Manifest]]:
    """A private copy of each missing plugin's catalog folder, kept until `stack` closes, with its manifest.

    What is shown, approved and installed is read from these copies, so a later change to the catalog folder
    cannot add anything. Raises ManifestError when a copy is not the plugin the scan found."""
    result: dict[str, tuple[Path, Manifest]] = {}
    for pid in missing:
        source = offered[pid].folder
        folder = stack.enter_context(staged(source))
        m = Manifest.load(folder)
        if m.id != pid:
            raise ManifestError(f"{source} now holds {m.id}, not {pid}; nothing was installed")
        result[pid] = (folder, m)
    return result


def _install_missing(missing: list[str], folders: dict[str, tuple[Path, Manifest]], store: Store) -> bool:
    """Install each staged copy (already approved with the flow); False after the first failure."""
    for pid in missing:
        try:
            m = store.install(folders[pid][0])
        except (ManifestError, OSError) as e:
            _err(f"dock: do: cannot install {uimsg.sanitize(pid)}: {uimsg.sanitize(str(e))}")
            return False
        _err(f"installed {uimsg.sanitize(m.id)} {uimsg.sanitize(m.version)}")
    return True


def _stop_brain(session: Session | None) -> None:
    if session is not None:
        session.stop_plugin(BRAIN_ID)


def cmd_do(store: Store, args: list[str], runner: flow.StepRunner | None = None, interactive: bool | None = None,
           ask: Callable[[str], str | None] | None = None, cwd: Path | None = None, verbose: bool = False) -> int:
    """`dock do "<request>"`: the brain makes a flow, which is shown, approved and run like `dock flow run`.

    `runner` replaces the real Session for the brain and the steps alike (tests)."""
    opts, tail = args[:], []
    if "--" in args:
        i = args.index("--")
        opts, tail = args[:i], args[i + 1:]
    yes = any(a in ("--yes", "-y") for a in opts)
    keep = "--keep" in opts
    rest = [a for a in opts if a not in ("--yes", "-y", "--keep")]
    for a in rest:
        if a.startswith("-"):
            _err(f"dock: do: unknown option '{a}'")
            return 2
    rest += tail
    if len(rest) != 1 or not rest[0].strip():
        print(DO_USAGE, file=sys.stderr, end="")
        return 2
    try:
        store.get(BRAIN_ID)
    except ManifestError:
        _err("dock: do: the brain plugin is not installed; run: dock plugin install plugins/brain")
        return 1
    if interactive is None:
        interactive = sys.stdin.isatty() and sys.stderr.isatty()
    try:
        installed = store.index()
        offered: dict[str, catalog.Available] = {}
        try:
            found = catalog.load(store)
        except (OSError, ManifestError) as e:  # the catalog is optional: a broken one costs only the extras
            _err(f"dock: do: catalog skipped: {uimsg.sanitize(str(e))}")
            found = []
        repeated: set[str] = set()
        for a in found:
            pid = a.manifest.id
            if pid in installed:
                continue
            if pid in offered:
                if pid not in repeated:
                    repeated.add(pid)
                    _err(f"dock: do: {uimsg.sanitize(pid)} is in several catalog folders; "
                         f"using {uimsg.sanitize(str(offered[pid].folder))}")
                continue
            offered[pid] = a
        manifests = {pid: a.manifest for pid, a in offered.items()}
        lookup = _Lookup(store, manifests)
        work = _new_work_folder(store)
        catalog_file = write_catalog(store, work, manifests)
    except (OSError, ManifestError) as e:
        _err(f"dock: do: cannot prepare the work folder: {uimsg.sanitize(str(e))}")
        return 1
    shown = uimsg.sanitize(str(work))
    _err(f"work folder: {shown}")
    session: Session | None = None
    try:
        runner, session = _open(store, runner, verbose, work)
        try:
            f = _plan(lookup, runner, rest[0], catalog_file)
        except flow.FlowError as e:
            _stop_brain(session)
            shutil.rmtree(work, ignore_errors=True)  # no step ran: it only held catalog.json
            _err(f"dock: do: brain could not make a flow: {uimsg.sanitize(str(e))}")
            return 1
        _stop_brain(session)  # it has network and the work mount: not while the steps run
        missing = _missing(f, store)
        with contextlib.ExitStack() as stack:  # the staged copies live until the install is done
            try:
                copies = _stage_missing(stack, missing, offered)
            except (OSError, ManifestError) as e:
                shutil.rmtree(work, ignore_errors=True)
                _err(f"dock: do: cannot stage the plugins: {uimsg.sanitize(str(e))}")
                return 1
            staged_lookup = _Lookup(store, {pid: m for pid, (_, m) in copies.items()})
            errors = flow.check(f, staged_lookup, {})  # the copies are what will run: check them
            if errors:
                shutil.rmtree(work, ignore_errors=True)
                _err(f"dock: do: the plugins changed since the plan: {uimsg.sanitize('; '.join(str(e) for e in errors))}")
                return 1
            _show(flow.render(f, staged_lookup, set(missing)))
            paths.warn_if_in_unused(store, [staged_lookup.get(pid) for pid in dict.fromkeys(s.plugin for s in f.steps)])
            will_install = interactive and not yes
            if missing:
                _show_missing(missing, {pid: m for pid, (_, m) in copies.items()}, will_install, store.dirs)
                if not will_install:  # nothing is installed without an interactive y
                    for pid in missing:
                        _err(f"dock: do: {uimsg.sanitize(pid)} is not installed; run: dock plugin install {uimsg.sanitize(pid)}")
                    shutil.rmtree(work, ignore_errors=True)
                    return 1
            if not _confirm(f, Path("plan.flow.toml"), cwd or paths.start_of(store), yes, interactive, ask):
                shutil.rmtree(work, ignore_errors=True)
                return 1
            if not _install_missing(missing, copies, store):
                shutil.rmtree(work, ignore_errors=True)
                return 1
        return _execute(f, {}, store, runner, work, keep, session)
    except KeyboardInterrupt:
        _err(f"work folder kept: {shown}")
        return 130
    finally:
        if session is not None:
            session.close()
