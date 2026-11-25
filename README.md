# dock

A tiny CLI host where every feature is an out-of-process, any-language plugin that runs in a container.

## Repository structure

```
.
├─ makefile        delegates to cli/makefile (no logic of its own)
├─ cli/            the Python host and its plugins
│  ├─ src/         the host package (python -m src, or the `dock` command)
│  ├─ tests/       the test suite (pytest) and its helpers and fixtures
│  ├─ plugins/     container plugins: hello (example), dm (downloader)
│  ├─ docs/        PROTOCOL.md, the plugin protocol
│  └─ makefile     install, check, test, run, plugin-install
└─ cross/          planned: a cross-platform client (not here yet)
```

## Prerequisites

- [uv](https://docs.astral.sh/uv/), which installs Python 3.15.2 for the project
- Docker or Podman, running (every plugin runs in a container; the tests need one too)

## Make targets

Run from the repository root. Each one calls the same target in `cli/makefile`.

| Target               | What it does                                                       |
|----------------------|--------------------------------------------------------------------|
| `make cli-install`   | Install dependencies (`uv sync`)                                   |
| `make cli-check`     | Lint and typecheck (ruff with `--fix`, ty)                         |
| `make cli-check-ci`  | The same without fixing anything                                   |
| `make cli-test`      | Run the tests (needs Docker or podman)                             |
| `make cli-run`       | Run dock, e.g. `make cli-run ARGS="plugin list"`                   |
| `make cli-plugin-install` | Install the example plugin into `$DOCK_HOME` (default `~/.dock`) |
| `make check`         | Same as `cli-check`                                                |
| `make clean`         | Remove Python caches                                               |
| `make help`          | List the targets                                                   |

## Docs

- [cli/README.md](cli/README.md): usage, shell, plugins, configuration, `dm`
- [cli/docs/PROTOCOL.md](cli/docs/PROTOCOL.md): the plugin protocol
