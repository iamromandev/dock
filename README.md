# dock

A tiny CLI host where every feature is an out-of-process, any-language plugin that runs in a container.

## Repository structure

```
.
├─ .moon/            moon workspace: every apps/* and plugins/* folder with a moon.yml is a project
├─ .prototools       tool versions (moon, uv); `proto install` installs them
├─ makefile          delegates to moon (no logic of its own)
├─ apps/
│  ├─ cli/           the Python host (moon project `cli`)
│     ├─ src/        the host package (python -m src, or the `dock` command)
│     ├─ tests/      the host tests (pytest), plus the host-driven dm tests (they start containers)
│     └─ moon.yml    install, check, check-ci, test, run, clean
│  ├─ server/        a localhost WebSocket host for plugins (moon project `server`; see apps/server/docs/SERVER_PROTOCOL.md)
│  └─ cross/         a Kotlin Multiplatform desktop client for the server (moon project `cross`; see apps/cross/README.md)
├─ packages/
│  └─ host/          the host core the CLI and the server share (moon project `host`)
├─ plugins/          container plugins, usable by any host
│  ├─ dm/            the downloader (moon project `dm`)
│  │  ├─ tests/      dm's in-process tests (pytest, no container runtime) and the torrent fixtures
│  │  └─ moon.yml    install, check, check-ci, test, clean
│  ├─ brain/       plans a flow from a request with a local model (moon project `brain`)
│  ├─ story/       writes a text story into a Markdown file with a local Ollama model (moon project `story`)
│  ├─ narrate/     reads a text or Markdown file aloud into a WAV with a local Kokoro server (moon project `narrate`)
│  │  └─ server/   the host-side Kokoro (mlx-audio) server the plugin calls; Apple Silicon only
│  ├─ extract/     unpacks an archive safely into a new folder (moon project `extract`)
│  └─ transcribe/  turns speech in an audio or video file into text and subtitles with a local Whisper server (moon project `transcribe`)
│     └─ server/   the host-side Whisper (mlx-whisper) server the plugin calls; Apple Silicon only
└─ protocol/         PROTOCOL.md, the plugin protocol every host and plugin follows
```

## Prerequisites

- [proto](https://moonrepo.dev/proto), then `proto install` in the repository: it installs moon and uv at the
  versions in `.prototools`. uv installs Python 3.15.2 for the CLI
- Docker or Podman, running (every plugin runs in a container; the tests need one too)

## Make targets

Run from the repository root. Each one runs a moon task (`moon run <project>:<task>` does the same).

| Target               | What it does                                                       |
|----------------------|--------------------------------------------------------------------|
| `make cli-sync`      | Install dependencies (`uv sync`)                                   |
| `make cli-install`   | Put `dock` on your PATH (editable uv tool install of this checkout; `git pull` upgrades it) |
| `make cli-uninstall` | Remove that `dock` command                                         |
| `make cli-check`     | Lint and typecheck the CLI (ruff with `--fix`, ty) |
| `make cli-check-ci`  | The same without fixing anything                                   |
| `make cli-test`      | Run the tests (needs Docker or podman)                             |
| `make cli-run`       | Run dock, e.g. `make cli-run ARGS="plugin list"`                   |
| `make cli-clean`     | Remove the CLI's Python caches                         |
| `make dm-install`    | Install dm's dev tools (`uv sync`)                                 |
| `make dm-check`      | Lint and typecheck dm (ruff with `--fix`, ty)                      |
| `make dm-check-ci`   | The same without fixing anything                                   |
| `make dm-test`       | Run dm's in-process tests (no Docker or podman needed)             |
| `make dm-clean`      | Remove dm's Python caches                                          |
| `make brain-install`, `brain-check`, `brain-check-ci`, `brain-test`, `brain-clean` | The same for the brain (its tests need no Docker or model) |
| `make story-install`, `story-check`, `story-check-ci`, `story-test`, `story-clean` | The same for the story plugin (its tests need no Docker or model) |
| `make narrate-install`, `narrate-check`, `narrate-check-ci`, `narrate-test`, `narrate-clean` | The same for the narrate plugin (its tests need no Docker or model) |
| `make extract-install`, `extract-check`, `extract-check-ci`, `extract-test`, `extract-clean` | The same for the extract plugin (its tests need no Docker) |
| `make transcribe-install`, `transcribe-check`, `transcribe-check-ci`, `transcribe-test`, `transcribe-clean` | The same for the transcribe plugin (its tests need no Docker, model or ffmpeg) |
| `make transcribe-server` | Start the host-side Whisper server the transcribe plugin calls (Apple Silicon only) |
| `make install-hooks` | Install pre-commit and its git hook (runs `cli:check` on staged `apps/cli/**/*.py`, `dm:check` on staged `plugins/dm/**/*.py`); once per clone |
| `make install`, `check`, `check-ci`, `test`, `clean` | The same task in every project (`install` runs `cli:sync` for the CLI) |
| `make help`          | List the targets                                                   |

## Flows

A flow runs several plugins' commands in order. Write it as TOML:

```toml
[params.url]
type = "string"      # string, int or bool
required = true

[[steps]]
plugin = "dev.dock.dm"
command = "dm"
inputs = { url = "{params.url}", dir = "{work}" }
label = "download"

[[steps]]
plugin = "dev.dock.extract"
command = "extract"
inputs = { file = "{steps.1.files[0]}" }
```

- `dock flow check <file>` checks it and shows what it would do, starting nothing.
- `dock flow run [--yes] [--keep] <file> [name=value...]` shows the steps, asks `Run this flow? [y/N/s]` (`s` saves
  it as `<name>.flow.toml` in the current folder first), then runs them. `--yes` skips the question.
- References in input values: `{params.x}` a parameter, `{work}` the flow's private work folder (every step may write
  there), `{steps.N.field}` and `{steps.N.field[i]}` an output of an earlier step (1-based).
- At most 16 steps; the first failing step stops the flow. The work folder (under `$DOCK_HOME/flows`) is removed after
  a successful run unless you pass `--keep`; after a failure it is kept and its path is printed.

## dock do

`dock do "<request>"` asks a local model to plan a flow from your request, shows it, and runs it after you approve
(`y`, `N`, or `s` to save it as `plan.flow.toml`), the same way as `dock flow run`. Flags: `--yes`, `--keep`. A request that starts with `-` goes after `--`: `dock do -- "-5 degrees"`.

- Install the brain once: `dock plugin install plugins/brain`.
- It needs a local [Ollama](https://ollama.com) with a model pulled (default `qwen2.5:7b`).
- `DOCK_BRAIN_URL` sets the endpoint (default `http://host.docker.internal:11434`) and `DOCK_BRAIN_MODEL` the
  model. Both are read from your environment.
- On Linux, or with podman, `host.docker.internal` may not resolve inside the container: set `DOCK_BRAIN_URL` to an
  address that does (for example the host's LAN address).
- The brain sees only the catalog of your installed plugins (ids, descriptions, commands, inputs), never your files.

## Docs

- [apps/cli/README.md](apps/cli/README.md): usage, shell, plugins, configuration, `dm`
- [protocol/PROTOCOL.md](protocol/PROTOCOL.md): the plugin protocol
