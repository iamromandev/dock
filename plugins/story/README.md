# dev.dock.story

Writes a text story from a prompt with a local Ollama model and saves it as a new Markdown file in the current folder. Text only. Python standard library only.

## Use

```
dock story write "a fox who learns to share" [--audience kids|adult] [--length short|medium|long] [-o name.md]
```

- `prompt` (required, at most 2000 characters).
- `--audience`: `kids` (default; gentle, no violence or sexual content) or `adult` (mature themes, no explicit sexual content).
- `--length`: `short` (about 300 words, default), `medium` (800), `long` (1500). A story outside the allowed range is still saved, with a warning.
- `--output` / `-o`: a plain file name ending in `.md`. Default `story-YYYYMMDD-HHMMSS.md`. An existing file is never replaced. Saving needs a filesystem with hard-link support (`os.link`).

The file holds `# <title>`, a blank line, then the story. Result: `{"exit_code": 0, "outputs": {"files": [path], "words": n}}`.

Errors: an invalid prompt or wrong input types are JSON-RPC errors (`-32602`). A bad audience, length or output name is logged as an error and the result is `{"exit_code": 1}`. Closing stdin during a run cancels it.

## Setup

Run Ollama on the host and pull the model once: `ollama pull qwen2.5:7b`.

| Env var | Default |
|---|---|
| `DOCK_STORY_URL` | `http://host.docker.internal:11434` |
| `DOCK_STORY_MODEL` | `qwen2.5:7b` |

Permissions: `network`, `fs:write:cwd`.

## Develop

`make story-test`, `make story-check-ci` (tests use a local stand-in Ollama server; no Docker or model needed).
