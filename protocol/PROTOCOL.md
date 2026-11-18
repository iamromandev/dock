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

### Running in a container

A manifest may add a `container` entry to `[run]`, next to the native ones:

```toml
[run]
any       = { cmd = "python3", args = ["main.py"] }
container = { cmd = "python3", args = ["/plugin/main.py"], dockerfile = "Dockerfile" }
```

`cmd` is required; `args` and `dockerfile` (relative to the plugin folder, must
stay inside it) are optional. When the user enables isolation, the host builds
an image from that Dockerfile and runs `cmd` in it with `<runtime> run -i`; the
Dockerfile should put the plugin folder at `/plugin` (`COPY . /plugin`), which is
also the working directory. The protocol is unchanged: the plugin still speaks
JSON-RPC on stdin/stdout, and still gets `DOCK_PLUGIN_ID` and `DOCK_API_VERSION`.
Inside the container the environment is limited to `LANG`, `TZ` (if the
host has them), `HOME=/tmp` and those two, plus the image's own `PATH` (the
host's `PATH` is not passed on); `data_dir` is mounted at the same path,
and `invoke.cwd` is reachable only for plugins that declare `fs:write:cwd`, and the
host refuses to start such a plugin when the cwd is `/`, the user's home
directory, or the dock home or something above or inside it.
A plugin that needs more of the host's environment (say an API key) declares the
variable names in `[capabilities]`:

```toml
[capabilities]
env = ["DOCK_LLM_BASE_URL", "DOCK_LLM_API_KEY"]
```

Each declared name that is set on the host (even to the empty string) is passed
into the container as a bare `-e NAME`: the runtime client reads the value from
its own environment, so the value never appears on the command line (and so not
in the process list). Names that are not set on the host are not passed, and
nothing that is not declared gets in. Names must match `[A-Z_][A-Z0-9_]*` and may
not be one dock sets itself (`HOME`, `PATH`, `LANG`, `TZ`, `DOCK_PLUGIN_ID`,
`DOCK_API_VERSION`); `Manifest.load` rejects the manifest otherwise. A native
plugin ignores `env`: it already inherits the whole environment.
No network without the `network` permission. A plugin with only a `container`
entry has no native build and cannot run unless isolation is on. Installing a
plugin that declares `container` copies its whole folder (including sources such
as `*.go` and `go.mod`, which a native-only install leaves out), because the
Dockerfile builds from the installed folder. `dock plugin build <id>` rebuilds
the image on demand. With `container`, the `id` and `version` become the image
tag `dock/<id>:<version>`, so they may only contain letters, digits, `.`, `_`
and `-`, and must not start with `.` or `-`.

### Running as WebAssembly

A manifest may declare `wasm` in `[run]`; the host then runs the plugin with the
`wasmtime` command line, whatever other entries or isolation settings there are:

```toml
[run]
wasm = { module = "plugin.wasm" }                            # a WASI module in the plugin folder
wasm = { python = "3.15", script = "main.py", args = [] }    # a script on the interpreter the host pins
```

Exactly one form; `module` and `script` must exist inside the plugin folder, `python` must be
a version the host provides, and `args` (optional) follow the module or script. The protocol
is unchanged: JSON-RPC on the module's stdin/stdout, with `DOCK_PLUGIN_ID` and
`DOCK_API_VERSION` set. The guest sees only:

- its plugin folder at `/plugin`, read-only (for Python, the standard library at `/python`, read-only);
- `data_dir` at its host path, read-write;
- `invoke.cwd` at its host path, read-write, only with `fs:write:cwd` (refused for `/`, the user's
  home, or the dock home or something above or inside it, as with containers);
- the environment `HOME` (a folder inside `data_dir`), `DOCK_PLUGIN_ID`, `DOCK_API_VERSION`, and the
  `[capabilities] env` names that are set on the host (by name, never by value on the command line).

There is no network: a manifest that declares `network` is rejected at install (the `http` request,
see HTTP through the host, is how a WASM plugin reaches hosts it declares). WASI 0.1 has no
threads, so a Python WASM plugin reads stdin in one loop and sees a `cancel` notification only between
messages; the host's force kill after the cancel grace ends a step that does not return.

## Host → plugin requests

