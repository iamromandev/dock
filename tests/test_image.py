"""The image plugin as dock really runs it: the built plugin.wasm under a real wasmtime.

Skipped unless wasmtime is available (`DOCK_TEST_WASMTIME` or PATH, as in test_wasm_real.py) and
`plugin/image/plugin.wasm` has been built (`make build`, which needs cargo and the wasm32-wasip1 target).
"""

import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cli.store import Store  # noqa: E402

PLUGIN = ROOT / "plugin" / "image"
WASM = PLUGIN / "plugin.wasm"
MAX_WASM_BYTES = 5 * 1024 * 1024
RUN_TIMEOUT = 120


def wasmtime_path() -> str | None:
    override = os.environ.get("DOCK_TEST_WASMTIME")
    if override:
        return override if os.access(override, os.X_OK) else None
    return shutil.which("wasmtime")


WASMTIME = wasmtime_path()


def png(width: int, height: int, rgb=(200, 80, 40)) -> bytes:
    """A minimal solid-colour RGB PNG, from the standard library only."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    row = b"\x00" + bytes(rgb) * width
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(row * height)) + chunk(b"IEND", b""))


def png_size(data: bytes) -> tuple[int, int]:
    assert data.startswith(b"\x89PNG"), "not a PNG"
    return struct.unpack(">II", data[16:24])


def gif(frames: list[tuple[int, int]]) -> bytes:
    """A GIF whose frames have the given sizes; each frame's pixel data is one tiny LZW block (or cut short)."""
    out = b"GIF89a" + struct.pack("<HHBBB", 10, 10, 0x80, 0, 0) + bytes([0, 0, 0, 255, 255, 255])
    for w, h in frames:
        out += b"\x2c" + struct.pack("<HHHHB", 0, 0, w, h, 0) + b"\x02\x02\x44\x01\x00"
    return out + b"\x3b"


def png_rgba_transparent(width: int, height: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    row = b"\x00" + bytes(4) * width
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(row * height)) + chunk(b"IEND", b""))


def png_header_only(width: int, height: int, color_type: int = 2) -> bytes:
    """Just a signature and IHDR: enough to claim a size without any pixels."""
    body = b"IHDR" + struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + body + struct.pack(">I", zlib.crc32(body))


