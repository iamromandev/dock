# dock transcribe server

Host-side server that wraps [mlx-whisper](https://github.com/ml-explore/mlx-examples/tree/main/whisper)
(`mlx-community/whisper-large-v3-turbo` on Apple MLX). MLX needs Metal, so this cannot run in a container; the
transcribe plugin (in a container) decodes media with ffmpeg and posts PCM windows to it at
`http://host.docker.internal:7862`.

## Start

```sh
uv run --project plugins/transcribe/server python plugins/transcribe/server/serve.py
```

Binds `127.0.0.1` only; port from `DOCK_TRANSCRIBE_PORT` (default 7862). Apple Silicon only (the project resolves
for `darwin`/`arm64` only). The model (about 1.6 GB, not gated) downloads from Hugging Face on the first request.

Tests (no mlx or weights needed): `uv run --project plugins/transcribe/server pytest`

## API

- `POST /transcribe[?language=xx]` body: raw int16 little-endian mono PCM at 16000 Hz, at most 23,040,000 bytes
  (12 minutes; the plugin sends windows of at most 610 s). `Content-Type: application/octet-stream` (parameters
  allowed), else 415. `language` is optional, one of Whisper's language codes (auto-detected when omitted); an
  unknown code, a repeated `language` or any other query key is 400.
  200 `{"language":"en","segments":[{"start":0.0,"end":2.5,"text":"..."}]}`; times are seconds from the start of the
  window, rounded to milliseconds. `language` is the forced one if given, else the detected one.
  Empty or odd-length body, bad `Content-Length`: 400; body over the limit: 413; transcription failure: 500
  `{"error":"<Type>: msg"}` (at most 500 chars, traceback on stderr). One transcription at a time: one running plus
  one waiting request; any more get 503 `{"error":"busy: a request is already waiting"}`.
- `GET /health` gives `{"ok":true,"model":"whisper-large-v3-turbo"}`.
- Every route rejects a `Host` other than `127.0.0.1`, `localhost` or `host.docker.internal` (with or without a
  port; missing too) with 403 `{"error":"forbidden host"}`. Unknown paths get 404, other methods 405 (HEAD too,
  with `Content-Length: 0`); a stalled client is dropped after 30 s. All error bodies are `{"error":...}`.

## Silence filtering

Whisper hallucinates text on silence, and `no_speech_prob` does not catch it: on 3 s of silence it returned
`{"start":0.0,"end":29.98,"text":" Thank you.","no_speech_prob":9e-11}`. The server therefore clamps every segment
to `[0, window length]` and drops a segment when it ends at or before its start after clamping, has
`no_speech_prob` above 0.6, has empty text, or its own audio slice has an RMS below 0.003 (float scale).

Limits of the filter:

- Steady background noise can still produce a short invented word: a probe on 3 s of noise at RMS 0.01 gave
  "You".
- Segments with `no_speech_prob` above 0.6 are dropped, which can drop real speech in a 30 s window that opens
  with a long pause.
- Quiet speech is kept down to about RMS 0.003 (about -50 dBFS); anything quieter is treated as silence.
