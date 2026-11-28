# Writing a dock plugin

A plugin is a container that speaks the JSON-RPC protocol of [protocol/PROTOCOL.md](../protocol/PROTOCOL.md) over stdin and stdout. Python plugins share the kit `dock_plugin` (moon project `plugin-kit`, [packages/plugin-kit](../packages/plugin-kit)): the protocol loop, cancel, safe files and network plumbing. It is Python standard library only.

## Layout of a plugin

```
plugins/<name>/
  main.py          thin: imports, a command table, rpc.run (about 60 lines at most)
  plugin.toml      the manifest
  <name>core/      the plugin's own logic, one module per concern
  dock_plugin/     the vendored kit (a copy; do not edit)
  tests/           unit tests on <name>core, a few end-to-end tests over stdio
  Dockerfile, moon.yml, pyproject.toml, ruff.toml, uv.lock, README.md
```

`main.py` holds no logic:

```python
from dock_plugin import rpc
from greetcore.commands import greet

rpc.run({"greet": greet}, "greet", "greeting")

# a command: def greet(inputs: dict, ctx: rpc.Context) -> dict   (the outputs)
```

## Kit or plugin core

A piece belongs in the kit when it is protocol, cancel, file, network plumbing or env parsing and at least two plugins use it. Everything about the plugin's domain (formats, parsing, prompts, algorithms) belongs in its `<name>core/`. Do not add a kit feature for one plugin.

Every kit module is optional: `rpc` imports only `ui` and `cancel`, never `net` or `files`, and the kit imports nothing outside the standard library (both are tested).

## Kit API

A command is `(inputs: dict, ctx: Context) -> dict`; the dict is sent as `{"exit_code": 0, "outputs": ...}`.

- `rpc.run(commands, name, label=None)`: serves `initialize`, `invoke`, `cancel` and `shutdown`. `name` is answered to `initialize` and is the progress bar id; `label` its label (default `name`). Closing stdin during an invoke cancels it. An unknown command is -32601.
- `rpc.Context`: `cwd` (absolute), `in_dir`, `work`, `call` (a `cancel.Call` for this invoke), `ask(method, params)` (a request to the host; waits for its result by id; `{"eof": True}` when stdin closes; an error answer raises `Failure`).
- `rpc.Failure(message, code=None)`: no code logs the message as an error and answers `{"exit_code": 1}`; a code (such as -32602) answers a JSON-RPC error. `rpc.Cancelled` answers -32800. Any other exception answers -32603.
- `rpc.require_str(inputs, name)`, `rpc.optional_str(inputs, name)`: -32602 when missing or not a string.
- `ui`: `say(level, text)`, `bar(percent, **extra)`, `table(id, title, columns, rows)`, `notify(method, **params)`, `send(msg)`.
- `cancel`: `Call` (`register(sock)`, `track(proc)`, `stop()`, `cancelled()`; the lock is never held across I/O), `open_socket(host, port, call, timeout, read_timeout=None)`, `run_worker(target, args, call, name)` (runs in a thread, returns the result, raises `Cancelled` fast on a cancel).
- `files`: `resolve_source(name, in_dir, work, who)` (inside the in or work folder, regular file, no escaping link, no FIFO), `plain_name(name, what, max_bytes=200)`, `Outputs` (`check(targets)` before the work; `publish(files, call)` writes `.name.part` and links it, never replacing; `with Outputs() as out:` discards what the run made when the block raises, a cancel included).
- `net`: `check_url(base, var)`, `request(base, path, body, call, service=, var=, start=, read_timeout=, max_reply=)` returns `(status, data)` and raises `Failure` with a message that names the service and how to start it.
- `env`: `positive_int(var, default)`.

## Start a new plugin

1. Create `plugins/<name>/` with `main.py`, `plugin.toml`, `<name>core/`, `tests/` and the project files; copy `moon.yml`, `pyproject.toml` and `ruff.toml` from a similar plugin.
2. Add `<name>` to `packages/plugin-kit/plugins.txt`.
3. Run `make plugin-kit-sync`. It creates `plugins/<name>/dock_plugin/` and a `VENDORED` note.
4. Dockerfile: `COPY main.py plugin.toml <name>core/ dock_plugin/ /plugin/`.
5. `moon.yml`: put `main.py`, `plugin.toml`, `Dockerfile`, `<name>core/**/*` and `dock_plugin/**/*` in the `sources` file group, so changes rerun the checks.
6. Write the tests, `make <name>-check` and `make <name>-test`.

## Changing the kit

Edit `packages/plugin-kit/dock_plugin/`, never a plugin's copy. Then `make plugin-kit-sync` copies the kit byte for byte into every listed plugin (and removes extra files). `make plugin-kit-check` and CI (`.github/workflows/plugin-kit.yml`) run `python3 -I tools/vendor.py check`, which fails with one line per file that differs, is missing or is extra, such as `plugins/transcribe/dock_plugin/files.py differs from packages/plugin-kit; edit the kit and run make plugin-kit-sync`.

## Test rules

- At most 100 tests per test folder.
- Unit tests on the core, plus a few end-to-end tests that drive `main.py` over stdio.
- Fakes for servers (a localhost HTTP server) and binaries (scripts on `PATH`); no Docker, GPU or model in unit tests.
- No sleep-based races: wait on an event, a message or a join with a timeout.

## Not yet on the kit

These plugins still carry their own copy of the plumbing and have no `dock_plugin/`: transcribe, summarize, extract, narrate, story, image, brain, dm. They move over one at a time, starting with transcribe.
