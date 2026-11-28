# dock server protocol, version 1

The server listens on `ws://127.0.0.1:<port>` (default port 7464; loopback only). Every WebSocket text message is one
JSON object with a `"type"`. Server settings: `DOCK_SERVER_TOKEN` (or `[server] token` in `$DOCK_HOME/config.toml`),
`--port`, `--max-runs`, `--in`, `--out`.

## Handshake

The first client message must be `hello`. Anything else, or a wrong token, gets `error` (`auth`) and the server closes
the connection. The `hello` must arrive within 10 seconds. A browser page from another origin is refused at the
WebSocket upgrade (HTTP 403): only no `Origin` header, `null`, or `http(s)://localhost` / `127.0.0.1` (any port) pass.
`version` must be the integer 1. An unsupported version gets `error` (`version`), whose message lists the supported versions
(`[1]`), and the connection closes.

| Client sends | Fields |
|---|---|
| `hello` | `version` (int), `token` (string) |

| Server answers | Fields |
|---|---|
| `welcome` | `version` (1), `server` (server version string) |

## Client messages (after `welcome`)

| Type | Fields | Meaning |
|---|---|---|
| `list` | `id` | List installed plugins and their commands. |
| `run` | `id`, `plugin`, `command`, `inputs` | Run `plugin:command` with `inputs` (an object, or omitted for a command that declares none). One run at a time per connection. |
| `answer` | `ask`, `result` | Answer an `ask` frame: `ask` is the id from that frame, `result` an object (not an object: `bad_message`). An answer for an unknown or finished ask is ignored (logged on the server, no error frame). |
| `cancel` | `run` | Cancel the run with that id if it is active; it ends with `done` code 130. No-op when idle or for another run id (no error). |

## Server messages

| Type | Fields | Meaning |
|---|---|---|
| `welcome` | `version`, `server` | Handshake accepted. |
| `plugins` | `id`, `plugins: [{id, commands: [name, ...]}]` | Reply to `list`. |
| `ui` | `run`, `method`, `params` | A plugin's output for a run. `method` is one of `output`, `log`, `bar`, `table`, `panel`, `clear`, `warn`; `params` are the plugin's own (for `warn`: `{"message": ...}`). |
| `ask` | `run`, `ask`, `method`, `params` | The plugin needs input (`prompt_user`, `form`); `ask` is a number, unique within the connection. Reply with `answer`; the run waits for it. After `cancel` or a disconnect the plugin gets `{"eof": true}` instead, and an ask not yet sent by then is never sent. |
| `done` | `run`, `code`, `error?` | The run ended. `code` is the plugin's exit code, 1 when the plugin failed (then `error` says why), 130 when cancelled. |
| `error` | `id?`, `code`, `message` | A request failed. `id` is the request's id when it had one. |

## Error codes

| Code | When |
|---|---|
| `auth` | Missing or wrong token, or the first message was not `hello`. The connection closes. |
| `version` | `hello` asked for an unsupported version. The connection closes. |
| `unknown_plugin` | `run` named a plugin or command that is not installed. |
| `bad_inputs` | `run` inputs are not an object, or do not fit the command's declared inputs (missing required, wrong type, unknown key, any input for a command that declares none). Declared defaults are filled in. |
| `busy` | `run` while this connection already has an active run. |
| `capacity` | The server is already running `--max-runs` runs (a slot is taken when the run starts). |
| `bad_message` | Not JSON, not an object, unknown `type`, or missing fields. The connection stays open. |
| `internal` | The server failed while handling a message. Details stay in the server log; the connection stays open. |

## Disconnect

When the client disconnects, the server cancels the active run (as `cancel` would), releases any `ask` it waits on
(the plugin gets `{"eof": true}`), waits up to 10 seconds for it to stop, then stops its containers and frees its slot.
Frames the run still sends to the closed socket are dropped.

Error text sent to clients (`done.error`, `error.message`) has host folders replaced by `<dock>`.
