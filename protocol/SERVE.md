# dock serve protocol: version 1

`dock serve` runs dock as a host for another program. The client starts `dock serve` as a child process and talks to
it over stdin and stdout. It opens no socket and reads nothing from the terminal. This protocol is versioned on its
own (`SERVE_VERSION` in `cli/__init__.py`); it is not the plugin protocol (see [PROTOCOL.md](PROTOCOL.md)), and the
plugin `api_version` is unchanged.

## Framing

The same as the plugin protocol: JSON-RPC 2.0, one message per line, UTF-8.

- stdout holds only protocol lines. dock's own warnings and the logs of its recovery step go to stderr.
- Client requests have client-chosen ids (an integer or a string). dock's requests to the client use the string ids
  `"p<n>"`.
- A line over 1 MB (1 048 576 bytes) is refused with `-32700` (`id: null`), and the connection carries on. So is a line
  that is not JSON. A JSON value that is not an object is refused with `-32600`.
- Every string the client sends is data. dock never runs it through a shell.

## Messages

| Direction | Method | Params | Result / notes |
|---|---|---|---|
| client to dock | `hello` | `{client: {name, version}}` | `{serve_version: "1", dock_version}`. Must come first: any other request before it is answered with `-32600` `"hello first"`. |
| client to dock | `commands` | `{}` | `{commands: [{name, plugin, summary, inputs: [{name, type, required, flag?, short?}]}]}`. `inputs` is `[]` for a command that declares none. |
| client to dock | `run` | `{command, args: [string], cwd}` | The plugin's whole `invoke` result: `{exit_code, ...extra}`. See Errors. |
| client to dock | `cancel` (notification) | `{id}` | `id` is the `run` request's id. An unknown id is ignored. |
| client to dock | `shutdown` | `{}` | `{}`, sent after every running call is cancelled and its plugin stopped. dock then exits 0. |
| dock to client | `progress` (notification) | `{call, percent, message, done?, total?, speed?, name?}` | `call` is the `run` request's id. The optional fields are the plugin's (see PROTOCOL.md). |
| dock to client | `log` (notification) | `{call, level, message}` | The plugin's logs, and dock's own warnings about that call. `debug` is sent only when dock runs with `-v`. |
| dock to client | `output` (notification) | `{call, text}` | What the plugin would print on stdout. |
| dock to client | `prompt` (request, id `"p<n>"`) | `{call, prompt}` | The client replies `{text}` or `{eof: true}`. An error reply counts as `{eof: true}`. |

## `run`

- `command` is a command name as on the CLI (`download`, or `dev.dock.download:download` to pick a plugin).
- `args` are the raw CLI arguments after the command name. dock parses them exactly as the CLI does (typed inputs,
  required inputs, defaults), so the same mistakes are refused, before any plugin starts.
- `cwd` is an absolute path to a folder that exists. It is the folder a plugin with `fs:write:cwd` may write to, natively
  and under isolation.
- `inputs` is reserved. In version 1 a request that has it is refused with `-32602`.
- A `run` id must be unique among the calls that are running. A duplicate is answered with `-32600` (the call that is
  already running is not affected).
- Calls run at the same time. Each one has its own plugin process, so cancelling, killing or crashing one never changes
  how another ends. A finished call's process may serve a later call with the same plugin and `cwd`; it is stopped after
  `[serve] idle_timeout` seconds of rest (default 300, `0` = never), and when dock exits.
- While a call runs, its plugin's `output`, `log` and `progress` arrive as notifications with `call` set.
- A plugin's `prompt_user` never reads dock's stdin. dock sends a `prompt` request with the call id, and passes the
  answer back to the plugin. When the call is cancelled while a prompt is pending, the plugin gets `{eof: true}`.

### Errors

| Code | Meaning |
|---|---|
| `-32700` | The line is too long or not JSON. |
| `-32600` | Not a request object, `hello` not sent first, or a `run` id that is already running. |
| `-32601` | Unknown method, or unknown (or ambiguous) command. |
| `-32602` | Bad params: the CLI's usage error text (without the `dock: ` prefix), a `cwd` that is not an absolute existing folder, or `inputs` given. |
| `-32800` | The call was cancelled. |
| `-32000` and others | The plugin's own error, with its code and message; or dock could not start the plugin (for example isolation refused the `cwd`). |

## Cancel and shutdown

- `cancel {id}` stops that call only. The plugin gets `cancel` first and is force-killed after `DOCK_CANCEL_GRACE`
  seconds (default 5); the call is answered with `-32800` by then, within about a second more.
- `shutdown`, or end of stdin without it, cancels every call, stops every plugin process (unprovisioning the ones that
  were provisioned) and exits 0. `shutdown` is answered with `{}` once the cleanup is done; on end of stdin there is
  no reply.
- Plugins that a SIGKILLed `dock serve` left provisioned are cleaned at the next start, as for the CLI.
- `dock serve` ignores SIGINT (a Ctrl-C meant for its client does not kill it); SIGTERM ends it like end of stdin.