@unittest.skipUnless(WASMTIME, "wasmtime is not available (put it on PATH or set DOCK_TEST_WASMTIME)")
@unittest.skipUnless(WASM.is_file(), "plugin/image/plugin.wasm is not built (make build needs cargo)")
class ImagePlugin(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="dock-image-")).resolve()
        cls.addClassCleanup(shutil.rmtree, cls.tmp, ignore_errors=True)
        cls.store = Store(cls.tmp / "home")
        (cls.store.home / "config.toml").write_text(f'[wasm]\nruntime = "{WASMTIME}"\n')
        host_home = cls.tmp / "host-home"
        host_home.mkdir()
        cls.env = {**os.environ, "DOCK_HOME": str(cls.store.home), "PYTHONPATH": str(ROOT), "HOME": str(host_home)}
        # Install only the manifest and the module: the plugin folder also holds a large cargo target/.
        src = cls.tmp / "image-src"
        src.mkdir()
        shutil.copy(PLUGIN / "plugin.toml", src)
        shutil.copy(WASM, src)
        cls.store.install(src)

    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="work-", dir=self.tmp))

    def image(self, *args: str, stdin: str = "") -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "cli", "image", *args], input=stdin, capture_output=True, text=True,
            env=self.env, cwd=self.work, timeout=RUN_TIMEOUT, check=False,
        )

    def test_the_module_is_within_the_size_budget(self):
        self.assertLessEqual(WASM.stat().st_size, MAX_WASM_BYTES)

    def test_help_lists_the_operations_and_exits_0(self):
        r = self.image("--help")
        self.assertEqual(r.returncode, 0, r.stderr)
        for op in ("resize", "crop", "rotate", "flip", "convert", "compress", "edit"):
            self.assertIn(f"image {op}", r.stdout)

    def test_a_usage_error_exits_2_and_creates_no_file(self):
        (self.work / "a.png").write_bytes(png(4, 4))
        r = self.image("rotate", "a.png", "45")
        self.assertEqual(r.returncode, 2)
        self.assertIn("image: ", r.stderr)
        self.assertEqual([p.name for p in self.work.iterdir()], ["a.png"])

    def test_resize_halves_the_dimensions_into_the_default_name(self):
        (self.work / "a.png").write_bytes(png(40, 20))
        r = self.image("resize", "a.png", "50%")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(png_size((self.work / "a.edited.png").read_bytes()), (20, 10))
        self.assertRegex(r.stdout, r"^a\.edited\.png: 40×20 → 20×10, .+ → .+\n$")

    def test_rotate_90_swaps_the_dimensions(self):
        (self.work / "a.png").write_bytes(png(40, 20))
        r = self.image("rotate", "a.png", "90")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(png_size((self.work / "a.edited.png").read_bytes()), (20, 40))

    def test_convert_to_jpg_writes_jpeg_magic_bytes(self):
        (self.work / "a.png").write_bytes(png(16, 16))
        r = self.image("convert", "a.png", "a.jpg")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.work / "a.jpg").read_bytes().startswith(b"\xff\xd8\xff"))

    def test_an_input_outside_the_current_folder_is_refused(self):
        (self.tmp / "x.png").write_bytes(png(8, 8))
        r = self.image("resize", "../x.png", "50%")
        self.assertEqual(r.returncode, 1)
        self.assertIn("inside the current folder", r.stderr)

    def test_an_output_outside_the_current_folder_is_refused(self):
        (self.work / "a.png").write_bytes(png(8, 8))
        r = self.image("resize", "a.png", "50%", "-o", "../out.png")
        self.assertEqual(r.returncode, 1)
        self.assertIn("inside the current folder", r.stderr)
        self.assertFalse((self.tmp / "out.png").exists())

    def test_an_absolute_input_outside_the_current_folder_is_refused(self):
        outside = self.tmp / "abs.png"
        outside.write_bytes(png(8, 8))
        r = self.image("resize", str(outside), "50%")
        self.assertEqual(r.returncode, 1)
        self.assertIn("inside the current folder", r.stderr)
        self.assertEqual([p.name for p in self.work.iterdir()], [])

    def test_a_symlinked_input_pointing_outside_cannot_be_read(self):
        outside = self.tmp / "secret.png"
        outside.write_bytes(png(8, 8))
        (self.work / "link.png").symlink_to(outside)
        r = self.image("resize", "link.png", "50%")
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("inside the current folder", r.stderr)
        self.assertEqual([p.name for p in self.work.iterdir()], ["link.png"])

    def test_a_symlinked_folder_pointing_outside_cannot_be_written_through(self):
        (self.work / "a.png").write_bytes(png(8, 8))
        away = Path(tempfile.mkdtemp(prefix="away-", dir=self.tmp))
        (self.work / "link").symlink_to(away, target_is_directory=True)
        r = self.image("resize", "a.png", "50%", "-o", "link/out.png", "--force")
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("inside the current folder", r.stderr)
        self.assertEqual(list(away.iterdir()), [])

    def test_a_symlink_output_counts_as_existing_and_needs_force(self):
        (self.work / "a.png").write_bytes(png(8, 8))
        away = Path(tempfile.mkdtemp(prefix="away-", dir=self.tmp))
        (self.work / "o.png").symlink_to(away / "target.png")
        r = self.image("resize", "a.png", "50%", "-o", "o.png")
        self.assertEqual(r.returncode, 1)
        self.assertIn("exists", r.stderr)
        self.assertEqual(list(away.iterdir()), [])

    def test_the_overwrite_prompt_answered_no_keeps_the_file_and_exits_1(self):
        (self.work / "a.png").write_bytes(png(8, 8))
        (self.work / "o.png").write_bytes(b"keep")
        r = self.image("resize", "a.png", "50%", "-o", "o.png", stdin="n\n")
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertEqual((self.work / "o.png").read_bytes(), b"keep")
        self.assertIn("Overwrite o.png?", r.stdout + r.stderr)

    def test_the_overwrite_prompt_answered_yes_replaces_the_file(self):
        (self.work / "a.png").write_bytes(png(8, 8))
        (self.work / "o.png").write_bytes(b"keep")
        r = self.image("resize", "a.png", "50%", "-o", "o.png", stdin="y\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(png_size((self.work / "o.png").read_bytes()), (4, 4))

    def test_the_overwrite_prompt_at_end_of_input_keeps_the_file(self):
        (self.work / "a.png").write_bytes(png(8, 8))
        (self.work / "o.png").write_bytes(b"keep")
        r = self.image("resize", "a.png", "50%", "-o", "o.png", stdin="")
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertEqual((self.work / "o.png").read_bytes(), b"keep")

    def test_force_overwrites_without_asking(self):
        (self.work / "a.png").write_bytes(png(8, 8))
        (self.work / "o.png").write_bytes(b"keep")
        r = self.image("resize", "a.png", "50%", "-o", "o.png", "--force")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(png_size((self.work / "o.png").read_bytes()), (4, 4))
        self.assertNotIn("Overwrite", r.stdout + r.stderr)

    def test_edit_applies_the_operations_in_the_order_given(self):
        (self.work / "a.png").write_bytes(png(100, 50))
        ok = self.image("edit", "a.png", "--resize", "200", "--crop", "150x10+40+0", "-o", "o.png")
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(png_size((self.work / "o.png").read_bytes()), (150, 10))
        bad = self.image("edit", "a.png", "--crop", "150x10+40+0", "--resize", "200", "-o", "p.png")
        self.assertEqual(bad.returncode, 2)
        self.assertIn("outside the image", bad.stderr)
        self.assertFalse((self.work / "p.png").exists())

    def test_crop_flip_and_compress_work_end_to_end(self):
        (self.work / "a.png").write_bytes(png(40, 20))
        self.assertEqual(self.image("crop", "a.png", "10x5+30+15", "-o", "c.png").returncode, 0)
        self.assertEqual(png_size((self.work / "c.png").read_bytes()), (10, 5))
        self.assertEqual(self.image("flip", "a.png", "v", "-o", "f.png").returncode, 0)
        self.assertEqual(self.image("convert", "a.png", "a.jpg").returncode, 0)
        r = self.image("compress", "a.jpg", "--quality", "30", "-o", "s.jpg")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.work / "s.jpg").read_bytes().startswith(b"\xff\xd8\xff"))

    def test_quality_for_webp_output_exits_2(self):
        (self.work / "a.png").write_bytes(png(8, 8))
        r = self.image("edit", "a.png", "--rotate", "90", "-o", "o.webp", "--quality", "50")
        self.assertEqual(r.returncode, 2)
        self.assertIn("--quality only applies to JPEG output", r.stderr)
        self.assertEqual([p.name for p in self.work.iterdir()], ["a.png"])

    def test_webp_and_gif_outputs_start_with_their_magic_bytes(self):
        (self.work / "a.png").write_bytes(png(16, 16))
        self.assertEqual(self.image("convert", "a.png", "a.webp").returncode, 0)
        self.assertEqual(self.image("convert", "a.png", "a.gif").returncode, 0)
        webp = (self.work / "a.webp").read_bytes()
        self.assertTrue(webp.startswith(b"RIFF") and webp[8:12] == b"WEBP")
        self.assertTrue((self.work / "a.gif").read_bytes().startswith(b"GIF8"))
        back = self.image("convert", "a.webp", "b.png")
        self.assertEqual(back.returncode, 0, back.stderr)
        self.assertEqual(png_size((self.work / "b.png").read_bytes()), (16, 16))

    def test_an_animated_gif_warns_and_uses_the_first_frame(self):
        (self.work / "a.gif").write_bytes(gif([(1, 1), (1, 1), (1, 1)]))
        r = self.image("convert", "a.gif", "a.png")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("animated image, using the first frame only", r.stderr)

    def test_a_gif_with_a_huge_second_frame_fails_cleanly_without_a_trap(self):
        (self.work / "a.gif").write_bytes(gif([(1, 1), (60000, 60000)]))
        r = self.image("convert", "a.gif", "a.png")
        self.assertIn(r.returncode, (0, 1), r.stderr)
        self.assertNotIn("exited during", r.stderr)
        if r.returncode == 1:
            self.assertIn("image: ", r.stderr)

    def test_a_100000_square_png_header_is_refused_with_the_limit_message(self):
        for w, h, ct in ((100000, 100000, 2), (30000, 30000, 2), (50000, 50000, 0)):
            (self.work / "big.png").write_bytes(png_header_only(w, h, ct))
            r = self.image("convert", "big.png", "o.png")
            self.assertEqual(r.returncode, 1, r.stderr)
            self.assertIn("limit 64 megapixels", r.stderr)
            self.assertIn(f"{w}\u00d7{h}", r.stderr)

    def test_a_resize_beyond_the_pixel_limit_is_refused_without_a_trap(self):
        (self.work / "a.png").write_bytes(png(4, 4))
        r = self.image("resize", "a.png", "200000x200000!")
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("result too large", r.stderr)
        self.assertIn("limit 64 megapixels", r.stderr)
        self.assertNotIn("exited during", r.stderr)
        self.assertEqual([p.name for p in self.work.iterdir()], ["a.png"])

    def test_transparent_png_to_jpeg_is_white_where_transparent(self):
        (self.work / "t.png").write_bytes(png_rgba_transparent(8, 8))
        self.assertEqual(self.image("convert", "t.png", "o.jpg").returncode, 0)
        r = self.image("convert", "o.jpg", "check.png")
        self.assertEqual(r.returncode, 0, r.stderr)
        data = (self.work / "check.png").read_bytes()
        pos, idat, color_type = 8, b"", None
        while pos < len(data):
            n, kind = struct.unpack(">I4s", data[pos:pos + 8])
            if kind == b"IHDR":
                color_type = data[pos + 8 + 9]
            if kind == b"IDAT":
                idat += data[pos + 8:pos + 8 + n]
            pos += 12 + n
        self.assertEqual(color_type, 6)
        first = zlib.decompress(idat)[1:5]  # first pixel of the first row: filter-independent
        self.assertTrue(all(c >= 250 for c in first[:3]), first)

    def test_an_input_missing_with_an_existing_output_exits_1_without_asking(self):
        (self.work / "o.png").write_bytes(b"keep")
        r = self.image("resize", "nope.png", "50%", "-o", "o.png", stdin="y\n")
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("no such file", r.stderr)
        self.assertNotIn("Overwrite", r.stdout + r.stderr)
        self.assertEqual((self.work / "o.png").read_bytes(), b"keep")

    def test_a_crop_outside_the_image_with_an_existing_output_exits_2_without_asking(self):
        (self.work / "a.png").write_bytes(png(8, 8))
        (self.work / "o.png").write_bytes(b"keep")
        r = self.image("crop", "a.png", "20x20+0+0", "-o", "o.png", stdin="y\n")
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertNotIn("Overwrite", r.stdout + r.stderr)
        self.assertEqual((self.work / "o.png").read_bytes(), b"keep")

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores directory permissions")
    def test_an_unwritable_destination_exits_1_keeps_the_original_and_leaves_no_temp(self):
        (self.work / "a.png").write_bytes(png(8, 8))
        sub = self.work / "ro"
        sub.mkdir()
        (sub / "out.png").write_bytes(b"keep")
        sub.chmod(0o555)
        self.addCleanup(sub.chmod, 0o755)
        r = self.image("resize", "a.png", "50%", "-o", "ro/out.png", "--force")
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("image: ", r.stderr)
        self.assertEqual((sub / "out.png").read_bytes(), b"keep")
        self.assertEqual([p.name for p in sub.iterdir()], ["out.png"])

    @unittest.skipUnless(os.environ.get("DOCK_TEST_IMAGE_BENCH") == "1", "set DOCK_TEST_IMAGE_BENCH=1 to run the benchmark")
    def test_benchmark_resizing_a_12_megapixel_jpeg_takes_under_5_seconds(self):
        (self.work / "big.png").write_bytes(png(4000, 3000))
        r = self.image("convert", "big.png", "big.jpg")
        self.assertEqual(r.returncode, 0, r.stderr)
        start = time.monotonic()
        r = self.image("resize", "big.jpg", "50%")
        elapsed = time.monotonic() - start
        print(f"\nimage benchmark: resize 4000x3000 JPEG 50% took {elapsed:.2f}s", file=sys.stderr)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertLess(elapsed, 5.0)

    def test_the_manifest_asks_for_nothing_beyond_writing_the_current_folder(self):
        m = tomllib.loads((PLUGIN / "plugin.toml").read_text())
        self.assertEqual(m["capabilities"]["permissions"], ["fs:write:cwd"])
        self.assertEqual(set(m["capabilities"]), {"permissions"})
        self.assertEqual(list(m["run"]), ["wasm"])
        for key in ("network", "env", "http"):
            self.assertNotIn(key, m)
            self.assertNotIn(key, m["capabilities"])


if __name__ == "__main__":
    unittest.main()
