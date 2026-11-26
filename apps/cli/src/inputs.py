"""Typed command inputs: declaration model, validation and argument parsing.

Pure functions, no I/O. The host parses a command's args against its declared
inputs before any plugin starts, so mistakes fail fast with a clear message.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

TYPES = ("string", "int", "bool")
_FLAG = re.compile(r"--[A-Za-z0-9][A-Za-z0-9-]*")
_SHORT = re.compile(r"-[A-Za-z0-9]")
_INT = re.compile(r"[+-]?[0-9]+")


class UsageError(Exception):
    """The user's args do not fit the declared inputs. Callers prefix "dock: <command>: "."""


@dataclass(frozen=True)
class InputSpec:
    name: str
    type: str
    required: bool = False
    default: object = None  # None = no default
    flag: str | None = None  # "--long"; None = positional
    short: str | None = None  # "-x"


def specs_from_toml(command: str, raw: list) -> list[InputSpec]:
    """Build and validate the input specs of one command; ValueError names command and input."""
    if not isinstance(raw, list):
        raise ValueError(f"command '{command}': inputs must be an array")
    specs: list[InputSpec] = []
    names: set[str] = set()
    flags: set[str] = set()
    seen_optional_positional = False
    for item in raw:
        name = item.get("name") if isinstance(item, dict) else None
        if not isinstance(name, str) or not name:
            raise ValueError(f"command '{command}': input '{name if name else '?'}': 'name' must be a non-empty string")

        def fail(reason: str) -> ValueError:
            return ValueError(f"command '{command}': input '{name}': {reason}")

        if name in names:
            raise fail("duplicate name")
        names.add(name)
        if "type" not in item:
            raise fail("missing 'type'")
        typ = item["type"]
        if typ not in TYPES:
            raise fail(f"unknown type '{typ}'")
        required = item.get("required", False)
        if not isinstance(required, bool):
            raise fail("'required' must be a boolean")
        if typ == "bool":
            if "default" in item:
                raise fail("bool inputs cannot have a default")
            if required:
                raise fail("bool inputs cannot be required")
        default = item.get("default")
        if "default" in item:
            ok = {"string": isinstance(default, str), "bool": isinstance(default, bool),
                  "int": isinstance(default, int) and not isinstance(default, bool)}[typ]
            if not ok:
                raise fail(f"default must be {typ}")
            if required:
                raise fail("'required' and 'default' cannot be combined")
        flag, short = item.get("flag"), item.get("short")
        if flag is not None and not (isinstance(flag, str) and _FLAG.fullmatch(flag)):
            raise fail(f"malformed flag '{flag}'")
        if short is not None:
            if flag is None:
                raise fail("'short' needs a 'flag'")
            if not (isinstance(short, str) and _SHORT.fullmatch(short)):
                raise fail(f"malformed short '{short}'")
        for f in (flag, short):
            if f is not None:
                if f in flags:
                    raise fail(f"duplicate flag '{f}'")
                flags.add(f)
        if flag is None:
            if typ == "bool":
                raise fail("a positional input cannot be bool")
            if required and seen_optional_positional:
                raise fail("required positional cannot follow an optional positional")
            if not required:
                seen_optional_positional = True
        specs.append(InputSpec(name, typ, required, default, flag, short))
    return specs


def _convert(spec: InputSpec, value: str):
    if spec.type == "int":
        if not _INT.fullmatch(value):
            raise UsageError(f"input '{spec.name}' must be an integer, got '{value}'")
        return int(value)
    return value


def parse_args(specs: list[InputSpec], args: list[str]) -> dict:
    """Parse raw args into {name: value}; raises UsageError."""
    positionals = [s for s in specs if s.flag is None]
    by_flag: dict[str, InputSpec] = {}
    for s in specs:
        if s.flag is not None:
            by_flag[s.flag] = s
            if s.short is not None:
                by_flag[s.short] = s
    values: dict = {}
    pos: list[str] = []
    only_pos = False
    i = 0
    while i < len(args):
        a = args[i]
        if only_pos or not a.startswith("-") or a == "-":
            pos.append(a)
        elif a == "--":
            only_pos = True
        else:
            name, eq, val = a.partition("=")
            spec = by_flag.get(name)
            if spec is None:
                raise UsageError(f"unknown flag '{name}'")
            if spec.name in values:
                raise UsageError(f"flag '{name}' given more than once")
            if spec.type == "bool":
                if eq:
                    raise UsageError(f"flag '{name}' takes no value")
                values[spec.name] = True
            else:
                if not eq:
                    i += 1
                    if i >= len(args):
                        raise UsageError(f"flag '{name}' needs a value")
                    val = args[i]  # consumed even if it starts with '-'
                values[spec.name] = _convert(spec, val)
        i += 1
    if len(pos) > len(positionals):
        raise UsageError(f"unexpected argument '{pos[len(positionals)]}'")
    for spec, v in zip(positionals, pos):
        values[spec.name] = _convert(spec, v)
    for s in specs:
        if s.name in values:
            continue
        if s.required:
            raise UsageError(f"missing required input '{s.name}'")
        if s.type == "bool":
            values[s.name] = False
        elif s.default is not None:
            values[s.name] = s.default
    return values


def parse_for(manifest, command: str, args: list[str]) -> dict | None:
    """Inputs for one invocation: None when the command declares no inputs; raises UsageError."""
    cmd = manifest.command(command)
    if cmd is None or cmd.inputs is None:
        return None
    return parse_args(cmd.inputs, args)
