# dock

A tiny CLI host where every feature is a plugin. A plugin is any program, in
any language, that speaks the [dock protocol](protocol/PROTOCOL.md):
JSON-RPC 2.0 over stdin/stdout, one message per line. Every plugin runs in a
container.

### Behaviour change: dock runs plugins only in a container

dock used to run a plugin as a native process, a WebAssembly module or (opt-in) a container. Now every plugin runs
with `<runtime> run -i --rm` in a container, and nothing else. Removed:

- native runs (`[run].any` and the per-platform `[run]` entries) and WASM runs (`[run].wasm`, the `wasmtime` runner);
- HTTP through dock (`[capabilities].http`, the `http` request, `[http]` in `config.toml`);
- the `network:listen` permission and `requires_permission`;
- capabilities and providers (`provides`, `requires`, the `call` request, `[providers]` in `config.toml`);
- provision and unprovision, with their crash recovery (`[lifecycle].provision`, the `state/` markers);
- `ended_by_cancel` and the progress transfer fields (`done`, `total`, `speed`, `name`, `phase`, `uploaded`, `peers`);
  progress is `{percent, message}`;
- the bundled `chat`, `llm`, `download` and `image` plugins. Only `hello` is left.

A plugin whose `[run]` is not exactly `container = { ... }`, or that uses a removed key, fails at install with a
message that names the problem. A `config.toml` with an `[isolation]`, `[wasm]`, `[http]` or `[providers]` table fails
too, and a `config.toml` that does not parse is always fatal. `[isolation]` became `[container]`:

```toml
[container]
runtime = "docker"      # a command name or an absolute path; without it dock uses docker, then podman
```

`enabled` and `plugins` are gone: every plugin is always in a container.

Leftovers in `~/.dock` are not read any more. `~/.dock/state` is unused. A `~/.dock/wasm` folder from WASM installs is
read-only; delete it with `chmod -R u+w ~/.dock/wasm && rm -rf ~/.dock/wasm`.

```
cli/ (Python host)
 ├─ main.py       parse argv, built-ins, forward everything else
 ├─ store.py      ~/.dock: install, index, resolve command -> plugin
 ├─ manifest.py   plugin.toml: commands, permissions, how to build and run the container
 ├─ isolation.py  the container runner: image build, `run` argv, cwd checks, cleanup of dead containers
 ├─ lifecycle.py  session: start plugins, invoke, cancel, stop
 ├─ process.py    container process + JSON-RPC client, one shared inbox per session
 ├─ shell.py      the interactive dock> prompt
 └─ ui.py         the host owns the terminal: output, logs, progress, prompts
        │  stdio (through `<runtime> run -i`)
   ├─ hello (Python)   2 commands, prompts, progress
   └─ dm (Python)      download a URL or a .torrent into the current folder
```

## Try it

Needs Python 3.11+ and Docker or Podman (running).

```sh
make install      # install the example plugin into ~/.dock (the image is built on first use)
python3 -m cli help
python3 -m cli hello Roman
python3 -m cli hello "Ada Lovelace"                   # quote a name of several words
python3 -m cli count 30
make test         # tests in throwaway DOCK_HOMEs; they need Docker (or podman)
```

`pip install -e .` gives you a real `dock` command. Set `DOCK_HOME` to use
another plugin folder.

With no container runtime, a command that starts a plugin fails with `dock: no container runtime found (install
docker or podman, or set [container] runtime in <config>)` (exit 1). `help`, `version`, `plugin list` and
`plugin info` still work (`plugin info` shows `runtime: none found ...`).

`make test` needs a running runtime and stops at once with one line if there is none. `DOCK_TEST_RUNTIME` overrides
the runtime the tests use. Test modules that start no plugin (such as the unit classes in `tests/test_inputs.py`)
run without one. The first run builds a few small fixture images, which can take minutes; later runs reuse them.
On macOS the test folders must be in a path the Docker VM shares (`/Users` by default); if mounts come up empty, set
`TMPDIR` to a folder under `/Users`.

### Interactive shell

`python3 -m cli shell` (or bare `dock` on a terminal) opens a `dock>` prompt.
Each line is an operation, split with shell-style quoting: `hello "Ada Lovelace"`.
Plugins stay running between operations.
Built-ins: `help`, `version`, `plugin ...`, `search ...`, `status`, `exit`/`quit` (or Ctrl-D).
`status` lists the running plugins with their idle time and the container runtime they run in.
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
code is printed as `exit <n>`. Every plugin is stopped on exit.
`dock shell` also works without a terminal: lines are read from stdin, so
`printf 'hello Bob\nstatus\n' | python3 -m cli shell` runs them in one session.

