# image

Edit images from the dock CLI: resize, crop, rotate, flip, convert, compress, and chain several
edits in one go. Formats: PNG, JPEG, WebP and GIF. The plugin is Rust compiled to one WASM module
(`plugin.wasm`) that dock runs under wasmtime, with write access to the current folder only.

## Build

Needs Rust with the `wasm32-wasip1` target (`rustup target add wasm32-wasip1`) and `wasmtime` on PATH.

```sh
make build        # builds plugin/image/plugin.wasm when cargo is available
make install      # installs only plugin.toml, plugin.wasm and this README, not target/
cd plugin/image && cargo test
```

## Usage

```
usage:
  image resize   <in> <size>            [-o out] [--force]   size: W | xH | WxH | WxH! | N%
  image crop     <in> <W>x<H>+<X>+<Y>   [-o out] [--force]
  image rotate   <in> 90|180|270        [-o out] [--force]
  image flip     <in> h|v               [-o out] [--force]
  image convert  <in> <out>                      [--force]
  image compress <in> [--quality N]     [-o out] [--force]   N: 1-100, default 80
  image edit     <in> (--resize S | --crop G | --rotate D | --flip h|v)... [--quality N] -o out [--force]

example: image resize photo.jpg 50% -o small.jpg
```

Without `-o` the result is `<name>.edited.<ext>` next to the input. If the output exists, you are asked
`Overwrite <out>? [y/N]`; `--force` skips the question. `edit` applies its operations in the order given.

Exit codes: 0 done, 1 failed (missing file, unreadable image, refused overwrite, path outside the
current folder), 2 usage error.

## Notes

- Files must be inside the current folder (not the home folder itself); paths that leave it are refused.
- WebP output is lossless only, so `--quality` (and `compress`) work for JPEG output only.
- An animated GIF or WebP is read as its first frame, with a warning. GIF output is not animated.
- Metadata (EXIF and so on) is dropped and EXIF rotation is not applied.
- Transparency becomes white when writing JPEG.
- Limits: 200 MB per file and 64 megapixels per image.
