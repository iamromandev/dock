# dock CLI

A tiny CLI host where every feature is a plugin. A plugin is any program, in
any language, that speaks the [dock protocol](../../protocol/PROTOCOL.md):
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
src/ (Python host)
 ├─ main.py       parse argv, built-ins, forward everything else
 ├─ store.py      ~/.dock: install, index, resolve command -> plugin
 ├─ manifest.py   plugin.toml: commands, permissions, how to build and run the container
 ├─ isolation.py  the container runner: image build, `run` argv, cwd checks, cleanup of dead containers
 ├─ lifecycle.py  session: start plugins, invoke, cancel, stop
 ├─ process.py    container process + JSON-RPC client, one shared inbox per session
 ├─ shell.py      the interactive dock> prompt
 ├─ editor.py     the inline shell's line editor (keys.py reads the keys)
 ├─ live.py       the live renderer: layout, in-place redraw, forms (term.py owns the terminal)
 └─ ui.py         the host owns the terminal: output, logs, bars, tables, panels, prompts, forms (plain text)
        │  stdio (through `<runtime> run -i`)
   ├─ hello (Python)   2 commands, prompts, a progress bar
   └─ dm (Python)      download a URL, a .torrent or a magnet link into the current folder
```

## Try it

Needs [uv](https://docs.astral.sh/uv/) (it installs Python 3.15.2) and Docker or Podman (running). Run the `uv`
commands from `apps/cli/`; the `moon` ones work from anywhere in the repository (from its root, the `cli-*` make
targets, such as `make cli-test`, do the same).

```sh
uv sync
moon run cli:plugin-install   # install the example plugin into ~/.dock (the image is built on first use)
uv run dock help
uv run dock hello Roman
uv run dock hello "Ada Lovelace"                   # quote a name of several words
uv run dock count 30
moon run cli:test             # tests in throwaway DOCK_HOMEs; they need Docker (or podman)
```

`uv run dock` runs the `dock` command from the project environment. Run it from `apps/cli/`: the plugins live at the
repository root, so their paths are `../../plugins/dm` from there. dock is meant to run from this checkout with uv,
not to be published or installed as a wheel. Set `DOCK_HOME` to use another plugin folder.

With no container runtime, a command that starts a plugin fails with `dock: no container runtime found (install
docker or podman, or set [container] runtime in <config>)` (exit 1). `help`, `version`, `plugin list` and
`plugin info` still work (`plugin info` shows `runtime: none found ...`).

`moon run cli:test` needs a running runtime and stops at once with one line if there is none. `DOCK_TEST_RUNTIME` overrides
the runtime the tests use. Test modules that start no plugin (such as the unit classes in `tests/test_inputs.py`)
run without one. The first run builds a few small fixture images, which can take minutes; later runs reuse them.
On macOS the test folders must be in a path the Docker VM shares (`/Users` by default); if mounts come up empty, set
`TMPDIR` to a folder under `/Users`.

### Interactive shell

`uv run dock shell` (or bare `dock` on a terminal) opens a `dock>` prompt.
Each line is an operation, split with shell-style quoting: `hello "Ada Lovelace"`.
Plugins stay running between operations.
Built-ins: `help`, `version`, `plugin ...`, `search ...`, `status`, `exit`/`quit` (or Ctrl-D).
`status` lists the running plugins with their idle time and the container runtime they run in.
Tab completes commands, `plugin` subcommands and installed plugin ids.
History is kept in `~/.dock/history` (last 1000 lines); set `DOCK_NO_READLINE=1`
to run without readline (no history saved; in the line-based fallback, used when stdin or stderr is not a
terminal or with `DOCK_PLAIN=1`, no Tab completion either).
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
`printf 'hello Bob\nstatus\n' | uv run dock shell` runs them in one session.

#### Live and plain output

dock draws in one of two ways, chosen once per process:

- **Live**: stderr is a terminal and `TERM` is set and not `dumb`. `dock` and `dock shell` open the inline shell
  (the prompt sits at the bottom, the output scrolls above it), and every command, one-shot or in the shell, shows its
  bars, tables and panels in place below the text, then leaves their final state in the scrollback.
  Forms are answered with the keys. Nothing is shown in colour only; the one colour is a bold panel title, and
  `NO_COLOR=1` turns it off.
- **Plain**: stderr is redirected or piped, `TERM` is `dumb` or unset, or `DOCK_PLAIN=1`. dock prints text only, with no
  escape codes: a bar prints one line when it is done or when the command ends, a table or panel prints once as aligned
  text, a form asks numbered questions on stderr and reads the answers from stdin. In this mode `dock` and
  `dock shell` run the line-based shell (readline, as before).

Keys at the inline prompt:

| Key | Does |
|---|---|
| Left, Right, Home, End (Ctrl-B, Ctrl-F, Ctrl-A, Ctrl-E) | move the cursor |
| Backspace, Delete (Ctrl-D) | delete before or at the cursor |
| Ctrl-K, Ctrl-U, Ctrl-W | delete to the end of the line, to the start, the word before the cursor |
| Up, Down | previous and next history entry (what you were typing is kept) |
| Tab | complete; with several matches they are listed below the prompt |
| Ctrl-C | clear the line |
| Ctrl-D on an empty line | exit |
| Ctrl-L | clear the screen |
| Ctrl-Z | suspend dock; `fg` brings it back and redraws |

While a command runs the prompt is hidden and keys are ignored, except Ctrl-C (cancel) and Ctrl-Z, and the answers
to a form. A terminal that cannot be restored is the thing to report as a bug: dock restores the settings, shows the
cursor and erases what it drew on normal exit, Ctrl-D, an error, SIGTERM, SIGHUP, SIGQUIT (Ctrl-\) and Ctrl-Z. In a background job (`dock ... &`) it leaves the
terminal's modes alone. If the live drawing ever
misbehaves in your terminal, `DOCK_PLAIN=1` turns it off. A resize redraws at the new width; when the window is made
narrower, dock climbs over the old lines as many rows as a reflowing terminal made of them. A terminal that does
not reflow may lose a line or two above the drawing.

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

`dm` is a bundled plugin (`dev.dock.dm`) that downloads one http(s) URL, the content of a `.torrent` file, or a magnet link into the current folder:

```sh
uv run dock plugin install ../../plugins/dm  # by path, or `uv run dock plugin install dev.dock.dm` from the catalog
uv run dock dm https://example.com/file.tar.gz
uv run dock dm https://example.com/file.tar.gz -o name.tar.gz     # a file name, or a path in an existing subfolder
uv run dock dm https://example.com/file.tar.gz -d downloads       # an existing subfolder; the name comes from the URL
uv run dock dm ubuntu.torrent                                     # a .torrent file in the current folder
uv run dock dm dataset.torrent -d downloads --stall-seconds 60    # same -o and -d rules; give up sooner
uv run dock dm "magnet:?xt=urn:btih:<hash>&tr=<tracker>"          # a magnet link (quote it: the shell sees `&`)
uv run dock dm https://example.com/file.tar.gz --no-resume        # throw an old partial away, start from zero
uv run dock dm pack.torrent --list                                # list a torrent's files (see Choosing files)
```

The install prompt shows `network, fs:write:cwd`: `network` to fetch the URL, `fs:write:cwd` to save the file in
the folder you run it from.

- It never overwrites: an existing `name.ext` gives `name (1).ext`, `name (2).ext`, and so on.
- The download goes to a hidden `.name.<key>.dm-part` file first and is moved into place only when complete, so a
  partial file is never visible. On Ctrl-C or a failure that may succeed on a retry it is kept and the next run
  continues it (see [Resume](#resume)); on any other failure it is removed.
- URL credentials, the query and the fragment are never printed.
- `-o` and `-d` cannot leave the current folder, and `-o` with `-d` together is a usage error (exit 2).
- Like every plugin with `fs:write:cwd`, it will not run in your home folder itself (dock's cwd rule).

#### Torrents

An argument that ends in `.torrent` (any case) and names an existing file inside the current folder is a torrent;
`dm https://example.com/x.torrent` still downloads that file over HTTP. A `.torrent` outside the current folder
is not visible to `dm` (only the current folder is mounted in its container), so it is treated like any other argument
and fails as an invalid URL.

