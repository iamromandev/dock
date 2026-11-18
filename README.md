# dock

A tiny CLI host where every feature is a plugin. A plugin is any program, in
any language, that speaks the [dock protocol](protocol/PROTOCOL.md):
JSON-RPC 2.0 over stdin/stdout, one message per line.

```
cli/ (Python host)
 ├─ main.py       parse argv, built-ins, forward everything else
 ├─ store.py      ~/.dock: install, index, resolve command -> plugin
 ├─ manifest.py   plugin.toml + pick the build for this OS/arch
 ├─ lifecycle.py  session: start plugins + their providers, relay calls, clean up, recover
 ├─ process.py    child process + JSON-RPC client, one shared inbox per session
 └─ ui.py         the host owns the terminal: output, logs, progress, prompts
        │  stdio
   ┌────┴──────────────┬─────────────────┬──────────────────┐
 chat (Node)  ─call─▶  llm (Python)      download (Go)      hello (Python)
 conversation, via     provides "llm";   5 native builds,   2 commands,
 history, UI   host    toy or OpenAI-    progress, cancel   prompts, progress
                       compatible server
                       provision/unprovision
                                         image (Rust → WASM)
                                         resize, crop, rotate, flip,
                                         convert, compress; wasmtime
```

## Try it

Needs Python 3.11+, Go 1.22+ (to build the download plugin) and Node 18+ (for chat).
On recent macOS use a current Go release: macOS 26's loader refuses binaries
without an `LC_UUID` load command, which Go 1.22 builds lack.
The optional image plugin needs Rust stable with the `wasm32-wasip1` target to build and
[`wasmtime`](https://wasmtime.dev) to run; without that target `make build` skips it.

```sh
make build        # build plugins for this machine
make install      # install the example plugins into ~/.dock
python3 -m cli help
python3 -m cli hello Roman
python3 -m cli hello "Ada Lovelace"                   # quote a name of several words
python3 -m cli count 30
python3 -m cli chat                                   # built-in toy model
DOCK_LLM_BASE_URL=http://localhost:11434/v1 \
DOCK_LLM_MODEL=llama3.2 python3 -m cli chat           # real model via Ollama
python3 -m cli download https://example.com/ -o page.html
python3 -m cli image resize photo.jpg 50%             # also crop, rotate, flip, convert, compress, edit
make test         # end-to-end tests in a throwaway DOCK_HOME
```

The `image` plugin works on PNG, JPEG, WebP and GIF files in the current folder only (not the
home folder itself) and writes `photo.edited.jpg` unless you give `-o`. WebP output is lossless
(so `--quality` is for JPEG only), an animated GIF is read as its first frame, and metadata is
dropped. See [plugin/image/README.md](plugin/image/README.md).

`pip install -e .` gives you a real `dock` command. Set `DOCK_HOME` to use
another plugin folder.

### Interactive shell

`python3 -m cli shell` (or bare `dock` on a terminal) opens a `dock>` prompt.
Each line is an operation, split with shell-style quoting: `hello "Ada Lovelace"`.
Plugins stay running between operations, so `llm` provisions once per shell.
Built-ins: `help`, `version`, `plugin ...`, `status`, `exit`/`quit` (or Ctrl-D).
`status` lists the running plugins with their idle time and what they provide,
`isolated: <runtime>` for those running in a container, and `wasm` for those running as WebAssembly.
Tab completes commands, `plugin` subcommands and installed plugin ids.
History is kept in `~/.dock/history` (last 1000 lines); set `DOCK_NO_READLINE=1`
to run without readline (no completion or history).
A plugin idle longer than `idle_timeout` seconds (default 300) is stopped
before each prompt and again when a line is entered (so a long wait at the
prompt counts), and restarts on demand; set it in `~/.dock/config.toml`:

```toml
[shell]
idle_timeout = 300   # seconds; 0 = never stop idle plugins
```

Ctrl-C cancels the running operation and returns to the prompt. A non-zero exit
code is printed as `exit <n>`. Everything is unprovisioned and stopped on exit.
`dock shell` also works without a terminal: lines are read from stdin, so
`printf 'hello Bob\nstatus\n' | python3 -m cli shell` runs them in one session.

Behaviour change: bare `dock` on a terminal now opens the shell instead of
printing help (`dock help` still prints it; piped stdin still prints help).

Behaviour change: `hello`, `count`, `download` and `chat` declare typed inputs, so
dock checks their arguments before starting them (usage errors exit 2). A name of
several words must be quoted (`hello "Ada Lovelace"`; `hello Ada Lovelace` is an
error), `count abc` is dock's "must be an integer" error, `download` needs a URL,
and `chat` takes no arguments. dock reads anything starting with `-` as a flag, so a
negative number goes after `--` (`count -- -3`).

### Container isolation (opt-in)

By default a plugin is a native child process with your full environment,
filesystem and network; its `permissions` are only shown. To run plugins in a
container instead, add this to `~/.dock/config.toml`:

```toml
[isolation]
runtime = "docker"      # or "podman", "kiri", or an absolute path
enabled = true          # every plugin; or list some: plugins = ["dev.dock.hello"]
```

A plugin takes part when it has a `Dockerfile` and `[run].container` in its
`plugin.toml` (the bundled `hello` (which also serves `count`), `download`, `chat` and `llm` do; `download` compiles its Go
binary inside the image, so isolated use needs no Go on the host). With isolation on, a plugin without
them is refused (`dock: <id>: isolation is on but the plugin has no container
build ...`), never run unisolated. The image `dock/<id>:<version>` is built
from the installed folder on first use (the build log goes to
`~/.dock/logs/<id>.log`) and reused afterwards. Reinstalling a plugin removes
its image (through the configured runtime, if an `[isolation]` table names
one), so the next run rebuilds it from the new files.

The container is started with `<runtime> run -i --rm`, so the stdio protocol,
warm plugins in the shell and capability calls work unchanged. It gets only
what the declared permissions allow:

| Restriction | Detail |
|-------------|--------|
| Network | `--network none` unless the plugin declares `network` |
| Files | read-only root, writable `/tmp`; the plugin's data dir is mounted read-write; your current directory is mounted read-write only with `fs:write:cwd`, and then never when it is `/`, your home directory, or the dock home (`~/.dock`) or a directory above or inside it: dock refuses to start the plugin there |
| Environment | only `LANG`, `TZ` (if set), `HOME=/tmp`, `DOCK_PLUGIN_ID`, `DOCK_API_VERSION`, and the variables the plugin lists in `[capabilities] env` (see below); nothing else from your environment, not even `PATH` (the image's own is used) |
| Privileges | `--cap-drop ALL`, `no-new-privileges`, run as your uid:gid |
| Images | `--pull never`: a run never downloads anything; only `dock plugin build` and the first-use build do |

A plugin that needs more of your environment, such as an API key, lists the
variable names in its manifest; the bundled `llm` does:

```toml
[capabilities]
provides = ["llm"]
permissions = ["network"]
env = ["DOCK_LLM_BASE_URL", "DOCK_LLM_MODEL", "DOCK_LLM_API_KEY"]
```

Each listed name that is set on the host reaches the container; one that is not
set is left out. Values are never put on the runtime's command line (dock passes
a bare `-e NAME` and the runtime reads the value from its own environment), so
they do not show up in the process list. Names must look like `MY_VAR` and may
not be ones dock sets itself (`HOME`, `PATH`, `LANG`, `TZ`, `DOCK_PLUGIN_ID`,
`DOCK_API_VERSION`). Native plugins ignore `env` and keep your whole environment.
`dock plugin info <id>` shows the names on an `env:` line (`-` when there are none).

`dock plugin build <id>` (re)builds the image of an installed plugin on demand,
whether or not isolation is on (for example after editing its Dockerfile without
a version bump). `dock plugin info <id>` shows an `isolation:` line: `off`,
`docker (no network, no cwd access)` / `docker (network, cwd)` and so on, or
`on, but not supported by this plugin`. A plugin that declares `[run].container`
is installed with its build files (such as `main.go` and `go.mod`); other plugins
keep leaving out `*.go` and `go.mod`.

Dock removes a plugin's container whenever it stops or kills the plugin, and at
startup removes containers left by dock processes that died (labelled
`dev.dock.owner=<hostname>:<pid>`; only those of this machine whose pid is gone,
never on Windows). That cleanup is best effort, not a guarantee: if the runtime
is unreachable at that moment, a container can remain until a later start.
The startup sweep also runs without an `[isolation]` table when `docker` is on
`PATH`, and gives the runtime at most 5 seconds.

Things to know:

- A broken `config.toml` never runs plugins natively when it may hold your
  isolation settings: an unknown key in `[isolation]`, a misspelt table
  (`[isolaton]`), isolation keys outside the table, or a damaged section that is
  or looks like `[isolation]` all stop plugins from starting, with a message.
  A damage elsewhere in the file (say a broken `[shell`) does not, and a valid
  `[isolation]` section is still applied. Entries in `plugins` that are not
  installed plugin ids are reported once on stderr.
- Image builds run with the runtime's default network access and without any
  of the restrictions above, and run the Dockerfile's instructions as written;
  only install plugins whose Dockerfile you trust. Base images (`python:3.12-slim`,
  `golang:1-alpine`, `alpine`, `node:22-alpine`) are not pinned to a digest.
- The build context is the whole installed plugin folder, so anything in it
  (including a native `bin/`) is sent to the runtime; the bundled plugins
  ship a `.dockerignore` that leaves `bin/` out, and your own plugins
  should do the same.
- Paths containing `:` (your current directory with `fs:write:cwd`, or `~/.dock`)
  are not supported: they cannot be written in a `-v` mount.
- On SELinux hosts, bind mounts need the `:z` or `:Z` option, which dock does not
  add, so the plugin cannot read its mounts there.
- A new plugin version builds a new image and old `dock/*` images of other
  versions are not deleted (`docker image rm` them).
- On macOS the runtime's VM must share the mounted paths (Docker Desktop shares
  `/Users` by default), otherwise the plugin sees an empty directory.

