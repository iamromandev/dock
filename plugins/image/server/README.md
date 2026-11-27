# dock image server

Host-side server that wraps [mflux](https://github.com/filipstrand/mflux) (Z-Image-Turbo 4-bit on Apple MLX). MLX needs
Metal, so this cannot run in a container; the image plugin (in a container) calls it at
`http://host.docker.internal:7860`.

## Start

```sh
uv run --project plugins/image/server python plugins/image/server/serve.py
```

Binds `127.0.0.1` only; port from `DOCK_IMAGE_PORT` (default 7860). Apple Silicon only (the project
resolves for `darwin`/`arm64` only). The model (`filipstrand/Z-Image-Turbo-mflux-4bit`, not gated, no HF login needed) downloads from Hugging Face on the first request, about 5.5 GB.

Tests (no mflux or weights needed): `uv run --project plugins/image/server pytest`

## API

- `POST /generate` body `{"prompt","width","height","steps"|null,"seed"|null,"model":"z-image-turbo"|omitted}`.
  200 `application/x-ndjson`: `{"step":n,"steps":N}` lines, then `{"png":"<base64>","seed":S}`, or
  `{"error":str}`. Bad JSON or fields: 400 `{"error":...}`. `steps` null: 9 (max 50); any other model: 400;
  `seed` null: random, returned. A failed write (client gone) abandons the render.
- `GET /health` gives `{"ok":true,"models":["z-image-turbo"]}`.

## Spike findings (mflux 0.24.0, checked 2026-11)

- Python: `requires-python >=3.11`, no upper bound. `uv run --python 3.15.2 --with mflux==0.24.0` installs
  and imports on 3.15.2 (also 3.13) on macOS arm64. Resolution for a universal lock fails on linux
  (`mlx[cuda13]` has no cp315 wheel), hence `tool.uv.environments` limits it to darwin/arm64.
- API: `from mflux.models.z_image.variants.z_image import ZImage`;
  `from mflux.models.common.config import ModelConfig`.
  `ZImage(model_config=ModelConfig.z_image_turbo(), model_path="filipstrand/Z-Image-Turbo-mflux-4bit")` loads the
  pre-quantized 4-bit weights from that repo id (no `quantize` needed; the CLI does the same via `--model-path`).
  `model.generate_image(seed, prompt, num_inference_steps=9, height=1024, width=1024)` returns a
  `GeneratedImage`; `.image` is a PIL image. Guidance is forced to 0.0 (turbo is guidance-distilled).
- Progress: same registry as FLUX: `model.callbacks.register(obj)` with `obj.call_in_loop(self, t, seed, prompt,
  latents, config, time_steps)` once per step (`t` 0-based; `config.num_inference_steps` is the total).
  The registry is per model, so the server removes its callback after each render.
- Model: not gated, so no `hf auth login` or `HF_TOKEN`; about 5.5 GB on the first download.
- Verified: a real render through `generate()` at 256x256, 2 steps, `HF_HUB_OFFLINE=1`, gave a valid PNG and
  step callbacks (1, 2) and (2, 2), about 10 s on a warm cache.
