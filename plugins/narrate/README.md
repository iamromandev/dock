# dev.dock.narrate

Reads a text or Markdown file aloud and saves it as a new WAV file in the current folder. The speech comes from a local Kokoro server that runs on the host (see [server/README.md](server/README.md)); the plugin itself is Python standard library only.

## Use

```
dock narrate notes.md [--voice af_heart|af_bella|am_michael|am_fenrir] [--speed 0.5-2.0] [-o name.wav]
```

- `file` (required): a `.md` or `.txt` file (any case), resolved against the in folder (an absolute path stays absolute) and read only from inside it (symlinks that leave it are refused). At most 1,000,000 bytes, UTF-8.
- Markdown is reduced to speech: code blocks, tables, images, HTML tags, URLs and horizontal rules are dropped; link text, inline code words and headings are kept (a heading gets a final period if it has no punctuation). `.txt` is read as is. Nothing to read, or more than 20,000 characters after that, is an error.
- `--voice`: `af_heart` (default), `af_bella`, `am_michael`, `am_fenrir`.
- `--speed`: 0.5 to 2.0, default 1.0.
- `--output` / `-o`: a plain file name ending in `.wav`. Default `narrate-YYYYmmdd-HHMMSS.wav`. An existing file is never replaced. Saving needs a filesystem with hard-link support (`os.link`).

The text is split into chunks of at most 400 characters at sentence boundaries, sent to the server one by one, and the 24 kHz mono 16-bit audio is joined into one WAV. Progress is shown as a bar; the first request can take about 40 s while the server loads the model.

Result: `{"exit_code": 0, "outputs": {"files": [path], "seconds": n}}`.

Errors: wrong input types are JSON-RPC errors (`-32602`). A bad file, voice, speed or output name, an unreachable or failing server (its `{"error"}` is shown, a 503 busy included) and an unsupported answer (a sample rate other than 24000, broken audio) are logged as errors and the result is `{"exit_code": 1}`. A cancel, or closing stdin, during a run stops before the next chunk, aborts the request in flight, leaves no file and answers `-32800`.

## Setup

Start the server on the host (Apple Silicon only):

```
uv run --project plugins/narrate/server python plugins/narrate/server/serve.py
```

| Env var | Default |
|---|---|
| `DOCK_NARRATE_URL` | `http://host.docker.internal:7861` |
| `DOCK_NARRATE_MODEL` | `kokoro` (the only value) |

Permissions: `network`, `fs:read:in`, `fs:write:cwd`.

## Develop

`make narrate-test`, `make narrate-check-ci` (tests use a local stand-in server; no Docker, GPU or model needed). The server has its own tests: `uv run --project plugins/narrate/server pytest`.