### WASM runner

A plugin can run as a WebAssembly (WASI) module under the [`wasmtime`](https://wasmtime.dev)
command line instead of as a native process or a container. It is sandboxed by
construction and needs no Docker. Declare it in `plugin.toml` in one of two forms:

```toml
[run]
wasm = { module = "plugin.wasm" }                     # your own compiled module
# or: Python scripts on the interpreter dock pins
wasm = { python = "3.15", script = "main.py" }        # args = [...] is optional in both forms
```

A `wasm` entry wins over native and container entries and over `[isolation]`; there is
no opt-in, and dock never falls back to running the plugin natively or in a container.
Paths must stay inside the plugin folder and exist at install. The runtime is `wasmtime`
from `PATH`, or set it in `~/.dock/config.toml` (an unknown key in `[wasm]` is an error):

```toml
[wasm]
runtime = "/opt/wasmtime/wasmtime"
```

Without it, starting the plugin fails with `dock: WASM runtime 'wasmtime' not found
(install wasmtime)`. Tested with wasmtime 50.0.2. What the plugin gets:

| Grant | Detail |
|-------|--------|
| Files | its own plugin folder (read-only: dock removes the write bits when it installs it, and restores them before it removes or replaces it); its data dir read-write at the same path; your current directory read-write only with `fs:write:cwd`, never when it is `/`, your home directory, or the dock home or a directory above or inside it; for the Python form the interpreter's standard library, read-only. Nothing else on the machine is visible |
| Network | none: a plugin that declares `network` fails to install (`dock: <id>: WASM plugins cannot use 'network'`), and dock never gives wasmtime a socket option. It can still ask dock to fetch from hosts it declares (see [HTTP through dock](#http-through-dock)) |
| Environment | only `HOME` (a folder inside the data dir), `DOCK_PLUGIN_ID`, `DOCK_API_VERSION` and the names in `[capabilities] env` that are set on the host, handed over by name so values stay off the command line (`PYTHONHOME`, and with the Python form any `PYTHON*` name, cannot be declared) |

The stdio protocol, warm plugins in the shell, idle stop and the force kill after the
cancel grace work as for other plugins. A single-threaded Python plugin only sees `cancel`
between messages, so a long step is ended by the force kill. `dock plugin info <id>` shows
`runner: wasm (wasmtime)` or `runner: wasm (wasmtime, python 3.15)`.

For the Python form dock uses one pinned CPython build for WASI (3.15.2 from
`brettcannon/cpython-wasi-build`; WASI 0.1, so no threads, sockets or subprocesses),
kept in `~/.dock/wasm/python-<version>/` after its SHA-256 has been checked. The first
start of a Python-form plugin downloads it (about 15.5 MB, with a `dock: fetching python
...` line on stderr); later starts do not touch the network. A download whose hash does
not match, or that fails, installs nothing and the plugin does not start. Because installed
WASM folders are read-only, delete `~/.dock/wasm` or a plugin folder by hand only after
`chmod -R u+w` on it. dock itself checks before every start that the plugin folder and the
interpreter folder are still read-only and refuses to start the plugin if not (or when run
as root, where permission bits do not apply): reinstall the plugin, or delete
`~/.dock/wasm/python-<version>` (after `chmod -R u+w`) to fetch the interpreter again. The
interpreter's `.verified` marker records the SHA-256 it was checked against, and a folder
marked with another hash is fetched again. A download over 64 MiB is refused.

### HTTP through dock

Any plugin (native, container or WASM) can ask dock to make an HTTP request for it, to hosts its manifest
declares, without a network of its own. This is how a WASM plugin, which has no sockets, talks to an API:

```toml
[capabilities]
http = ["api.weather.example", "*.tiles.example", "http://localhost:8080", { host = "intranet.example", private = true }]
```

An entry is a host name, a wildcard `*.example.com` (any subdomain at any depth, not `example.com` itself) or an
IP literal (`[::1]` in brackets for IPv6), optionally with `:port`; without a port only 443 matches (80 for an
`http://` entry). Plain `http` needs the `http://` prefix. A path, another scheme, `*`, `*.com`, an empty string
or an unknown key in a table entry fails the install, naming the entry. `dock plugin info <id>` shows an `http:`
line with the entries. The plugin sends the protocol request `http` and gets `http_event` notifications with
the body in chunks, then the result; see [protocol/PROTOCOL.md](protocol/PROTOCOL.md#http-through-the-host).

Every request and every redirect hop is checked, and refused with error `-32001` (the reason in the message,
nothing sent) when:

- the host or port is not declared, the URL has `user:password@`, or it is `http` where the entry does not
  say `http://`;
- the name resolves to a private, loopback, link-local, reserved or multicast address, unless the entry is
  `localhost`, an IP literal or `{ host = "...", private = true }`. Dock looks the name up once and connects to
  the address it checked, so a name that answers differently the second time gains nothing;
- a redirect leads anywhere that fails these checks (at most 5 redirects are followed; `authorization`,
  `cookie` and `proxy-authorization` the plugin set are dropped when a redirect changes host. Any other header
  the plugin set, such as a custom `x-api-key`, is kept and follows a redirect to any other host the plugin
  declares: declare only hosts that may see those headers);
- the request body is over 16 MiB or its headers over 64 KiB, the response is over 256 MiB, 30 seconds pass
  with no bytes received, or the whole request (connect, headers and body, redirects included) takes longer than
  10 minutes. Raise or lower the response cap and the total time in `~/.dock/config.toml` (an unknown key in
  `[http]` is an error):

```toml
[http]
max_response_bytes = 1073741824
total_timeout = 600   # seconds, connect + headers + body
```

When several entries fit a URL, the most specific one decides (an exact host before a wildcard, a longer
wildcard before a shorter one, an entry with a port before one without), whatever their order: a
`private = true` entry for `lan.example.com` is not hidden by `*.example.com` listed before it. Small pieces of a
slow body (a token stream) are sent together, at most every 50 ms, instead of one event per read.

Ctrl-C closes the connection and ends the request with `-32800`. A plugin has one request in flight at a time.
Dock sends only the headers the plugin sets: no cookies, credentials or environment. Each request is one line in
`~/.dock/logs/<id>.log` without the query string, headers or body; the URL path is logged, so do not put a
secret in a path. Certificates are checked against the system trust store, with no opt-out. Proxy environment
variables (`HTTP_PROXY` and friends) are not used: dock connects straight to the address it checked. The TLS
tests (skipped without the `openssl` command) check a local https server against a throwaway CA: a certificate
from an unknown CA, or for another name, is refused. A plugin that stops reading its input cannot freeze dock:
the request waits, and ends on Ctrl-C or at the total timeout.

### LLM backends

The llm plugin talks to any OpenAI-compatible server. Without settings it uses
a small built-in toy model, so chat works out of the box.

| Variable | Example |
|---|---|
| `DOCK_LLM_BASE_URL` | `http://localhost:11434/v1` (Ollama), `http://localhost:1234/v1` (LM Studio), `https://api.openai.com/v1` |
| `DOCK_LLM_MODEL` | `llama3.2`, `gpt-4o-mini`; required with a base URL |
| `DOCK_LLM_API_KEY` | only if the server needs one |

With [isolation](#container-isolation-opt-in) on, `llm` runs in a container that
gets these three variables (and none of your others beyond `LANG` and `TZ`), and `chat` runs in one too.
A container cannot reach a model server on your machine's `localhost`: use
`http://host.docker.internal:11434/v1` on Docker Desktop (plain Docker on Linux
does not define that name), or run `llm` natively.

## What the example demonstrates

| Idea | Where |
|---|---|
| Language independence | Same protocol from Node, Go and Python, no shared code |
| Machine independence | `plugin/download/plugin.toml` lists 5 OS/arch builds; `make dist` builds them; the host picks one |
| Lazy discovery | `dock help` reads `index.json`; no plugin is started |
| Namespacing and collisions | ids are `vendor.name`; on a clash use `dock dev.dock.chat:chat` |
| Plugins using plugins | `chat` requires the `llm` capability; the host starts the llm plugin and relays calls and streamed tokens between them |
| Swappable providers | Any plugin that provides `llm` can replace the built-in one; pick one in `~/.dock/config.toml` under `[providers]` |
| Provision / unprovision | `llm` loads its model before chat starts and releases it after |
| Ctrl-C | Host runs plugins in their own process group and sends `cancel`, then still unprovisions |
| Crash recovery | `state/<id>.provisioned` marker; next run calls `unprovision` in a fresh process |
| Host owns the UI | Plugins never print; they send `output` / `log` / `progress` / `prompt_user` |
| Isolation | A plugin crash is an error message, not a host crash; stderr goes to `~/.dock/logs/` |

## Write a plugin

1. A folder with `plugin.toml`:

   ```toml
   id = "you.thing"
   version = "0.1.0"
   api_version = "1"
   [run]
   any = { cmd = "python3", args = ["main.py"], requires = ["python3"] }
   [[commands]]
   name = "thing"
   summary = "Does the thing"
   ```

   A command can declare typed inputs; dock then parses and checks the args
   before starting the plugin and sends them as `inputs` with `invoke`
   (see `protocol/PROTOCOL.md`, "Typed inputs"):

   ```toml
   [[commands]]
   name = "fetch"
   summary = "fetch <url> [--retries n]"
   inputs = [
     { name = "url",     type = "string", required = true },
     { name = "retries", type = "int",    flag = "--retries", default = 3 },
   ]
   ```

   Types are `string`, `int` and `bool`. A `bool` is a flag that is `false`
   unless given, so it takes neither `default` nor `required`.

2. A program that answers `initialize`, `invoke` and `shutdown` on stdio
   (follow `plugin/hello/main.py` or `plugin/chat/index.js`, in any language).
3. `dock plugin install ./thing`
4. Optional, to support isolation: a `Dockerfile` that puts the plugin at
   `/plugin` (`COPY . /plugin`) and a run entry that says what to execute in it:

   ```toml
   [run]
   any       = { cmd = "python3", args = ["main.py"], requires = ["python3"] }
   container = { cmd = "python3", args = ["/plugin/main.py"] }  # dockerfile = "Dockerfile" by default
   ```

   A container sees only a fixed environment. To receive host variables, list their
   names: `[capabilities] env = ["MY_API_KEY"]` (see "Container isolation").

## Not in this example yet

Remote registry and `dock plugin install <name>`, checksums and signatures,
permission enforcement for native plugins (permissions are enforced only with container isolation). See
`protocol/PROTOCOL.md` for the parts that are fixed and the versioning rule for adding them.