Behaviour change: bare `dock` on a terminal now opens the shell instead of
printing help (`dock help` still prints it; piped stdin still prints help).

Behaviour change: `hello` and `count` declare typed inputs, so
dock checks their arguments before starting them (usage errors exit 2). A name of
several words must be quoted (`hello "Ada Lovelace"`; `hello Ada Lovelace` is an
error), and `count abc` is dock's "must be an integer" error. dock reads anything
starting with `-` as a flag, so a negative number goes after `--` (`count -- -3`).

### Find plugins before installing

dock can read the `plugin.toml` of plugins that are not installed, from local catalog folders you list in
`~/.dock/config.toml`. It only reads the manifests: nothing is built, started or copied.

```toml
[catalog]
paths = ["~/projects/github/dock/plugin", "/opt/dock-plugins"]
```

Each entry is an absolute path or starts with `~` (a relative path is an error, as is any other key in `[catalog]`).
An entry that holds a `plugin.toml` is one plugin; otherwise each immediate subfolder with a `plugin.toml` is one
plugin (deeper folders are not searched). A missing entry, or a plugin whose manifest does not load, is skipped with
a warning. Without `[catalog]` the catalog is empty.

A command may list `keywords` in its `[[commands]]` entry (a list of non-empty strings) so people can find it:

```toml
[[commands]]
name = "hello"
summary = "Greet someone: hello [name]"
keywords = ["greet", "welcome"]
```

`dock search <word>...` (also in the shell) lists the commands of installed and catalog plugins for which every word
is a case-insensitive part of the command's name, summary or one of its keywords. It reads commands only, not plugin
ids or descriptions. One line per command, sorted by name then plugin id, with the plugin id and version and either
`installed` (plus `catalog has <version>` when the catalog holds another version) or `not installed:` and the command
that installs it. A command that only a catalog copy of an installed plugin has is listed as
`not in installed <version>:` and the command that installs the catalog copy. Exit 0 with a match, 1 with none, 2 without a word.

When a command is not installed but a catalog plugin has it, the "unknown command" error names that plugin and how to
install it (`dev.dock.hello 0.2.0 in the catalog has it (installed: 0.1.0)` when that plugin is installed at another
version); nothing is installed or started. `dock plugin install <id>` installs a catalog plugin by its id (an
existing folder is still a path; an id found in several catalog folders must be installed by path), and
`dock plugin info <id|path>` shows a plugin's manifest, with its keywords and `installed: no (<folder>)` (or
`installed: yes, <version> (<path>)` when that plugin id is installed), without installing it. Running a command of an installed plugin never reads the catalog.

### Update plugins from the catalog

`dock plugin update [--dry-run] [--yes] [<id>...]` (also in the shell) compares installed plugins with the catalog and
installs the newer ones. Without ids it checks every installed plugin; with ids, only those. It needs `[catalog]`
paths in `config.toml`.

Versions must be dotted numbers (`0.10.0`, `1.2`); they are compared part by part as integers and trailing zeros are
ignored (`1.0` equals `1.0.0`). A plugin is updated only when the catalog version is strictly newer. When several
catalog folders hold the same id, the highest version wins; if two or more folders tie for the highest, that plugin
is an error naming each folder and nothing changes. A catalog copy whose version is not dotted numbers is ignored
with a warning, and so is an installed plugin with such a version (an error when you named it).

An update installs the catalog copy like `dock plugin install <folder>`, then removes the old version's folder
under `plugin/<id>/` and, best effort, its `dock/<id>:<old>` image. A copy that cannot be installed leaves the old
version in place. In the shell, a running copy of the plugin is stopped just before it is updated.

`--dry-run` (before or after the ids) prints `would update <id> <old> → <new>` and changes nothing. Output is
`updated <id> <old> → <new>` per plugin, `all plugins up to date` when nothing was newer, and `<id> <version> is up to
date` for a named current plugin. A named id that is not installed or not in the catalog is an error. Plugins are
processed in id order and one failure does not stop the others. Exit 0 when all went well, 1 when any failed, 2 for an
unknown option.

### Permission prompts

