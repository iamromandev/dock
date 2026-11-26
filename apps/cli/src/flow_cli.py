"""dock flow run|check <file>: load, check, show, ask, then run a flow's steps in one Session."""

from __future__ import annotations

import json
import secrets
import shutil
import sys
import time
from collections.abc import Callable
from pathlib import Path

from src import approve, flow, isolation, uimsg
from src.inputs import InputSpec, UsageError, check_values
from src.lifecycle import Session
from src.manifest import ManifestError
from src.process import PluginError
from src.store import Store

PLANNER_ID = "dev.dock.planner"

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
    ask = ask or approve._ask_stdin
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
    path = Path(rest[0])
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
    cwd = cwd or Path.cwd()
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
    session = Session(store, verbose, work_dir=work)
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
        return _execute(f, params, store, runner, work, keep)
    except KeyboardInterrupt:
        _err(f"work folder kept: {uimsg.sanitize(str(work))}")
        return 130
    finally:
        if session is not None:
            session.close()


def _execute(f: flow.Flow, params: dict, store: Store, runner: flow.StepRunner, work: Path, keep: bool) -> int:
    shown = uimsg.sanitize(str(work))
    result = flow.run(f, params, runner, work, lambda line: _err(uimsg.sanitize(line)), store)
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


def write_catalog(store: Store, work: Path) -> Path:
    """<work>/catalog.json: what the planner may use, every installed plugin but the planner itself."""
    plugins = []
    for pid in sorted(store.index()):
        if pid == PLANNER_ID:
            continue
        m = store.get(pid)
        plugins.append({"id": m.id, "description": m.description, "permissions": list(m.permissions),
                        "commands": [_catalog_command(c) for c in m.commands]})
    path = work / "catalog.json"
    path.write_text(json.dumps({"plugins": plugins}, indent=1), encoding="utf-8")
    return path


def _plan(store: Store, runner: flow.StepRunner, prompt: str, catalog: Path) -> flow.Flow:
    """Ask the planner for a flow and check it; raises FlowError with the reason when it cannot be used."""
    try:
        result = runner(PLANNER_ID, "plan", {"prompt": prompt, "catalog": str(catalog)})
    except (PluginError, ManifestError) as e:
        raise flow.FlowError(None, str(e)) from e
    code = result.get("exit_code", 0)
    if code != 0:
        raise flow.FlowError(None, f"the planner failed (exit code {code})")
    if "flow" not in result:
        raise flow.FlowError(None, "the planner gave no flow")
    f = flow.from_json(result["flow"])
    if any(p["required"] for p in f.params.values()):
        raise flow.FlowError(None, "the flow declares required params, and a planned flow has none to give")
    if any(step.plugin == PLANNER_ID for step in f.steps):
        raise flow.FlowError(None, "a step may not use the planner")
    errors = flow.check(f, store, {})
    if errors:
        raise flow.FlowError(None, "; ".join(str(e) for e in errors))
    return f


def _stop_planner(session: Session | None) -> None:
    if session is not None:
        session.stop_plugin(PLANNER_ID)


def cmd_do(store: Store, args: list[str], runner: flow.StepRunner | None = None, interactive: bool | None = None,
           ask: Callable[[str], str | None] | None = None, cwd: Path | None = None, verbose: bool = False) -> int:
    """`dock do "<request>"`: the planner makes a flow, which is shown, approved and run like `dock flow run`.

    `runner` replaces the real Session for the planner and the steps alike (tests)."""
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
        store.get(PLANNER_ID)
    except ManifestError:
        _err("dock: do: the planner plugin is not installed; run: dock plugin install plugins/planner")
        return 1
    try:
        work = _new_work_folder(store)
        catalog = write_catalog(store, work)
    except (OSError, ManifestError) as e:
        _err(f"dock: do: cannot prepare the work folder: {uimsg.sanitize(str(e))}")
        return 1
    shown = uimsg.sanitize(str(work))
    _err(f"work folder: {shown}")
    session: Session | None = None
    try:
        runner, session = _open(store, runner, verbose, work)
        try:
            f = _plan(store, runner, rest[0], catalog)
        except flow.FlowError as e:
            _stop_planner(session)
            shutil.rmtree(work, ignore_errors=True)  # no step ran: it only held catalog.json
            _err(f"dock: do: planner could not make a flow: {uimsg.sanitize(str(e))}")
            return 1
        _stop_planner(session)  # it has network and the work mount: not while the steps run
        _show(flow.render(f, store))
        if not _confirm(f, Path("plan.flow.toml"), cwd or Path.cwd(), yes, interactive, ask):
            shutil.rmtree(work, ignore_errors=True)
            return 1
        return _execute(f, {}, store, runner, work, keep)
    except KeyboardInterrupt:
        _err(f"work folder kept: {shown}")
        return 130
    finally:
        if session is not None:
            session.close()
