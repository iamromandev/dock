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
```

## Try it

Needs Python 3.11+, Go 1.22+ (to build the download plugin) and Node 18+ (for chat).

```sh
make build        # build plugins for this machine
make install      # install the 4 example plugins into ~/.dock
python3 -m cli help
python3 -m cli hello Roman
python3 -m cli count 30
python3 -m cli chat                                   # built-in toy model
DOCK_LLM_BASE_URL=http://localhost:11434/v1 \
DOCK_LLM_MODEL=llama3.2 python3 -m cli chat           # real model via Ollama
python3 -m cli download https://example.com/ -o page.html
make test         # end-to-end tests in a throwaway DOCK_HOME
```

`pip install -e .` gives you a real `dock` command. Set `DOCK_HOME` to use
another plugin folder.

### LLM backends

The llm plugin talks to any OpenAI-compatible server. Without settings it uses
a small built-in toy model, so chat works out of the box.

| Variable | Example |
|---|---|
| `DOCK_LLM_BASE_URL` | `http://localhost:11434/v1` (Ollama), `http://localhost:1234/v1` (LM Studio), `https://api.openai.com/v1` |
| `DOCK_LLM_MODEL` | `llama3.2`, `gpt-4o-mini`; required with a base URL |
| `DOCK_LLM_API_KEY` | only if the server needs one |

## What the example demonstrates

| Idea | Where |
|---|---|
| Language independence | Same protocol from Node, Go and Python, no shared code |
| Machine independence | `plugins/download/plugin.toml` lists 5 OS/arch builds; `make dist` builds them; the host picks one |
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

2. A program that answers `initialize`, `invoke` and `shutdown` on stdio
   (follow `plugins/hello/main.py` or `plugins/chat/index.js`, in any language).
3. `dock plugins install ./thing`

## Not in this example yet

Remote registry and `dock plugins install <name>`, checksums and signatures,
permission enforcement (permissions are only declared and shown), warm plugin
processes (the llm plugin reloads its model on every `dock chat`). See `protocol/PROTOCOL.md` for the parts
that are fixed and the versioning rule for adding them.