A plugin's `[capabilities]` can ask for `permissions` (only `network` and `fs:write:cwd` exist; any other entry, or a
value that is not a list of strings, makes the manifest invalid and the install fail) and `env` (host variables passed into
its container). `dock plugin install [--yes] <path|id>` and `dock plugin update` (also in the shell) ask before they
grant them. The request is listed on stderr, in this order: `network`, `fs:write:cwd`, then
`env <NAME>` per variable:

```
dev.example.tool 0.1.0 asks for: network, env MY_API_KEY
Install it? [y/N]
```

`y` or `yes` (any case) approves; anything else, an empty answer or end of input declines (`not installed`, exit 1;
an update prints `skipped <id>: new permissions not approved`, keeps the old version and is not a failure). Ctrl-C
cancels. Nothing is written to `~/.dock` and no container runtime is called before approval; the plugin folder is copied once
into a private temporary folder, and the request shown and the files installed both come from that copy, so changing
the source while the question is open cannot add grants. A plugin that asks for nothing
installs without a question.

When the plugin is already installed, only grants it did not have are asked, as `<id> <old> → <new> adds: <grants>`;
an update asks per plugin right before it updates it, and dropped grants need no approval. `update --dry-run` shows
`(adds: <grants>)` on the `would update` lines and never asks.

`-y` or `--yes` (any position) approves without asking; the request is still printed. Without a terminal (stdin or stderr is not
a TTY, which includes a piped `dock shell`) dock never reads stdin: without `--yes` it prints
`dock: <request>; rerun with --yes to approve` and installs nothing for that plugin (exit 1).

**Behaviour change:** scripts and CI that install a plugin with permissions or env without a terminal now need
`--yes`. Installs of plugins that ask for nothing (such as `hello`) are unchanged.

### dm

`dm` is a bundled plugin (`dev.dock.dm`) that downloads one http(s) URL, or the content of a `.torrent` file, into the current folder:

```sh
python3 -m cli plugin install plugin/dm        # by path, or `python3 -m cli plugin install dev.dock.dm` from the catalog
python3 -m cli dm https://example.com/file.tar.gz
python3 -m cli dm https://example.com/file.tar.gz -o name.tar.gz     # a file name, or a path in an existing subfolder
python3 -m cli dm https://example.com/file.tar.gz -d downloads       # an existing subfolder; the name comes from the URL
python3 -m cli dm ubuntu.torrent                                     # a .torrent file in the current folder
python3 -m cli dm dataset.torrent -d downloads --stall-seconds 60    # same -o and -d rules; give up sooner
```

(After `pip install -e .`, `dock dm ...` works the same.) The install prompt shows `network, fs:write:cwd`: `network` to fetch the URL, `fs:write:cwd` to save the file in
the folder you run it from.

- It never overwrites: an existing `name.ext` gives `name (1).ext`, `name (2).ext`, and so on.
- The download goes to a hidden `.name.<hex>.dm-part` file first and is moved into place only when complete, so a
  partial file is never visible; on a failure or Ctrl-C it is removed.
