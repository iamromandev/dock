# dock

A tiny CLI host where every feature is a plugin. A plugin is any program, in
any language, that speaks the [dock protocol](protocol/PROTOCOL.md):
JSON-RPC 2.0 over stdin/stdout, one message per line.

```
cli/ (Python host)
 ├─ main.py       parse argv, built-ins, forward everything else
 ├─ store.py      ~/.dock: install, index, resolve command -> plugin
 ├─ manifest.py   plugin.toml + pick the build for this OS/arch
 ├─ lifecycle.py  initialize -> provision -> invoke -> unprovision -> shutdown, crash recovery
 ├─ process.py    child process + JSON-RPC client
 └─ ui.py         the host owns the terminal: output, logs, progress, prompts
        │  stdio
   ┌────┼───────────────┬──────────────────┐
 chat (Node)      download (Go)       hello (Python)
 provision/        5 native builds     2 commands, prompts,
 unprovision,      progress, cancel    progress
 streaming
```

## Try it

Needs Python 3.11+, Go 1.22+ (to build the download plugin) and Node 18+ (for chat).

```sh
make build        # build plugins for this machine
make install      # install the 3 example plugins into ~/.dock
python3 -m cli help
python3 -m cli hello Roman
python3 -m cli count 30
python3 -m cli chat
python3 -m cli download https://example.com/ -o page.html
make test         # end-to-end tests in a throwaway DOCK_HOME
```

`pip install -e .` gives you a real `dock` command. Set `DOCK_HOME` to use
another plugin folder.

## What the example demonstrates

| Idea | Where |
|---|---|
| Language independence | Same protocol from Node, Go and Python, no shared code |
| Machine independence | `plugins/download/plugin.toml` lists 5 OS/arch builds; `make dist` builds them; the host picks one |
| Lazy discovery | `dock help` reads `index.json`; no plugin is started |
| Namespacing and collisions | ids are `vendor.name`; on a clash use `dock dev.dock.chat:chat` |
| Provision / unprovision | `chat` loads its "model" only for the session and releases it after |
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

2. A program that answers `initialize`, `invoke` and `shutdown` on stdio
   (follow `plugins/hello/main.py` or `plugins/chat/index.js`, in any language).
3. `dock plugins install ./thing`

## Not in this example yet

Remote registry and `dock plugins install <name>`, checksums and signatures,
permission enforcement (permissions are only declared and shown), warm plugin
processes, plugin-to-plugin calls. See `protocol/PROTOCOL.md` for the parts
that are fixed and the versioning rule for adding them.