| Method        | Params                                        | Result                | Notes |
|---------------|-----------------------------------------------|-----------------------|-------|
| `initialize`  | `{api_version, host:{name,version}, data_dir}` | `{name, ...}`         | Always first. `data_dir` is a private, persistent folder for the plugin. |
| `provision`   | `{}`                                          | `{}`                  | Only if manifest says `lifecycle.provision = true`. Acquire resources (load a model, open a connection). |
| `invoke`      | `{command, args: [string], cwd, inputs?}`     | `{exit_code: int, ...}` | Run one command. The result may carry extra fields next to `exit_code` (for example the download plugin's `paths`); `exit_code` stays required, and a host that does not know a field ignores it. `args` are the raw CLI args after the command name; the plugin may parse them, or use `inputs` (see Typed inputs). Resolve relative paths in `args` against `cwd` (the user's directory), not the plugin's own. |
| `unprovision` | `{}`                                          | `{}`                  | Release what `provision` acquired. **Must be idempotent** and must work in a fresh process (see Recovery). |
| `shutdown`    | `{}`                                          | `{}`                  | Plugin should exit after replying. Host kills it after a timeout. |

## Host → plugin notifications

| Method   | Params | Notes |
|----------|--------|-------|
| `cancel` | `{}`   | User pressed Ctrl-C. Abort the running `invoke` (or provider `call`) and reply to it with error code `-32800`. |
| `call_event` | `{capability, data}` | A streamed event from a provider, during a `call` this plugin made. |
| `http_event` | `{id, status, headers}` or `{id, chunk}` | The streamed answer to an `http` request this plugin made (see HTTP through the host). `id` is that request's id. |

## Plugin → host notifications

| Method     | Params                       | Host behaviour |
|------------|------------------------------|----------------|
| `output`   | `{text}`                     | Written to stdout as-is (include your own `\n`; partial text allows streaming). |
| `log`      | `{level, message}`           | `debug` / `info` / `warn` / `error`, written to stderr. |
| `progress` | `{percent, message, done?, total?, speed?, name?}` | Progress bar on stderr. `percent` 0–100, or `null` if unknown. The optional fields describe a transfer: `done` (integer bytes so far), `total` (integer bytes, or `null` if unknown), `speed` (bytes per second) and `name` (what is being transferred). A host that does not know them ignores them, so a plugin may always send them. |

## Plugin → host requests

| Method        | Params     | Result                           |
|---------------|------------|----------------------------------|
| `prompt_user` | `{prompt}` | `{text}` or `{eof: true}` when input has ended |
| `call`        | `{capability, method, params}` | whatever the provider returns (see Capabilities) |
| `http`        | `{method, url, headers?, body?}` | `{status, bytes}`, after `http_event` notifications (see HTTP through the host) |

## HTTP through the host

A plugin of any runner (native, container, WASM) can ask the host to make an HTTP request for it, to the
hosts it declares in `plugin.toml`:

```toml
[capabilities]
http = ["api.weather.example", "*.tiles.example", "http://localhost:8080", { host = "intranet.example", private = true }]
```

Each entry is a host name (`api.example.com`), a wildcard `*.example.com` (any subdomain at any depth,
not `example.com` itself), or an IP literal (`10.0.0.5`, `[::1]`), optionally with `:port`; without a port
only the scheme's default port matches. Plain `http` is refused unless the entry is written with an
`http://` prefix. An entry is a string, or a table `{ host = "...", private = true }`. Paths, other
schemes, `*`, single-label wildcards like `*.com`, empty strings and unknown table keys fail at install,
naming the entry. `dock plugin info` lists the entries. `http` is independent of `network`; a WASM plugin
declares `http` and still may not declare `network`.

Request (plugin to host), at most one in flight per plugin:

```json
{"jsonrpc":"2.0","id":7,"method":"http","params":{"method":"GET","url":"https://api.weather.example/v1/now?city=Oslo","headers":{"accept":"application/json"}}}
```

`method` is one of `GET`, `HEAD`, `POST`, `PUT`, `PATCH`, `DELETE`. `headers` is an object of string values
(`host`, `content-length`, `transfer-encoding`, `connection` and a few other hop-by-hop headers are set by
the host and ignored). `body` is base64 text. The host sends nothing the plugin did not set: no cookies,
credentials or environment. The answer streams back as notifications, then the result:

```json
{"jsonrpc":"2.0","method":"http_event","params":{"id":7,"status":200,"headers":[["content-type","application/json"]]}}
{"jsonrpc":"2.0","method":"http_event","params":{"id":7,"chunk":"eyJ0ZW1wIjo0fQ=="}}
{"jsonrpc":"2.0","id":7,"result":{"status":200,"bytes":12}}
```

One head event (header names in lower case), then `chunk` events of at most 64 KiB of raw body each
(base64), then the result. HEAD, 204 and 304 responses have no chunks. Any status is a result, not an
error. The notifications of a request always arrive before its reply, and the host keeps serving the
other plugins while a body streams.

What the host checks, and answers with error `-32001` (and the reason in `message`) when it fails; nothing
is sent on the network for a refused request, and the plugin gets no response from a refused redirect hop:

