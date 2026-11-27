# dock plugin protocol — version 2

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
  `output`, `log`, `bar`, `table`, `panel`, `clear`, `prompt_user` and `form` messages, so the host owns the UI.


## Environment the host sets

| Variable           | Meaning                          |
|--------------------|----------------------------------|
| `DOCK_PLUGIN_ID`   | The plugin's id from its manifest |
| `DOCK_API_VERSION` | Protocol version (`2`)           |

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
  contains `:`, `,` or `"` is refused. `invoke.cwd` is the out folder: the one the user chose with `--out` or
  `[paths] out`, else the folder dock was started in.
- The in folder: only for plugins that declare `fs:read:in`, the folder the user chose with `--in` or `[paths] in`
  (default: the out folder) is mounted **read-only** at the same path, and `invoke.in_dir` names it. The same refusals
  as for the cwd apply (filesystem root, home directory, anything above it, the dock home), and the path is checked
  again at launch. If it is the cwd of a plugin that also has `fs:write:cwd`, the one read-write mount covers it;
  folders nested in each other are both mounted, the inner one after the outer. The read-only mount is recursive only on
  Docker 25+ with kernel 5.12+; on older systems submounts inside the in folder may stay writable.
- Network: none, unless the plugin declares the `network` permission. `network` is all or nothing: the brain plugin
  (`dev.dock.brain`, used by `dock do`) declares plain `network` in v1, so it is not limited to the model endpoint
  (a known gap).
- Privileges: all capabilities dropped, `no-new-privileges`, run as the host user's uid:gid.

Permissions are `network`, `fs:write:cwd` and `fs:read:in`, under `[capabilities] permissions`.
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
| `invoke`      | `{command, args: [string], cwd, inputs?, work?}`| `{exit_code: int, ...}` | Run one command. The result may carry extra fields next to `exit_code`; `exit_code` stays required, and a host that does not know a field ignores it. The result may carry an optional `outputs` object of JSON values (for example `{"files": ["/w/a.zip"]}`), used by hosts that chain commands (flows) to pass one command's results to the next; a host that does not know it ignores it, and this is still protocol v2. A cancelled call always ends with `-32800`. `args` are the raw CLI args after the command name; the plugin may parse them, or use `inputs` (see Typed inputs). Resolve relative paths in `args` against `cwd` (the user's directory), not the plugin's own. |
| `shutdown`    | `{}`                                          | `{}`                  | Plugin must exit at once after replying, with code 0, without waiting for stdin to close, and must write nothing more to stdout. Host kills it after a timeout (the CLI waits 3 s; the conformance checker allows 5 s). |

## Host → plugin notifications

| Method   | Params | Notes |
|----------|--------|-------|
| `cancel` | `{}`   | User pressed Ctrl-C. Abort the running `invoke` and reply to it with error code `-32800`. A `cancel` that arrives while nothing is in flight must be ignored and must not affect later `invoke`s. A plugin that has not answered a few seconds after `cancel` (or after a second Ctrl-C) is killed by the host and started afresh on next use. |

## Plugin → host notifications

| Method   | Params                                          | Host behaviour |
|----------|-------------------------------------------------|----------------|
| `output` | `{text}`                                        | Written to stdout (include your own `\n`; partial text allows streaming). |
| `log`    | `{level, message}`                              | `debug` / `info` / `warn` / `error`, written to stderr. |
| `bar`    | `{id, label?, percent, message?, done?}`        | Creates or updates a progress bar. `percent` is 0-100, or `null` when unknown. `done: true` marks it finished. |
| `table`  | `{id, columns, rows, title?}`                   | Creates a table, or replaces its contents. `columns` is a list of strings, `rows` a list of lists of strings or numbers. |
| `panel`  | `{id, items, title?}`                           | Creates a key-value panel, or replaces its contents. `items` is a list of `[key, value]` pairs (strings or numbers). |
| `clear`  | `{id, kind}`                                    | Removes a live area. `kind` is `bar`, `table` or `panel`. |