```
$ uv run dock dm ubuntu.torrent
saved ubuntu-24.04.iso (2.6 GB, 1 file)
$ uv run dock dm dataset.torrent -d downloads
saved downloads/dataset (1) (310.5 MB, 42 files)      # downloads/dataset already existed
```

- **Supported:** v1 torrents with one file or many, from HTTP(S) and UDP trackers (`announce` and `announce-list`).
  Magnet links are covered [below](#magnet-links), and the DHT [here](#the-dht). Peer exchange is receive-only (see
  [below](#the-dht)). Not supported yet: v2-only torrents, choosing files, and `.torrent` files given as a URL.
- **One file or a folder:** a single-file torrent saves one file; a multi-file torrent saves a folder with the
  torrent's name and its files in it, empty files included. The `(1)`, `(2)` naming of an existing name applies to
  both, and `-o` names the file or the folder. Paths in the torrent that would leave the folder (`..`, absolute
  paths, a file that is also a folder) are refused, so nothing is written outside the target.
- **Every piece is checked** against the torrent's hashes before it is stored. A peer that sends a bad piece or data
  nobody asked for is dropped and not contacted again. As for HTTP, the work happens in a hidden `.dm-part` file or
  folder that is moved into place only when complete; an interrupted torrent can be resumed (see [Resume](#resume)).
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
- **The live view:** in a terminal a torrent shows a bar for each of the 8 largest unfinished files (at most 28
  files get one in a run), a total bar, a small panel (download and upload speed, peers connected of known, ETA,
  ratio) and a table of up to 10 peers with their address, client and speeds. It updates at most 5 times a second.
  Piped (`dm x.torrent 2>log`), each finished file and the total print one line, followed once by the final panel and
  peers table. A magnet link shows `fetching metadata` as a bar without a percentage until the metadata arrives.
- **Privacy:** tracker URLs are never printed, and peer addresses appear only in that peers table (on your screen, or
  in the piped stderr), never in logs or errors (except with `-v`). A login in a
  tracker URL (`user:pass@host`) is stripped and not sent, so trackers that require one will not work.
- **Private and loopback addresses:** trackers and peers from a tracker or a magnet on private, link-local or loopback
  addresses are contacted like any other BitTorrent client does (the DHT is stricter, see [below](#the-dht)); a
  torrent can therefore make `dm` open connections to hosts on your network.
- **Limits:** a torrent file over 10 MB, more than 100,000 files or about a million pieces, or very deep paths are refused; memory
  stays bounded (a 512 MiB download peaks well under 200 MB).

#### Magnet links

An argument that starts with `magnet:?` (any case) is a magnet link. `dm` fetches the torrent's metadata (its
info dictionary) from peers, checks it against the info-hash in the link, and then downloads exactly as for a
`.torrent` file: same naming (the torrent's own name, or `-o`), `-d`, piece checks, resume, serving while it runs and
stall rules. Quote the link: it contains `&`.

```
$ uv run dock dm "magnet:?xt=urn:btih:c9e15763f722f23e98a29decdfae341b98d53056&dn=Linux+ISO&tr=udp%3A%2F%2Ftracker.example%3A6969"
saved Linux ISO (2.0 GB, 1 file)
saved Linux ISO.torrent
```

- **Supported parameters:** `xt=urn:btih:` with a 40-character hex or 32-character base32 info-hash (any case);
  `tr` trackers (`http`, `https` and `udp`; other schemes are ignored) and `x.pe=host:port` peers (an IPv4 address,
  `[IPv6]` or a host name; invalid ones are ignored). `dn` is read but never used as a file name or printed. Other
  parameters are ignored. A link with no usable `xt`, with several different info-hashes, or whose only topic is a v2
  hash (`urn:btmh:`) fails with `invalid magnet link` or `v2-only magnet links are not supported`.
- **A link with only an info-hash works.** `dm` looks for peers in the [DHT](#the-dht) as well as through the link's
  trackers and `x.pe` peers. With `--no-dht`, a link with neither a tracker nor a peer fails at once, before any
  network use: `dm: magnet c9e15763…: magnet link has no trackers or peers (and the DHT is off)`.
- **Metadata fetch.** While it looks for peers the progress line reads `finding peers (DHT)` (when nothing else has
  found one yet), then `fetching metadata · N peers` with no percentage. The metadata is accepted only when its SHA-1 equals the info-hash, and the same limits as for a
  `.torrent` apply (at most 10 MiB, safe paths, file and piece limits). A peer that sends a wrong size, a wrong
  block or data nobody asked for is dropped. The stall rules apply (`no peers found`, `stalled: ...`).
- **Output and privacy.** A magnet is named in messages only as `magnet <first 8 hex digits>…`, never by the link,
  its name, tracker URLs or peer addresses.
- **The saved `.torrent`.** After a successful download `dm` also writes `<name>.torrent` next to it (with `-o x`,
  `x.torrent`), never over an existing file (`name (1).torrent`, and so on), and prints `saved <name>.torrent`.
  It holds the fetched metadata and the link's tracker URLs exactly as given, all in one tier. **A tracker URL
  can contain a passkey or another secret; it is written to that file as is.** If the file cannot be written `dm`
  warns `could not save <name>.torrent: <reason>` and still exits 0 with the download in place.
- **Resume.** The partial and its resume key are the same as for the `.torrent` of that info-hash, so a magnet
  continues a partial that `dm file.torrent` started and the other way round. Once the metadata is verified and the
  partial exists, `dm` stores the metadata in a hidden `.dm.<key>.dm-meta` file beside it; the next run reads it
  (only if its SHA-1 still equals the info-hash, otherwise it is deleted and fetched again) and asks no peer for
  metadata. `--no-resume` deletes the partial and that file. It is deleted on success, and when the partial is
  (a failure before any piece was verified); Ctrl-C keeps both.
- **Serving the metadata.** Once it has verified the metadata `dm` also hands it to peers that ask for it on its
  connections (up to 8 queued requests per peer); before that it answers such requests with a rejection.

#### The DHT

For every torrent and magnet that is not private, `dm` also looks for peers in the BitTorrent DHT (BEP 5), next to
the trackers and `x.pe` peers. This is what lets a magnet that has only an info-hash download, and it finds extra
peers for a torrent whose trackers are dead. While no peer is connected and no tracker has answered, the progress
reads `finding peers (DHT)`. It looks again every 5 minutes, or after 60 seconds when fewer than 5 peers are
connected, and stops when the download ends or is cancelled. With the DHT on, a torrent whose trackers all fail
no longer fails at once: it waits for the stall time (`--stall-seconds`) and then ends with `no peers found`.

```
$ uv run dock dm "magnet:?xt=urn:btih:c9e15763f722f23e98a29decdfae341b98d53056"      # no tracker: the DHT finds peers
$ uv run dock dm --no-dht file.torrent                                                # trackers and x.pe peers only
```

- **`--no-dht`** turns it off for that run: no DHT traffic and the node cache is neither read nor written. A magnet
  with no tracker and no peer then fails at once with `(and the DHT is off)`.
- **Private torrents** (`private` set in the torrent, BEP 27) never use the DHT, even without `--no-dht`. For a
  magnet this is known only once the metadata has arrived: the DHT lookups stop then, and the peers only they had
  found, which are not yet connected, are dropped.
- **Read-only, no port.** `dm` is a read-only DHT node (BEP 43): it only asks, with the `get_peers` query, never
  answers a node, never announces and never opens a listening port. It sends from one UDP port the system picks and
  accepts only the replies to its own queries. Work is bounded: at most 8 queries in flight, 300 per lookup and 50
  datagrams a second; replies are size-checked, parsed strictly and capped; and nodes or peers on private,
  link-local, loopback or reserved addresses are never used from the DHT.
- **Bootstrap hosts.** A lookup starts from the nodes in the cache (below) and from `router.bittorrent.com:6881`,
  `dht.transmissionbt.com:6881` and `router.utorrent.com:6881`. Their names are resolved by your system's resolver.
- **The node cache.** After a download that found peers through the DHT, `dm` keeps up to 200 DHT node addresses (and
  ids) that answered, in `~/.dock/data/dev.dock.dm/dht-nodes.dat` (6 KiB at most; `DOCK_HOME` replaces `~/.dock`).
  It holds no info-hash and no peer, and leaves out nodes close to the info-hash you looked up. A missing,
  oversized, malformed or symlinked file is ignored and replaced; a folder `dm` cannot write to is skipped silently.
  Delete the file at any time to start fresh.
- **Privacy.** The bootstrap hosts and the DHT nodes `dm` asks learn your IP address and the info-hash you are looking
  for; peers found there learn your IP address, as with any BitTorrent client. Use `--no-dht` to avoid that. Node and
  peer addresses are never written to a log, an output line or an error; peers appear only in the peers table.
- **Peer exchange (PEX, BEP 11).** `dm` also takes the peers that connected peers name in their `ut_pex` messages
  (at most 50 per message, one message per peer per minute). It never sends any. PEX stays on with `--no-dht` and is
  off for private torrents.

#### Resume

An interrupted `dm` keeps what it has, and running the same command again in the same folder continues from there.

- **What is kept, and when.** On Ctrl-C, and on failures a retry may fix: a dropped or closed connection, a timeout,
  HTTP 408, 429 and 5xx, a full disk, and for torrents `no peers found`, `stalled` and tracker errors. Everything
  else removes the partial: other HTTP 4xx (404 and so on), an invalid URL or torrent, an unwritable folder, and
  any failure before the first byte was written. For torrents, a failure that a retry may fix also removes the
  partial when no piece has been verified yet (`no peers found` on a first run leaves nothing); Ctrl-C always keeps it.
  A completed download leaves nothing behind.
- **Automatic.** Run the same command (the same URL, or the same torrent, with the same `-o` or `-d`) in the same
  folder and `dm` finds its partial by name. It prints `resuming from 1.9 GB of 4.0 GB` and fetches only the rest.
  A different URL, `-o` name or folder never touches or uses another download's partial.
- **`--no-resume`** deletes the matching partial and starts from zero.
- **HTTP.** `dm` asks for the remaining bytes with a `Range` request that is conditional on the ETag or
  Last-Modified date it saw first, and re-fetches the last 1 MiB of what it already holds (an OS crash can tear
  the end of a file). A partial of 1 MiB or less is not worth resuming and starts over. If the server has no strong ETag (a `W/` one does not count) and no Last-Modified, or does
  not know the length, the download cannot be resumed and starts over with `cannot resume: starting over`. The
  same happens when the file changed on the server or the server ignores ranges: the partial is replaced, never
  mixed with new content.
- **Torrents.** On resume `dm` re-checks every piece already on disk against the torrent's hashes
  (`checking existing data`; about 1-2 seconds per GB), counts only the pieces that pass, and asks peers for the
  rest. A partial whose files do not match the torrent (missing, extra or wrong-sized files, a symlink) is deleted
  and the download starts over. A magnet link resumes the same way and also reuses its stored metadata (see
  [Magnet links](#magnet-links)).
- **What is left in the folder.** Hidden items next to the download, named `.<name>.<key>.dm-part` (the file or
  folder), `.<name>.<key>.dm-resume` (HTTP only, a few bytes: the length and the validator, never the URL or any
  credentials) and `.<name>.<key>.dm-lock` (`.dm.<key>.dm-lock` for a torrent; only while `dm` runs, or after
  it was killed), and for a magnet `.dm.<key>.dm-meta` (the torrent's metadata, up to 10 MiB, never the link).
  Partials made by `dm` 0.2.0 have a random name and are not resumed. To delete leftovers, including after a force-kill:
  `find . -maxdepth 1 \( -name '.*.dm-part' -o -name '.*.dm-resume' -o -name '.*.dm-lock' -o -name '.*.dm-meta' -o -name '.*.dm-meta.*.tmp' \) -exec rm -r {} +`
  (works in bash and zsh; it removes every `dm` partial, record, lock and stored metadata in the current folder, so run it only
  when nothing is downloading there).
- **Sizes.** A torrent partial shows its full size at once (the files are sparse), so `ls` and `du` differ until
  it is filled.
- **One at a time.** A second identical `dm` in the same folder fails at once with
  `already being downloaded in this folder`. In `dock shell` this message can appear for a moment right after
  Ctrl-C, while the cancelled download is still letting go of its partial; run it again.

#### Choosing files

A torrent or magnet link can be listed and downloaded in part:

```sh
uv run dock dm pack.torrent --list                       # numbered files with sizes and a total; downloads nothing
uv run dock dm pack.torrent --only 1,3-4                 # file numbers and ranges (1-based, torrent order)
uv run dock dm pack.torrent --only 'sub/*.mkv,poster.png'  # globs on the path inside the item
uv run dock dm pack.torrent --only 'sub/*' --skip '*.txt'  # --skip applies after --only
uv run dock dm "magnet:?xt=urn:btih:<hash>&tr=<tracker>" --list --skip '*.nfo'   # fetches the metadata, then lists
```

- **`--list`** prints `#  size  path` for each file and `N files, <size>`. With `--only` or `--skip` it lists the
  picked files, keeping their numbers in the whole torrent. A `.torrent` is listed without any network, lock or
  partial. A magnet fetches only its metadata (or reuses the stored one) and writes nothing.
- **Picks** are comma-separated: a number (`3`), a range (`2-5`) or a glob. The path is the one inside the item, such
  as `S01E01.mkv` or `extras/poster.png`, matched in full and case-sensitively; `*` and `?` match any characters
  (also `/`), and `[,]` matches a comma. A pick that matches nothing, a number out of range, a bad range or an empty
  selection is an error before anything is created or requested.
- **What is downloaded.** Only the pieces that touch a picked file. Bytes of unpicked files inside those pieces
  are kept in a hidden `.dm-side` folder inside the partial, and are removed before the download is moved into place:
  unpicked files and folders never appear in the result. The `saved` line counts the picked files.
- **Resume.** The flags of each run decide the selection. An interrupted selected download resumes with the same
  selection, a different one or none: files already on disk move between their place and the hidden side folder, and
  verified data from an earlier, wider selection is reused. `dm` 0.7.0 partials resume with a selection too. A layout
  that is not what `dm` left (extra entries, links, wrong sizes) is replaced, as before (`cannot resume: starting over`).
- A finished download is never changed: running `dm` again with other picks makes a new numbered sibling.
- `--only`, `--skip` and `--list` are for torrents and magnets; with an http(s) URL they are an error.

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
  only install plugins whose Dockerfile you trust. Base images (such as `python:3.15.2-slim`)
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
| Host owns the UI | Plugins never print; they send `output` / `log` / `bar` / `table` / `panel` / `clear` / `prompt_user` / `form` |
| Isolation | A plugin crash is an error message, not a host crash; stderr goes to `~/.dock/logs/`; the container only sees what its permissions allow |

## Write a plugin

1. A folder with `plugin.toml`, a `Dockerfile` that puts the plugin at `/plugin`, and a `.dockerignore`:

   ```toml
   id = "you.thing"
   version = "0.1.0"
   api_version = "2"
   [run]
   container = { cmd = "python3", args = ["/plugin/main.py"] }  # dockerfile = "Dockerfile" by default
   [[commands]]
   name = "thing"
   summary = "Does the thing"
   ```

   ```dockerfile
   FROM python:3.15.2-slim
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
   (follow `plugins/hello/main.py` at the repository root, in any language).
3. `dock plugin install ./thing`. The image is built on the first run, or with `dock plugin build you.thing`.
4. A container sees only a fixed environment. To receive host variables, list their
   names: `[capabilities] env = ["MY_API_KEY"]` (see "How plugins run").

## Not in this example yet

Remote registry and `dock plugin install <name>`, checksums and signatures. See
`protocol/PROTOCOL.md` for the parts that are fixed and the versioning rule for adding them.