- the scheme is `https`, or `http` where the entry says `http://`; the URL has no userinfo;
- the host and port match an entry (case-insensitive, trailing dot ignored, IDNs compared in punycode); when
  several entries match, the most specific wins: an exact host before a wildcard, a longer wildcard before a
  shorter one, an entry with a port before one without, then the first listed;
- the host does not resolve to a private, loopback, link-local, site-local, reserved, documentation or multicast
  address (an IPv4-mapped, 6to4 or NAT64 IPv6 address counts as its IPv4 address; only global unicast `2000::/3`
  IPv6 addresses pass; the verdict is the same on every Python version), unless the matching entry is `localhost`, an IP
  literal, or has `private = true`;
- the host connects to the address it checked, and does not look the name up a second time;
- every redirect hop (at most 5) passes the same checks; `authorization`, `cookie` and
  `proxy-authorization` set by the plugin are dropped when a redirect changes the origin; 303, and 301/302
  for POST, continue as GET without a body;
- the request body is at most 16 MiB and the headers at most 64 KiB in total;
- the response body is at most 256 MiB (`[http] max_response_bytes` in `config.toml`; an unknown key in
  `[http]` fails like one in `[isolation]`), and 30 seconds with no bytes received ends the request with
  "timed out"; so does a request that takes longer than `[http] total_timeout` seconds (default 600) from the
  start, connect, headers, body and redirects included;
- TLS certificates are verified against the system trust store, with no opt-out; a failure is `-32001`.

Small reads of a body are sent together (at most one `chunk` event per 50 ms unless a full 64 KiB is ready), so a
server that sends a few bytes at a time does not cost one event per byte.

A second `http` request while one is in flight gets `-32001`. A plugin that declares no `http` entries
gets `-32001` for every request. On `cancel` (Ctrl-C) the host closes the connection and replies to the
`http` request with `-32800`; when the plugin exits, it closes the connection too. A plugin must keep reading
its stdin while an `http` request is in flight: the host queues what it sends, and the relay waits (it never
blocks the host) while the plugin does not read, until `cancel`, the total timeout, or the plugin's exit.

Each request is one line in the plugin's log, `http <METHOD> <scheme>://<host>[:port]<path> -> <status>` or
`-> refused: <reason>`, with no query string, headers or body. Proxy environment variables are not used:
connections go straight to the checked address.

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
  `llm` capability is documented in `plugin/llm/main.py`).
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
  A `cancel` that arrives while the plugin has nothing in flight (it only
  concerns an operation that already finished, or one that ran in another
  plugin) must be ignored and must not affect later `invoke`s or `call`s. A
  plugin that has not answered its in-flight request a few seconds after
  `cancel` (or after a second Ctrl-C) is killed by the host and started afresh
  on next use.
- **Cycles:** a call that would re-enter a capability already being called is
  rejected.

## Error codes

| Code     | Meaning |
|----------|---------|
| `-32601` | Unknown method or command |
| `-32602` | Bad arguments |
| `-32001` | Refused by the host: an `http` request that is not allowed, or that hit a limit (`message` says why; it is shown to the plugin, not to the user) |
| `-32800` | Cancelled |
| `-32000` | Any other plugin error (`message` is shown to the user) |

An error `code` that a plugin sends is passed on to whoever was waiting for it, for every kind of request: a
provider that answers a `call` with `-32800` makes the host answer the calling plugin's `call` with `-32800`
too. A `code` that is not an integer is replaced by `-32000`.

## Lifecycle

```
spawn → initialize → [provision] → invoke* → [unprovision] → shutdown → (kill on timeout)
```

`invoke` may repeat any number of times, sequentially, between
`initialize`/`provision` and `shutdown` (the interactive `dock shell` keeps
plugins running between operations). A plugin must handle every `invoke` in
the same process and must not leak per-invoke state into the next one, unless
it documents otherwise.

## Recovery

Before `provision`, the host writes `$DOCK_HOME/state/<id>.provisioned`. It
deletes the marker after `unprovision` succeeds. If dock or the plugin is
killed hard, the marker survives; on the next run the host starts the plugin,
calls `initialize` then `unprovision`, and removes the marker. So `unprovision`
must be able to clean up using only what is on disk in `data_dir`.

The host also force-kills a plugin that ignores `cancel` past the grace period.
That plugin's `unprovision` is skipped and its marker stays. The next
`provision` (the host rewrites the marker first) or the next run's recovery
must therefore clean up whatever the killed process left behind, so
`provision` should tolerate leftovers of an earlier, unreleased provision.

## Versioning

`api_version` in `plugin.toml` must match the host's. Changes inside version 1
are additive only (new optional params, new methods). Breaking changes bump the
version, and the host may support several versions side by side.