A notification the host does not know, such as the `progress` of version 1, is ignored and logged to the plugin's
log file as `[unknown notification] <method>`.

### Live areas

`bar`, `table` and `panel` describe *live areas*. An area is identified by its `id` and its kind: the same `id`
used for two kinds makes two areas. Sending a message for an existing area replaces its contents; sending one for an
`id` that was cleared creates a new area. A `clear` for an unknown area is ignored. Areas belong to one `invoke`:
when it ends (success, error or cancel) the host shows their final state and forgets them.

- `percent` outside 0-100 is clamped; `NaN` counts as `null`.
- A bar with `percent: null` shows an activity marker instead of a bar; marked `done` it shows `done`.
- `label` and `message` are optional strings, empty by default. `title` is an optional string.
- Output written during an `invoke` (`output`, `log`) appears above the live areas. The areas' final state is written
  when the `invoke` ends, so in live mode a line such as dm's `saved ...` comes before the final panel and table
  in the scrollback, and in plain mode the areas print after it.
- The host does not limit how often an area is updated; a plugin should keep to about 5 updates a second for each
  area. dm does: a bar, panel or table of a torrent run changes at most every 0.2 s. The one exemption is the
  final update of a run (the closing `done` bars and the last panel and table), which is sent at once whatever
  the cap says, so that the last numbers on screen are the true ones.

## Plugin → host requests

| Method        | Params            | Result                           |
|---------------|-------------------|----------------------------------|
| `prompt_user` | `{prompt}`        | `{text}` or `{eof: true}` when input has ended |
| `form`        | `{title?, fields}` | `{values: {name: value}}` or `{eof: true}` when input has ended |

Any other request from a plugin (`call` and `http` included) is answered with error `-32601`.

### Forms

`fields` is a list of fields, each `{name, label, type, ...}` with a unique `name` and one of these types:

| `type`    | Extra keys                                       | Value in `values` |
|-----------|--------------------------------------------------|-------------------|
| `text`    | `default?` (string), `secret?` (bool, not echoed) | string            |
| `confirm` | `default?` (bool)                                | boolean           |
| `choice`  | `choices` (list of strings), `default?` (one of them) | string       |

A form that is cancelled (Ctrl-C) ends the run the same way a cancelled `prompt_user` does. An answer that
arrives after the plugin was cancelled is dropped. In plain mode (below) a field is asked up to 3 times, and a
form with no valid answer gets error `-32602`.

## Limits and invalid messages

| What                         | Limit |
|------------------------------|-------|
| `id` length                  | 64 characters |
| live areas per `invoke`      | 32 |
| table columns / rows         | 64 / 10,000 |
| form fields / choices        | 32 / 256 |

A message with a missing or wrongly typed field, or over a limit, is *invalid*. An invalid notification is dropped
and logged to the plugin's log file as `[invalid <method>] <reason>` (method and reason on one line, escaped and cut
short; for a host-side failure the reason is only the exception's class); the `invoke` goes on. An invalid `form`
request is answered with error `-32602` and the reason.

## Text the host shows

Everything a plugin sends as text (`output`, `log`, `prompt_user`, and every label, message, title, column, cell,
key, value and choice) is shown without control characters, in every rendering mode: C0 and C1 controls, DEL and
ESC are replaced with `U+FFFD`. So are Unicode format characters (category Cf: bidirectional overrides and isolates,
zero-width and invisible marks, tag characters) and the line and paragraph separators (`U+2028`, `U+2029`). The
zero-width joiner (`U+200D`) and non-joiner (`U+200C`) stay, so emoji sequences and Persian or Indic words keep their
shape; variation selectors are marks and stay too. The exceptions are the newline in `output` and `log`, and the tab
in `output`, which is shown as spaces. A plugin cannot send an escape sequence to the user's terminal.

