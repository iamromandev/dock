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

The plugin runs in a container with `/plugin` as the working directory, so it can
find its own files. The user's directory arrives in `invoke.cwd`.

## Running in a container

Every plugin runs in a container. `[run]` must hold exactly one entry, `container`:

```toml
[run]
container = { cmd = "python3", args = ["/plugin/main.py"], dockerfile = "Dockerfile" }
```

`cmd` is required; `args` and `dockerfile` (relative to the plugin folder, must
stay inside it) are optional. Any other key in `[run]` (`any`, a platform name, `wasm`)
fails the install. The host builds an image from that Dockerfile, on first use or
with `dock plugin build <id>`, and runs `cmd` in it with
`<runtime> run -i --rm --pull never ...`; the Dockerfile should put the plugin folder at `/plugin`
(`COPY . /plugin`), which is also the working directory. The protocol is plain:
the plugin speaks JSON-RPC on stdin/stdout, and gets `DOCK_PLUGIN_ID` and `DOCK_API_VERSION`.

What the container gets:

- Environment: `LANG`, `TZ` (if the host has them), `HOME=/tmp` and those two ids, plus the image's own
  `PATH` (the host's `PATH` is not passed on).
- Files: a read-only root and a writable `/tmp`; `data_dir` is mounted read-write at the same path;
  `invoke.cwd` is mounted read-write at the same path only for plugins that declare `fs:write:cwd`.
  The host refuses to start such a plugin when the cwd is the filesystem root, the user's home directory,
  any folder above it, or the dock home or something above or inside it. A `data_dir` or cwd path that
  contains `:`, `,` or `"` is refused.
- Network: none, unless the plugin declares the `network` permission.
- Privileges: all capabilities dropped, `no-new-privileges`, run as the host user's uid:gid.

Permissions are `network` and `fs:write:cwd`, under `[capabilities] permissions`.
A plugin that needs more of the host's environment (say an API key) declares the
variable names in `[capabilities]`:

```toml
[capabilities]
env = ["MY_API_URL", "MY_API_KEY"]
```

Each declared name that is set on the host (even to the empty string) is passed
into the container as a bare `-e NAME`: the runtime client reads the value from
its own environment, so the value never appears on the command line (and so not
in the process list). Names that are not set on the host are not passed, and
nothing that is not declared gets in. Names must match `[A-Z_][A-Z0-9_]*` and may
not be one dock sets itself (`HOME`, `PATH`, `LANG`, `TZ`, `DOCK_PLUGIN_ID`,
`DOCK_API_VERSION`); `Manifest.load` rejects the manifest otherwise.
The `id` and `version` become the image tag `dock/<id>:<version>`, so they may only contain
letters, digits, `.`, `_` and `-`, and must not start with `.` or `-`. Installing a plugin copies its
whole folder, because the Dockerfile builds from the installed folder.

A manifest that uses a removed feature fails the install: `provides` and `requires` under `[capabilities]`,
`[capabilities].http`, the permission `network:listen`, `requires_permission` on a command and
`[lifecycle].provision`.

## Host → plugin requests

| Method        | Params                                        | Result                | Notes |
|---------------|-----------------------------------------------|-----------------------|-------|
| `initialize`  | `{api_version, host:{name,version}, data_dir}` | `{name, ...}`         | Always first. `data_dir` is a private, persistent folder for the plugin. |
| `invoke`      | `{command, args: [string], cwd, inputs?}`     | `{exit_code: int, ...}` | Run one command. The result may carry extra fields next to `exit_code`; `exit_code` stays required, and a host that does not know a field ignores it. A cancelled call always ends with `-32800`. `args` are the raw CLI args after the command name; the plugin may parse them, or use `inputs` (see Typed inputs). Resolve relative paths in `args` against `cwd` (the user's directory), not the plugin's own. |
| `shutdown`    | `{}`                                          | `{}`                  | Plugin should exit after replying. Host kills it after a timeout. |

## Host → plugin notifications

| Method   | Params | Notes |
|----------|--------|-------|
| `cancel` | `{}`   | User pressed Ctrl-C. Abort the running `invoke` and reply to it with error code `-32800`. A `cancel` that arrives while nothing is in flight must be ignored and must not affect later `invoke`s. A plugin that has not answered a few seconds after `cancel` (or after a second Ctrl-C) is killed by the host and started afresh on next use. |

## Plugin → host notifications

| Method     | Params                       | Host behaviour |
|------------|------------------------------|----------------|
| `output`   | `{text}`                     | Written to stdout as-is (include your own `\n`; partial text allows streaming). |
| `log`      | `{level, message}`           | `debug` / `info` / `warn` / `error`, written to stderr. |
| `progress` | `{percent, message}`         | Progress bar on stderr. `percent` 0-100, or `null` if unknown. |

## Plugin → host requests

| Method        | Params     | Result                           |
|---------------|------------|----------------------------------|
| `prompt_user` | `{prompt}` | `{text}` or `{eof: true}` when input has ended |

Any other request from a plugin (`call` and `http` included) is answered with error `-32601`.

## Keywords

A `[[commands]]` entry may have `keywords`, a list of non-empty strings that `dock search` matches (next to the
command's name and summary). Anything else (not a list, a non-string, an empty string) fails `dock plugin install`
with a message naming the command. A plugin never receives them.

```toml
[[commands]]
name = "hello"
summary = "Greet someone: hello [name]"
keywords = ["greet", "welcome"]
```

## Typed inputs

A `[[commands]]` entry may declare its inputs. The host then parses and
validates the user's args before starting any plugin, and sends the result as
`inputs` in `invoke`. Commands without an `inputs` key are unchanged: no
parsing, no `inputs` param.

```toml
[[commands]]
name = "fetch"
inputs = [
  { name = "url",     type = "string", required = true },
  { name = "output",  type = "string", flag = "--output", short = "-o" },
  { name = "retries", type = "int",    flag = "--retries", default = 3 },
  { name = "quiet",   type = "bool",   flag = "--quiet" },
]
```

| Key        | Required | Meaning |
|------------|----------|---------|
| `name`     | yes      | Key in `inputs`. Unique within the command. |
| `type`     | yes      | `string`, `int` (base-10 integer) or `bool`. |
| `required` | no       | Default `false`. Cannot be combined with `default`; not allowed for `bool`. |
| `default`  | no       | Value used when not given; must match `type`. Not allowed for `bool` (a `bool` is `false` unless its flag is given). |
| `flag`     | no       | `--long` form. An input without `flag` is positional (not allowed for `bool`). |
| `short`    | no       | `-x` form (one character, no bundling); needs `flag`. |

Other keys in an input table are ignored, so newer manifests that add keys
still load on this host.

Parsing: positionals fill positional inputs in declared order (a required
positional cannot follow an optional one); flags may appear anywhere, as
`--name value`, `--name=value`, `-x value` or `-x=value`; a value-taking flag consumes the
next word even if it starts with `-`; a `bool` flag takes no value and sets
`true`; `--` ends flag parsing. An input not given takes its `default`, a
`bool` is `false`, and other optional inputs are absent. Invalid declarations
are rejected at install; invalid args fail with `dock: <command>: <message>`
(exit code 2 in one-shot use) and the plugin is not started.

`invoke` params become `{command, args, cwd, inputs?}`. `inputs` is an object
`{name: value}` with JSON types (string, integer, boolean), present only for
commands that declare `inputs` (an empty declaration gives `{}`). `args` stays
the unchanged raw list. When present, `inputs` is authoritative: a plugin
should use it instead of re-parsing `args`. The change is additive within
protocol v1.


## Error codes

| Code     | Meaning |
|----------|---------|
| `-32601` | Unknown method or command |
| `-32602` | Bad arguments |
| `-32800` | Cancelled |
| `-32000` | Any other plugin error (`message` is shown to the user) |

An error `code` that a plugin sends is passed on to whoever was waiting for it. A `code` that is not an
integer is replaced by `-32000`.

## Lifecycle

```
spawn → initialize → invoke* → shutdown → (kill on timeout)
```

`invoke` may repeat any number of times, sequentially, between
`initialize` and `shutdown` (the interactive `dock shell` and `dock serve` keep
plugins running between operations). A plugin must handle every `invoke` in
the same process and must not leak per-invoke state into the next one, unless
it documents otherwise. The host force-kills a plugin that ignores `cancel` past the grace period,
and removes its container whenever the plugin stops.

## Versioning

`api_version` in `plugin.toml` must match the host's. Changes inside version 1
are additive only (new optional params, new methods). Breaking changes bump the
version, and the host may support several versions side by side.

Version 1 dropped the requests `call`, `http`, `provision` and `unprovision`, the notifications
`call_event`, `http_event` and `event`, the `ended_by_cancel` result field, the progress transfer fields
(`done`, `total`, `speed`, `name`, `phase`, `uploaded`, `peers`), the error code `-32001` and `DOCK_LISTEN_PORT`
without a version bump, because there are no plugins outside this repository.