- URL credentials, the query and the fragment are never printed.
- `-o` and `-d` cannot leave the current folder, and `-o` with `-d` together is a usage error (exit 2).
- Like every plugin with `fs:write:cwd`, it will not run in your home folder itself (dock's cwd rule).

#### Torrents

An argument that ends in `.torrent` (any case) and names an existing file inside the current folder is a torrent;
`dm https://example.com/x.torrent` still downloads that file over HTTP. A `.torrent` outside the current folder
is not visible to `dm` (only the current folder is mounted in its container), so it is treated like any other argument
and fails as an invalid URL.

```
$ python3 -m cli dm ubuntu.torrent
saved ubuntu-24.04.iso (2.6 GB, 1 file)
$ python3 -m cli dm dataset.torrent -d downloads
saved downloads/dataset (1) (310.5 MB, 42 files)      # downloads/dataset already existed
```

- **Supported:** v1 torrents with one file or many, from HTTP(S) and UDP trackers (`announce` and `announce-list`).
  Not supported yet: magnet links, DHT, peer exchange, resume, v2-only torrents, choosing files, and `.torrent` files
  given as a URL.
- **One file or a folder:** a single-file torrent saves one file; a multi-file torrent saves a folder with the
  torrent's name and its files in it, empty files included. The `(1)`, `(2)` naming of an existing name applies to
  both, and `-o` names the file or the folder. Paths in the torrent that would leave the folder (`..`, absolute
  paths, a file that is also a folder) are refused, so nothing is written outside the target.
- **Every piece is checked** against the torrent's hashes before it is stored. A peer that sends a bad piece or data
  nobody asked for is dropped and not contacted again. As for HTTP, the work happens in a hidden `.dm-part` file or
  folder that is moved into place only when complete, and removed on a failure or Ctrl-C.
- **Outbound only, nothing listens.** `dm` connects to the peers the tracker gives it and never opens a port, so
  swarms where most peers cannot accept connections can be slow, or have no reachable peers at all.
- **It uploads while it runs.** To the peers it has connected to, `dm` serves the pieces it has verified (four
  unchoke slots plus one rotating every 10 seconds) until the download ends. Peers and trackers therefore see your
  IP address. There is no seeding after the download completes: every connection closes.
- **Stalls:** with no verified piece for 120 seconds and no connected peer it fails with `no peers found`; with
  peers connected but still nothing for 300 seconds, `stalled: no progress for 5 minutes`. `--stall-seconds N`
  (at least 1) sets the first time; the second is 2.5 times it.
- **Errors** are one line, prefixed with the torrent name, for example `dm: data.torrent: no peers found`,
  `tracker error: <reason>`, `invalid torrent: unsafe path`, `invalid torrent: conflicting paths`, or
  `cannot write to <folder>: <reason>`. Exit code 1, as for a failed HTTP download.
- **Privacy:** tracker URLs are never printed, and neither are peer addresses (except with `-v`). A login in a
  tracker URL (`user:pass@host`) is stripped and not sent, so trackers that require one will not work.
- **Private and loopback addresses:** trackers and peers on private, link-local or loopback addresses are contacted
  like any other BitTorrent client does; a torrent can therefore make `dm` open connections to hosts on your network.
- **Limits:** a torrent file over 10 MB, more than 100,000 files or about a million pieces, or very deep paths are refused; memory
  stays bounded (a 512 MiB download peaks well under 200 MB).

### How plugins run

Every plugin runs in a container, started as `<runtime> run -i --rm`, so the stdio protocol and warm plugins in
the shell work as for any child process. It gets only what the declared permissions allow:

| Restriction | Detail |
|-------------|--------|
| Network | `--network none` unless the plugin declares `network` |
| Files | read-only root, writable `/tmp`; the plugin's data dir is mounted read-write at the same path; your current directory is mounted read-write only with `fs:write:cwd` (see below) |
| Environment | only `LANG`, `TZ` (if set), `HOME=/tmp`, `DOCK_PLUGIN_ID`, `DOCK_API_VERSION`, and the variables the plugin lists in `[capabilities] env` (see below); nothing else from your environment, not even `PATH` (the image's own is used) |
| Privileges | `--cap-drop ALL`, `no-new-privileges`, run as your uid:gid |
| Images | `--pull never`: a run never downloads anything; only `dock plugin build` and the first-use build do |

dock refuses to start a plugin that declares `fs:write:cwd` when the current directory is the filesystem root, your
home directory, any folder above your home directory, or the dock home (`~/.dock`) or a folder inside or above it.
Paths are compared the way the filesystem does (case-insensitive on macOS). A data dir or a mounted current
directory whose path contains `:`, `,` or `"` is refused too: such a path cannot be written in a `--mount` option.

The runtime is `docker` from `PATH`, then `podman`, or the one you set in `~/.dock/config.toml`:

```toml
[container]
runtime = "podman"      # a command name looked up on PATH, or an absolute path
```

A relative path such as `./docker` is refused (a plugin folder could ship its own). A configured runtime that is
missing is an error: dock never falls back to another one. An unknown key or a misspelt table name (`[containr]`) is
an error too, and so is a `config.toml` that does not parse. `config.toml` takes `[container]`, `[catalog]
paths` (see "Find plugins before installing"), and `[shell] idle_timeout`; `[isolation]`, `[wasm]`, `[http]`, `[providers]` and `[serve]` are errors with a
message that says what replaced them.

A plugin that needs more of your environment, such as an API key, lists the variable names in its manifest:

```toml
[capabilities]
permissions = ["network"]
env = ["MY_API_KEY", "MY_API_URL"]
```

Each listed name that is set on the host reaches the container; one that is not
set is left out. Values are never put on the runtime's command line (dock passes
a bare `-e NAME` and the runtime reads the value from its own environment), so
they do not show up in the process list. Names must look like `MY_VAR` and may
not be ones dock sets itself (`HOME`, `PATH`, `LANG`, `TZ`, `DOCK_PLUGIN_ID`,
`DOCK_API_VERSION`). `dock plugin info <id>` shows the names on an `env:` line (`-` when there are none) and a
`runtime:` line such as `docker (no network, no cwd access)`.

**Images.** The image `dock/<id>:<version>` is built from the installed plugin folder on first use (dock prints
`dock: building image ...`, and the build log goes to `~/.dock/logs/<id>.log`) and reused afterwards.
`dock plugin build <id>` (re)builds it on demand, for example after editing the Dockerfile without a version bump.
Reinstalling a plugin, or `dock plugin remove`, drops its image, so the next run rebuilds it from the new files. A
first install into a `DOCK_HOME` that has no such plugin does not drop anything, so if you edit a plugin without
bumping its version and install it into a fresh home, an image from an earlier install can still be used: run
`dock plugin build <id>`. When dock cannot ask a runtime to drop an old image, it warns that the image may be stale.

Dock removes a plugin's container whenever it stops or kills the plugin, and at
startup removes containers left by dock processes that died (labelled
`dev.dock.owner=<hostname>:<pid>`; only those of this machine whose pid is gone,
never on Windows). That cleanup is best effort, not a guarantee: if the runtime
is unreachable at that moment, a container can remain until a later start.
The startup sweep gives the runtime at most 5 seconds.

Things to know:

- Image builds run with the runtime's default network access and without any
  of the restrictions above, and run the Dockerfile's instructions as written;
  only install plugins whose Dockerfile you trust. Base images (such as `python:3.12-slim`)
  are not pinned to a digest.
- The build context is the whole installed plugin folder, so anything in it is sent to
  the runtime; give your plugin a `.dockerignore` for what the image does not need.
- On SELinux hosts, bind mounts need the `:z` or `:Z` option, which dock does not
  add, so the plugin cannot read its mounts there.
- On macOS the runtime's VM shares the files: Docker Desktop shares `/Users` by default, otherwise
  the plugin sees an empty directory.
- A new plugin version builds a new image and old `dock/*` images of other
  versions are not deleted (`docker image rm` them).

## What the example demonstrates

| Idea | Where |
|---|---|
| Language independence | Same protocol for any language; the plugin is whatever its image runs |
| Lazy discovery | `dock help` reads `index.json`; no plugin is started |
| Namespacing and collisions | ids are `vendor.name`; on a clash use `dock dev.dock.hello:hello` |
| Ctrl-C | Host sends `cancel`, then force-kills the container after a grace period |
| Host owns the UI | Plugins never print; they send `output` / `log` / `progress` / `prompt_user` |
| Isolation | A plugin crash is an error message, not a host crash; stderr goes to `~/.dock/logs/`; the container only sees what its permissions allow |

## Write a plugin

1. A folder with `plugin.toml`, a `Dockerfile` that puts the plugin at `/plugin`, and a `.dockerignore`:

   ```toml
   id = "you.thing"
   version = "0.1.0"
   api_version = "1"
   [run]
   container = { cmd = "python3", args = ["/plugin/main.py"] }  # dockerfile = "Dockerfile" by default
   [[commands]]
   name = "thing"
   summary = "Does the thing"
   ```

   ```dockerfile
   FROM python:3.12-slim
   COPY . /plugin
   ```

   `[run]` must hold exactly `container`; any other key fails the install. The image `dock/<id>:<version>` is
   built from the Dockerfile, so `id` and `version` may only hold letters, digits, `.`, `_` and `-`.
   Permissions are `network` and `fs:write:cwd`, under `[capabilities] permissions`; both are enforced by the
   container (see "How plugins run").

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
   (follow `plugin/hello/main.py`, in any language).
3. `dock plugin install ./thing`. The image is built on the first run, or with `dock plugin build you.thing`.
4. A container sees only a fixed environment. To receive host variables, list their
   names: `[capabilities] env = ["MY_API_KEY"]` (see "How plugins run").

## Not in this example yet

Remote registry and `dock plugin install <name>`, checksums and signatures. See
`protocol/PROTOCOL.md` for the parts that are fixed and the versioning rule for adding them.