## Rendering: live and plain

The host picks a mode per process. In *live* mode (stderr is a terminal, `TERM` is not `dumb`, and `DOCK_PLAIN` is
unset or empty) live areas are drawn on stderr and updated in place. In *plain* mode they are text, and no escape
sequence is written anywhere:

- a bar prints one line when it is marked `done`, or when the `invoke` ends;
- a table or panel prints its final contents, as aligned text, when it is cleared or the `invoke` ends;
- `output` and `log` print as they arrive;
- a form asks each field on stderr and reads one line per answer from stdin.

The plugin does not need to know which mode is in use.

## Keywords

A `[[commands]]` entry may have `keywords`, a list of non-empty strings that `dock search` matches (next to the
command's name and summary). Anything else (not a list, a non-string, an empty string) fails `dock plugin install`
with a message naming the command. A plugin never receives them.

```toml
[[commands]]
name = "greet"
summary = "Greet someone: greet [name]"
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

`invoke` params become `{command, args, cwd, inputs?, work?, in_dir?}`. `inputs` is an object
`{name: value}` with JSON types (string, integer, boolean), present only for
commands that declare `inputs` (an empty declaration gives `{}`). `args` stays
the unchanged raw list. When present, `inputs` is authoritative: a plugin
should use it instead of re-parsing `args`. The change is additive within
protocol v2.

`work` is an optional absolute path string, set only when the command runs as a
flow step: that folder is mounted read-write at that same path, and the plugin
may write there as well as in `cwd`. Plugins that do not know `work` ignore it.
Single-command runs never carry it. Still protocol v2.

`in_dir` is an optional absolute path string, sent only to plugins that declare `fs:read:in`: the user's in folder,
mounted read-only at that same path. Plugins without the permission get neither the mount nor the key; older plugins
ignore the key. Still protocol v2.


## Error codes

| Code     | Meaning |
|----------|---------|
| `-32601` | Unknown method or command |
| `-32602` | Bad arguments, or an invalid `form` |
| `-32800` | Cancelled |
| `-32000` | Any other plugin error (`message` is shown to the user) |

An error `code` that a plugin sends is passed on to whoever was waiting for it. A `code` that is not an
integer is replaced by `-32000`.

## Lifecycle

```
spawn → initialize → invoke* → shutdown → (kill on timeout)
```

`invoke` may repeat any number of times, sequentially, between
`initialize` and `shutdown` (the interactive `dock shell` keeps
plugins running between operations). A plugin must handle every `invoke` in
the same process and must not leak per-invoke state into the next one, unless
it documents otherwise. The host force-kills a plugin that ignores `cancel` past the grace period,
and removes its container whenever the plugin stops.

## Versioning

`api_version` in `plugin.toml` must match the host's: a manifest with another version fails to load with
`<id> needs protocol v<n>, this dock speaks v2`, at install and when an installed plugin is invoked. Changes inside
a version are additive only (new optional params, new methods). Breaking changes bump the version.

### Changes from v1

- `api_version` and `DOCK_API_VERSION` are `2`; version 1 plugins are refused, and every plugin in this repository
  moved to version 2. `dock plugin update` and `dock plugin remove` still work on an installed version 1 plugin.
- The `progress` notification is removed. Use `bar`.
- New notifications `bar`, `table`, `panel` and `clear`, and the new request `form`.
- Limits on ids, areas, tables and forms; invalid messages are dropped and logged.
- All text is sanitised by the host.
- `initialize` sends `api_version: "2"`.

Version 1 dropped the requests `call`, `http`, `provision` and `unprovision`, the notifications
`call_event`, `http_event` and `event`, the `ended_by_cancel` result field, the progress transfer fields
(`done`, `total`, `speed`, `name`, `phase`, `uploaded`, `peers`), the error code `-32001` and `DOCK_LISTEN_PORT`
without a version bump, because there are no plugins outside this repository.
