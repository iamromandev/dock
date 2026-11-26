"""Linear multi-plugin flows: model, loading, static check, render and the runner.

A flow is a few steps that run in order, each one command of one plugin. A step's input values may refer to flow
params ({params.x}), the shared work folder ({work}) and the outputs of an earlier step ({steps.N.field[i]}).
Nothing here starts a process: the caller passes in a StepRunner.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from src import approve, uimsg
from src.inputs import TYPES, UsageError, check_values
from src.manifest import Manifest, ManifestError
from src.process import PluginError

MAX_STEPS = 16
REF = re.compile(r"\{(params\.\w+|work|steps\.\d+\.\w+(?:\[\d+\])?)\}")
_LOOKS_LIKE_REF = re.compile(r"\{(?:params|work|steps)\b[^{}]*\}")
_NAME = re.compile(r"\w+")
_TOP = {"params", "steps"}
_STEP_KEYS = {"plugin", "command", "inputs", "label"}
_PARAM_KEYS = {"type", "required"}


class FlowError(Exception):
    """A problem with a flow; `step` is the 1-based step it is about, None when it is about the flow."""

    def __init__(self, step: int | None, reason: str):
        super().__init__(f"step {step}: {reason}" if step is not None else reason)
        self.step = step
        self.reason = reason


@dataclass(frozen=True)
class Step:
    plugin: str
    command: str
    inputs: dict = field(default_factory=dict)
    label: str | None = None


@dataclass(frozen=True)
class Flow:
    params: dict[str, dict]  # name -> {"type": ..., "required": ...}
    steps: tuple[Step, ...]


# loading -------------------------------------------------------------------------
def _unknown(where: str, obj: dict, allowed: set[str], step: int | None) -> None:
    for key in obj:
        if key not in allowed:
            raise FlowError(step, f"{where}unknown key '{key}'")


def _step(n: int, raw) -> Step:
    if not isinstance(raw, dict):
        raise FlowError(n, "a step must be a table")
    _unknown("", raw, _STEP_KEYS, n)
    for key in ("plugin", "command"):
        if not isinstance(raw.get(key), str) or not raw[key]:
            raise FlowError(n, f"'{key}' must be a non-empty string")
    inputs = raw.get("inputs", {})
    if not isinstance(inputs, dict):
        raise FlowError(n, "inputs must be a table")
    for k, v in inputs.items():
        if not isinstance(v, (str, int, bool)):
            raise FlowError(n, f"input '{k}' must be a string, integer or boolean")
    label = raw.get("label")
    if label is not None and not isinstance(label, str):
        raise FlowError(n, "'label' must be a string")
    return Step(raw["plugin"], raw["command"], dict(inputs), label)


def from_json(obj) -> Flow:
    """A Flow from parsed JSON/TOML; strict: raises FlowError for anything unexpected."""
    if not isinstance(obj, dict):
        raise FlowError(None, "a flow must be a table")
    _unknown("", obj, _TOP, None)
    raw_params = obj.get("params", {})
    if not isinstance(raw_params, dict):
        raise FlowError(None, "params must be a table")
    params: dict[str, dict] = {}
    for name, p in raw_params.items():
        if not _NAME.fullmatch(name):
            raise FlowError(None, f"param name '{name}' must be letters, digits and _")
        if not isinstance(p, dict):
            raise FlowError(None, f"param '{name}' must be a table")
        _unknown(f"param '{name}': ", p, _PARAM_KEYS, None)
        if p.get("type") not in TYPES:
            raise FlowError(None, f"param '{name}': type must be one of {', '.join(TYPES)}")
        required = p.get("required", False)
        if not isinstance(required, bool):
            raise FlowError(None, f"param '{name}': required must be true or false")
        params[name] = {"type": p["type"], "required": required}
    raw_steps = obj.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise FlowError(None, "a flow needs at least one step")
    if len(raw_steps) > MAX_STEPS:
        raise FlowError(None, f"a flow has at most {MAX_STEPS} steps, got {len(raw_steps)}")
    return Flow(params, tuple(_step(i, s) for i, s in enumerate(raw_steps, 1)))


def load_toml(path: Path) -> Flow:
    try:
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise FlowError(None, f"{path}: {e}") from e
    return from_json(data)


def _q(text: str) -> str:
    """A TOML basic string."""
    out = []
    for ch in text:
        if ch in '"\\':
            out.append("\\" + ch)
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _v(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value) if isinstance(value, int) else _q(value)


def to_toml(f: Flow) -> str:
    """The flow as TOML text that load_toml reads back to an equal Flow."""
    out: list[str] = []
    for name, p in f.params.items():
        out += [f"[params.{_q(name)}]", f"type = {_q(p['type'])}", f"required = {_v(p['required'])}", ""]
    for s in f.steps:
        out += ["[[steps]]", f"plugin = {_q(s.plugin)}", f"command = {_q(s.command)}"]
        out.append("inputs = { " + ", ".join(f"{_q(k)} = {_v(v)}" for k, v in s.inputs.items()) + " }")
        if s.label is not None:
            out.append(f"label = {_q(s.label)}")
        out.append("")
    return "\n".join(out)


# checking ------------------------------------------------------------------------
def _refs(value) -> list[str]:
    return REF.findall(value) if isinstance(value, str) else []


def _is_ref_valued(value) -> bool:
    return isinstance(value, str) and (bool(REF.search(value)) or bool(_LOOKS_LIKE_REF.search(value)))


def _check_refs(n: int, key: str, value, flow: Flow) -> list[FlowError]:
    errs: list[FlowError] = []
    if not isinstance(value, str):
        return errs
    for token in _LOOKS_LIKE_REF.findall(value):
        if not REF.fullmatch(token):
            errs.append(FlowError(n, f"input '{key}': malformed reference {token}"))
    for ref in _refs(value):
        if ref.startswith("params."):
            if ref[7:] not in flow.params:
                errs.append(FlowError(n, f"input '{key}': {{{ref}}} is not a declared param"))
        elif ref.startswith("steps."):
            k = int(ref.split(".")[1])
            if not 1 <= k < n:
                errs.append(FlowError(n, f"input '{key}': {{{ref}}} must refer to an earlier step (1 to {n - 1}), not step {k}"))
    return errs


def check(f: Flow, store, params: dict | None = None) -> list[FlowError]:
    """Everything wrong with the flow that can be known before it runs; all of it, not just the first.

    With `params` (the values a run was given), a {params.x} that has no value is reported too.
    """
    errs: list[FlowError] = []
    for n, s in enumerate(f.steps, 1):
        try:
            m = store.get(s.plugin)
        except ManifestError as e:
            errs.append(FlowError(n, str(e)))
            continue
        cmd = m.command(s.command)
        if cmd is None:
            errs.append(FlowError(n, f"{s.plugin} has no command '{s.command}'"))
            continue
        step_errs: list[FlowError] = []
        for k, v in s.inputs.items():
            step_errs += _check_refs(n, k, v, f)
            if params is not None:
                for ref in _refs(v):
                    if ref.startswith("params.") and ref[7:] in f.params and ref[7:] not in params:
                        step_errs.append(FlowError(n, f"input '{k}': {{{ref}}} has no value; pass {ref[7:]}=..."))
        errs += step_errs
        if cmd.inputs is None:
            if s.inputs:
                errs.append(FlowError(n, "command takes no inputs"))
            continue
        deferred = {k for k, v in s.inputs.items() if _is_ref_valued(v)}
        names = {sp.name for sp in cmd.inputs}
        for k in sorted(deferred - names):
            errs.append(FlowError(n, f"unknown input '{k}'"))
        literal = {k: v for k, v in s.inputs.items() if k not in deferred}
        try:
            check_values([sp for sp in cmd.inputs if sp.name not in deferred], literal)
        except UsageError as e:
            errs.append(FlowError(n, str(e)))
    return errs


# rendering -----------------------------------------------------------------------
def _s(text) -> str:
    return uimsg.sanitize(str(text))


def _what(s: Step) -> str:
    """`<plugin> <command>`, then the label (extra text only) in brackets; not sanitized."""
    return f"{s.plugin} {s.command}" + (f" ({s.label})" if s.label is not None else "")


def render(f: Flow, store) -> list[str]:
    """What the flow will do, one line per step, then what each plugin may do; safe to print."""
    lines = []
    for n, s in enumerate(f.steps, 1):
        args = " ".join(f"{_s(k)}={_s(v)}" for k, v in s.inputs.items())
        lines.append(f"{n}. {_s(_what(s))}" + (f" {args}" if args else ""))
    lines += ["", "Permissions:"]
    for pid in dict.fromkeys(s.plugin for s in f.steps):
        try:
            grants = approve.grants(store.get(pid))
        except ManifestError:
            lines.append(f"  {_s(pid)}: not installed")
            continue
        lines.append(f"  {_s(pid)}: {', '.join(_s(g) for g in grants) or 'none'}")
    return lines


# running -------------------------------------------------------------------------
def fill(value, ctx: dict):
    """`value` with references replaced. ctx: {"params": {...}, "work": str, "steps": {N: outputs}}.

    A value that is exactly one reference keeps the JSON type of what it refers to; a reference inside other text
    becomes a string. Raises FlowError (step None) naming the reference that has no value.
    """
    if isinstance(value, dict):
        return {k: fill(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [fill(v, ctx) for v in value]
    if not isinstance(value, str):
        return value
    whole = REF.fullmatch(value)
    if whole:
        return _lookup(whole.group(1), ctx)
    return REF.sub(lambda m: str(_lookup(m.group(1), ctx)), value)


def _lookup(ref: str, ctx: dict):
    if ref == "work":
        return ctx["work"]
    if ref.startswith("params."):
        name = ref[7:]
        if name not in ctx["params"]:
            raise FlowError(None, f"{ref} has no value")
        return ctx["params"][name]
    _, n, rest = ref.split(".", 2)
    m = re.fullmatch(r"(\w+)(?:\[(\d+)\])?", rest)
    assert m is not None  # REF guarantees it
    outputs = ctx["steps"].get(int(n))
    if outputs is None or m.group(1) not in outputs:
        raise FlowError(None, f"{ref} is not in the outputs of step {n}")
    got = outputs[m.group(1)]
    if m.group(2) is None:
        return got
    i = int(m.group(2))
    if not isinstance(got, list) or i >= len(got):
        raise FlowError(None, f"{ref} is not in the outputs of step {n}")
    return got[i]


class StepRunner(Protocol):
    def __call__(self, plugin_id: str, command: str, inputs: dict | None) -> dict:
        """Run one command (inputs None: the command declares none); returns {"exit_code": int, "outputs": {...}?}. May raise PluginError."""
        ...


@dataclass
class FlowResult:
    ok: bool
    done: int
    failed_step: int | None = None
    reason: str | None = None
    outputs: list[dict] = field(default_factory=list)  # per finished step
    interrupted: bool = False


def _failed(n: int, outputs: list[dict], reason: str, interrupted: bool = False) -> FlowResult:
    return FlowResult(False, n - 1, n, reason, outputs, interrupted)


def run(f: Flow, params: dict, steps: StepRunner, work: Path, report: Callable[[str], None], store=None) -> FlowResult:
    """Run the steps strictly in order; stops at the first one that fails, is cancelled or raises PluginError or ManifestError.

    `store` supplies the commands' declared inputs: each step's filled inputs are checked against them before the
    step starts. A command that declares no inputs gets None; without a store inputs are passed as they are.
    """
    ctx: dict = {"params": params, "work": str(work), "steps": {}}
    outputs: list[dict] = []
    total = len(f.steps)
    for n, s in enumerate(f.steps, 1):
        try:
            inputs: dict | None = fill(s.inputs, ctx)
            m: Manifest | None = store.get(s.plugin) if store is not None else None
            cmd = m.command(s.command) if m is not None else None
            if cmd is not None and cmd.inputs is not None:
                inputs = check_values(cmd.inputs, inputs)  # type: ignore[arg-type]
            elif cmd is not None:
                if inputs:
                    raise FlowError(None, "command takes no inputs")
                inputs = None
        except FlowError as e:
            return _failed(n, outputs, e.reason)
        except (UsageError, ManifestError) as e:
            return _failed(n, outputs, str(e))
        report(f"[{n}/{total}] {_what(s)}")
        try:
            result = steps(s.plugin, s.command, inputs)
        except KeyboardInterrupt:
            return _failed(n, outputs, "interrupted", True)
        except (PluginError, ManifestError) as e:
            return _failed(n, outputs, str(e))
        code = result.get("exit_code", 0)
        if code == 130:
            return _failed(n, outputs, "interrupted", True)
        if code != 0:
            return _failed(n, outputs, f"exit code {code}")
        out = result.get("outputs")
        if out is not None and not isinstance(out, dict):
            report(f"warning: step {n}: outputs is not an object; ignored")
        outputs.append(out if isinstance(out, dict) else {})
        ctx["steps"][n] = outputs[-1]
    return FlowResult(True, total, None, None, outputs)
