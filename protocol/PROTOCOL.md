# dock plugin protocol — version 1

This file is the contract. Any program, in any language, that follows it is a
dock plugin. The host never loads plugin code; it starts the plugin as a child
process and talks to it over stdin/stdout.

## Framing

- JSON-RPC 2.0, one JSON object per line (newline-delimited), UTF-8.
- Plugin reads requests from **stdin**, writes responses/notifications to **stdout**.
- **stdout is reserved for protocol messages.** Anything else (debug prints,
  stack traces) goes to **stderr**; the host writes it to
  `$DOCK_HOME/logs/<plugin-id>.log`.
- The plugin never touches the terminal. All user-facing I/O goes through the
  `output`, `log`, `progress` and `prompt_user` messages, so the host owns the UI.

## Environment the host sets

| Variable           | Meaning                          |
|--------------------|----------------------------------|
| `DOCK_PLUGIN_ID`   | The plugin's id from its manifest |
| `DOCK_API_VERSION` | Protocol version (`1`)           |

The plugin process runs with its install directory as the working directory,
so it can find its own files. The user's directory arrives in `invoke.cwd`.

## Host → plugin requests

| Method        | Params                                        | Result                | Notes |
|---------------|-----------------------------------------------|-----------------------|-------|
| `initialize`  | `{api_version, host:{name,version}, data_dir}` | `{name, ...}`         | Always first. `data_dir` is a private, persistent folder for the plugin. |
| `provision`   | `{}`                                          | `{}`                  | Only if manifest says `lifecycle.provision = true`. Acquire resources (load a model, open a connection). |
| `invoke`      | `{command, args: [string], cwd}`              | `{exit_code: int}`    | Run one command. `args` are the raw CLI args after the command name; the plugin parses them. Resolve relative paths in `args` against `cwd` (the user's directory), not the plugin's own. |
| `unprovision` | `{}`                                          | `{}`                  | Release what `provision` acquired. **Must be idempotent** and must work in a fresh process (see Recovery). |
| `shutdown`    | `{}`                                          | `{}`                  | Plugin should exit after replying. Host kills it after a timeout. |

## Host → plugin notifications

| Method   | Params | Notes |
|----------|--------|-------|
| `cancel` | `{}`   | User pressed Ctrl-C. Abort the running `invoke` (or provider `call`) and reply to it with error code `-32800`. |
| `call_event` | `{capability, data}` | A streamed event from a provider, during a `call` this plugin made. |

## Plugin → host notifications

| Method     | Params                       | Host behaviour |
|------------|------------------------------|----------------|
| `output`   | `{text}`                     | Written to stdout as-is (include your own `\n`; partial text allows streaming). |
| `log`      | `{level, message}`           | `debug` / `info` / `warn` / `error`, written to stderr. |
| `progress` | `{percent, message}`         | Progress bar on stderr. `percent` 0–100, or `null` if unknown. |

## Plugin → host requests

| Method        | Params     | Result                           |
|---------------|------------|----------------------------------|
| `prompt_user` | `{prompt}` | `{text}` or `{eof: true}` when input has ended |
| `call`        | `{capability, method, params}` | whatever the provider returns (see Capabilities) |

## Capabilities: plugins using other plugins

A plugin can serve a named **capability** to other plugins, for example the
llm plugin serves `llm` and chat uses it. Plugins never talk to each other
directly; the host relays every message.

Declared in `plugin.toml`:

```toml
[capabilities]
provides = ["llm"]   # on the provider
requires = ["llm"]   # on the user of it
```

Flow for chat calling `llm.generate`:

```
chat ──call {capability:"llm", method:"generate", params}──▶ host
host ──call {capability:"llm", method:"generate", params}──▶ llm
llm  ──event {data:{token}}  (any number)──────────────────▶ host
host ──call_event {capability:"llm", data:{token}}─────────▶ chat
llm  ──result {text}───────────────────────────────────────▶ host ──▶ chat
```

Rules:

- **Provider side:** a plugin that `provides` a capability must answer the
  host request `call {capability, method, params}`. It may send any number of
  `event {data}` notifications while working; the host forwards each to the
  caller as `call_event {capability, data}`. What `method`, `params`, `data`
  and the result mean is defined by the capability, not by this protocol (the
  `llm` capability is documented in `plugins/llm/main.py`).
- **Caller side:** a plugin may only call capabilities listed in its
  `requires`; anything else is rejected.
- **Startup:** before invoking a command, the host starts and provisions one
  provider for each capability the plugin `requires`. A missing provider
  fails the command before it starts. Providers live for the whole dock
  command and are unprovisioned and stopped after it, newest first.
- **Choosing a provider:** if several installed plugins provide the same
  capability, the user picks one in `$DOCK_HOME/config.toml`:

  ```toml
  [providers]
  llm = "dev.dock.llm"
  ```

- **Isolation:** providers cannot use `prompt_user`; only the plugin running
  the command talks to the user. `log` and `progress` from providers are shown
  as usual.
- **Cancel:** on Ctrl-C the host sends `cancel` to every plugin in the session.
- **Cycles:** a call that would re-enter a capability already being called is
  rejected.

## Error codes

| Code     | Meaning |
|----------|---------|
| `-32601` | Unknown method or command |
| `-32602` | Bad arguments |
| `-32800` | Cancelled |
| `-32000` | Any other plugin error (`message` is shown to the user) |

## Lifecycle

```
spawn → initialize → [provision] → invoke → [unprovision] → shutdown → (kill on timeout)
```

## Recovery

Before `provision`, the host writes `$DOCK_HOME/state/<id>.provisioned`. It
deletes the marker after `unprovision` succeeds. If dock or the plugin is
killed hard, the marker survives; on the next run the host starts the plugin,
calls `initialize` then `unprovision`, and removes the marker. So `unprovision`
must be able to clean up using only what is on disk in `data_dir`.

## Versioning

`api_version` in `plugin.toml` must match the host's. Changes inside version 1
are additive only (new optional params, new methods). Breaking changes bump the
version, and the host may support several versions side by side.
