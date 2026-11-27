# image

Container plugin `dev.dock.image`: generates a PNG from a text prompt with the host image server
(Z-Image-Turbo 4-bit on Apple MLX, which cannot run in a container) and writes it to the current folder.

## Install

```sh
dock plugin install plugins/image
```

## Start the server

On the host, see [server/README.md](server/README.md):

```sh
uv run --project plugins/image/server python plugins/image/server/serve.py
```

The model is Z-Image-Turbo 4-bit (`filipstrand/Z-Image-Turbo-mflux-4bit`). It is not gated: no Hugging Face
login or token is needed. The first request downloads about 5.5 GB. Steps: 1 to 50 (default 9).

## Use

```sh
dock image "a cat in a spacesuit" [-o name.png] [--width N] [--height N] [--steps N] [--seed N] [--model z-image-turbo]
```

Width and height are multiples of 16 from 256 to 2048 (default 512; rounded to the nearest multiple of 16). 512 px takes about 1.5-2 min on a base M1, 1024 px several minutes. `-o` must be a `.png` name in the
current folder that does not exist yet; the default is `image-YYYYmmdd-HHMMSS.png`.

## Environment

- `DOCK_IMAGE_URL`: server address (default `http://host.docker.internal:7860`).
- `DOCK_IMAGE_MODEL`: model, only `z-image-turbo` (the default; any other value is an error).

## Live check

With the server running and the brain plugin and Ollama set up, in a scratch folder:

```sh
dock do "create image for me" --yes
```

A PNG appears in the folder.

## Tests

```sh
cd plugins/image && uv run pytest && uv run ruff check && uv run ty check
```
