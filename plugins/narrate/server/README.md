# dock narrate server

Host-side server that wraps [mlx-audio](https://github.com/Blaizzy/mlx-audio) (Kokoro-82M bf16 on Apple MLX). MLX
needs Metal, so this cannot run in a container; the narrate plugin (in a container) calls it at
`http://host.docker.internal:7861`.

## Start

```sh
uv run --project plugins/narrate/server python plugins/narrate/server/serve.py
```

Binds `127.0.0.1` only; port from `DOCK_NARRATE_PORT` (default 7861). Apple Silicon only (the project resolves for
`darwin`/`arm64` only). The model (`mlx-community/Kokoro-82M-bf16`, not gated, no HF login needed) downloads from
Hugging Face on the first request. The first request also loads the pipeline and spaCy, about 40 s cold; later
requests are fast.

Tests (no mlx or weights needed): `uv run --project plugins/narrate/server pytest`

## API

- `POST /speak` body `{"text","voice","speed","model":"kokoro"|omitted}`. `text`: non-blank string, at most 1000
  characters. `voice`: one of `af_heart`, `af_bella`, `am_michael`, `am_fenrir`. `speed`: number 0.5 to 2.0.
  200 `{"audio":"<base64 int16 little-endian mono PCM>","sample_rate":24000}`. Bad JSON or fields: 400
  `{"error":...}`; body over 65536 bytes: 413; model failure: 500 `{"error":"<Type>: msg"}` (at most 500 chars,
  traceback on stderr); a model that yields no audio is also a 500. One generation at a time: the server allows one
  running plus one waiting request and answers 503 `{"error":"busy: a request is already waiting"}` to any more.
  `POST /speak` needs `Content-Type: application/json` (parameters such as `; charset=utf-8` allowed), else 415.
  Every route rejects a `Host` header other than `127.0.0.1`, `localhost` or `host.docker.internal` (with or without
  a port; missing too) with 403 `{"error":"forbidden host"}`, which blocks browser and DNS-rebinding requests. The
  plugin sends `Host: host.docker.internal:7861`. Other methods get 405; a stalled client is dropped after 30 s.
- `GET /health` gives `{"ok":true,"models":["kokoro"],"voices":[...]}`.

## Spike findings (checked 2026-11-28, Python 3.15.2 darwin/arm64)

- `mlx-audio==0.6.8` + `misaki[en]==0.9.4` work; they pull mlx 0.32.5, spacy 3.8.18, torch 2.15.0, numpy 2.5.5.
- API: `from mlx_audio.tts.utils import load_model`; `model = load_model("mlx-community/Kokoro-82M-bf16")`;
  `for r in model.generate(text=..., voice=..., speed=..., lang_code="a")`: `r.audio` is float32 mono and
  `model.sample_rate == 24000`. The server concatenates the parts, clips to [-1, 1] and scales to int16.
- Voices checked: af_heart, af_bella, am_michael, am_fenrir.
- spaCy pip-installs `en_core_web_sm` at runtime when missing, so it is pinned as a direct dependency (wheel URL in
  `pyproject.toml`).
- torch warns that `torch.jit.script` is unsupported on 3.14+; harmless.
- Timings: cold first call about 42 s (pipeline + spaCy); warm about 0.5 s per 4 s of audio.
- Live smoke (server on port 7871, af_heart, speed 1.0, one sentence, 156000 bytes = 3.25 s of audio): cold
  40.8 s, warm 0.37 s.
