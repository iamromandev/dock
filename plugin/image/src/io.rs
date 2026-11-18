//! Paths, formats, decode, encode and the atomic write.

use crate::cli::{Kind, UsageError};
use image::codecs::gif::GifEncoder;
use image::codecs::jpeg::JpegEncoder;
use image::codecs::png::PngEncoder;
use image::codecs::webp::WebPEncoder;
use image::{DynamicImage, ImageEncoder, ImageFormat, ImageReader, RgbImage};
use std::fs;
use std::io::{self, BufWriter, Read as _, Write};
use std::path::{Component, Path, PathBuf};
use std::sync::atomic::{AtomicU32, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

pub const MAX_PIXELS: u64 = 64_000_000;
pub const MAX_BYTES: u64 = 200 * 1024 * 1024;
pub const DEFAULT_JPEG_QUALITY: u8 = 90;

/// A runtime failure: exit code 1.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Fail {
    pub message: String,
}

impl Fail {
    pub fn new(message: impl Into<String>) -> Self {
        Fail { message: message.into() }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Format {
    Png,
    Jpeg,
    WebP,
    Gif,
}

/// The output format from the file extension, case-insensitive (R13).
pub fn format_for(path: &Path) -> Result<Format, UsageError> {
    let ext = path.extension().and_then(|e| e.to_str()).map(str::to_ascii_lowercase);
    match ext.as_deref() {
        Some("png") => Ok(Format::Png),
        Some("jpg" | "jpeg") => Ok(Format::Jpeg),
        Some("webp") => Ok(Format::WebP),
        Some("gif") => Ok(Format::Gif),
        _ => Err(UsageError::new(
            None::<Kind>,
            format!("{}: unsupported output extension (use .png, .jpg, .jpeg, .webp or .gif)", path.display()),
        )),
    }
}

const OUTSIDE_MSG: &str = "the image plugin can only reach files inside the current folder";

fn outside(p: &str) -> Fail {
    Fail::new(format!("{p}: {OUTSIDE_MSG}"))
}

/// A sandbox refusal: EPERM, or WASI's ENOTCAPABLE (63 and 76 under wasmtime).
pub fn is_denied(e: &io::Error) -> bool {
    e.kind() == io::ErrorKind::PermissionDenied || matches!(e.raw_os_error(), Some(1 | 63 | 76))
}

/// Join `p` to `cwd`, normalise `.` and `..` lexically, and refuse a result outside `cwd` (R16).
pub fn resolve(cwd: &Path, p: &str) -> Result<PathBuf, Fail> {
    if p == "~" || p.starts_with("~/") {
        return Err(outside(p));
    }
    let joined = if Path::new(p).is_absolute() { PathBuf::from(p) } else { cwd.join(p) };
    let mut out = PathBuf::new();
    for c in joined.components() {
        match c {
            Component::ParentDir => {
                if !out.pop() {
                    return Err(outside(p));
                }
            }
            Component::CurDir => {}
            other => out.push(other.as_os_str()),
        }
    }
    let mut base = PathBuf::new();
    for c in cwd.components() {
        match c {
            Component::ParentDir => {
                base.pop();
            }
            Component::CurDir => {}
            other => base.push(other.as_os_str()),
        }
    }
    if out.starts_with(&base) {
        Ok(out)
    } else {
        Err(outside(p))
    }
}

pub struct Decoded {
    pub animated: bool,
    pub bytes: u64,
}

const MAX_ALLOC: u64 = 1024 * 1024 * 1024;

fn too_large(what: String) -> Fail {
    Fail::new(format!("image too large ({what})"))
}

/// Skip `n` bytes of `r`; false when the stream ends first.
fn skip(r: &mut impl io::Read, n: u64) -> bool {
    io::copy(&mut r.take(n), &mut io::sink()).is_ok_and(|c| c == n)
}

fn read_byte(r: &mut impl io::Read) -> Option<u8> {
    let mut b = [0u8; 1];
    r.read_exact(&mut b).ok()?;
    Some(b[0])
}

/// Skip a run of data sub-blocks up to and including the zero terminator.
fn skip_sub_blocks(r: &mut impl io::Read) -> bool {
    loop {
        match read_byte(r) {
            Some(0) => return true,
            Some(n) => {
                if !skip(r, n as u64) {
                    return false;
                }
            }
            None => return false,
        }
    }
}

/// True when a GIF stream holds two or more image frames. Walks the block structure without decoding any
/// pixel, so a frame with a huge declared size costs nothing. Anything malformed counts as not animated.
pub fn gif_is_animated(r: &mut impl io::Read) -> bool {
    let mut head = [0u8; 13];
    if r.read_exact(&mut head).is_err() || !head.starts_with(b"GIF8") {
        return false;
    }
    if head[10] & 0x80 != 0 && !skip(r, 3 << ((head[10] & 7) + 1)) {
        return false;
    }
    let mut frames = 0;
    loop {
        match read_byte(r) {
            Some(0x21) => {
                if read_byte(r).is_none() || !skip_sub_blocks(r) {
                    return false;
                }
            }
            Some(0x2c) => {
                let mut d = [0u8; 9];
                if r.read_exact(&mut d).is_err() {
                    return false;
                }
                if d[8] & 0x80 != 0 && !skip(r, 3 << ((d[8] & 7) + 1)) {
                    return false;
                }
                if read_byte(r).is_none() || !skip_sub_blocks(r) {
                    return false;
                }
                frames += 1;
                if frames >= 2 {
                    return true;
                }
            }
            _ => return false,
        }
    }
}

fn limits() -> image::Limits {
    let mut limits = image::Limits::default();
    limits.max_alloc = Some(MAX_ALLOC);
    limits
}

fn animated(path: &Path, format: ImageFormat) -> bool {
    let Ok(f) = fs::File::open(path) else { return false };
    let mut r = io::BufReader::new(f);
    match format {
        ImageFormat::Gif => gif_is_animated(&mut r),
        ImageFormat::WebP => image::codecs::webp::WebPDecoder::new(r)
            .map(|mut d| {
                let _ = image::ImageDecoder::set_limits(&mut d, limits());
                d.has_animation()
            })
            .unwrap_or(false),
        _ => false,
    }
}

fn be16(b: &[u8]) -> u32 {
    u16::from_be_bytes([b[0], b[1]]) as u32
}
fn le24(b: &[u8]) -> u32 {
    b[0] as u32 | (b[1] as u32) << 8 | (b[2] as u32) << 16
}

/// Width and height straight from the format header bytes (PNG, GIF, JPEG, WebP), no decoding and no
/// allocation. None when the bytes are not a recognisable header.
pub fn header_dims(b: &[u8]) -> Option<(u32, u32)> {
    if b.len() >= 24 && b.starts_with(b"\x89PNG\r\n\x1a\n") && &b[12..16] == b"IHDR" {
        return Some((u32::from_be_bytes(b[16..20].try_into().ok()?), u32::from_be_bytes(b[20..24].try_into().ok()?)));
    }
    if b.len() >= 10 && b.starts_with(b"GIF8") {
        return Some((u16::from_le_bytes([b[6], b[7]]) as u32, u16::from_le_bytes([b[8], b[9]]) as u32));
    }
    if b.len() >= 4 && b.starts_with(&[0xff, 0xd8]) {
        let mut i = 2;
        while i + 4 <= b.len() {
            if b[i] != 0xff {
                return None;
            }
            let m = b[i + 1];
            if m == 0xff {
                i += 1;
                continue;
            }
            if (0xc0..=0xcf).contains(&m) && !matches!(m, 0xc4 | 0xc8 | 0xcc) {
                return (i + 9 <= b.len()).then(|| (be16(&b[i + 7..]), be16(&b[i + 5..])));
            }
            if m == 0xd8 || m == 0x01 || (0xd0..=0xd7).contains(&m) {
                i += 2;
            } else {
                i += 2 + be16(&b[i + 2..]) as usize;
            }
        }
        return None;
    }
    if b.len() >= 30 && b.starts_with(b"RIFF") && &b[8..12] == b"WEBP" {
        return match &b[12..16] {
            b"VP8X" => Some((le24(&b[24..]) + 1, le24(&b[27..]) + 1)),
            b"VP8L" if b[20] == 0x2f => {
                let bits = u32::from_le_bytes(b[21..25].try_into().ok()?);
                Some(((bits & 0x3fff) + 1, ((bits >> 14) & 0x3fff) + 1))
            }
            b"VP8 " if b[23..26] == [0x9d, 0x01, 0x2a] => {
                Some((u16::from_le_bytes([b[26], b[27]]) as u32 & 0x3fff, u16::from_le_bytes([b[28], b[29]]) as u32 & 0x3fff))
            }
            _ => None,
        };
    }
    None
}

/// Header size of the file at `path`, falling back to the image crate's reader for anything unusual.
fn file_dims(path: &Path) -> Option<(u32, u32)> {
    let mut head = Vec::new();
    io::Read::take(fs::File::open(path).ok()?, 1 << 20).read_to_end(&mut head).ok()?;
    header_dims(&head).or_else(|| {
        ImageReader::open(path).ok()?.with_guessed_format().ok()?.into_dimensions().ok()
    })
}

/// Read an image, detecting its format from the content (R12). Files over MAX_BYTES and images over
/// MAX_PIXELS are refused before any pixel is decoded (NFR4). An animated GIF or WebP gives its first frame.
pub fn decode(path: &Path) -> Result<(DynamicImage, Decoded), Fail> {
    let unreadable = || Fail::new("unsupported or unreadable image");
    let meta = match fs::metadata(path) {
        Ok(m) => m,
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Err(Fail::new("no such file")),
        Err(e) if is_denied(&e) => return Err(Fail::new(OUTSIDE_MSG)),
        Err(_) => return Err(unreadable()),
    };
    if meta.len() > MAX_BYTES {
        return Err(too_large(format!("{} MB, limit {} MB", meta.len() / (1024 * 1024), MAX_BYTES / (1024 * 1024))));
    }
    let open = || {
        ImageReader::open(path)
            .map_err(|e| if is_denied(&e) { Fail::new(OUTSIDE_MSG) } else { unreadable() })?
            .with_guessed_format()
            .map_err(|_| unreadable())
    };
    let reader = open()?;
    let Some(format) = reader.format().filter(|f| matches!(f, ImageFormat::Png | ImageFormat::Jpeg | ImageFormat::WebP | ImageFormat::Gif)) else {
        return Err(unreadable());
    };
    drop(reader);
    let (w, h) = file_dims(path).ok_or_else(unreadable)?;
    if w as u64 * h as u64 > MAX_PIXELS {
        return Err(too_large(format!("{w}×{h}, limit {} megapixels", MAX_PIXELS / 1_000_000)));
    }
    let mut reader = open()?;
    reader.limits(limits());
    let img = reader.decode().map_err(|_| unreadable())?;
    Ok((img, Decoded { animated: animated(path, format), bytes: meta.len() }))
}

/// Width and height from the header, without decoding. None when unreadable.
pub fn dimensions(path: &Path) -> Option<(u32, u32)> {
    file_dims(path)
}

/// Transparent pixels become white (R18).
fn flatten_on_white(img: &DynamicImage) -> RgbImage {
    let rgba = img.to_rgba8();
    let mut out = RgbImage::new(rgba.width(), rgba.height());
    for (x, y, p) in rgba.enumerate_pixels() {
        let a = p[3] as u32;
        let mix = |c: u8| ((c as u32 * a + 255 * (255 - a) + 127) / 255) as u8;
        out.put_pixel(x, y, image::Rgb([mix(p[0]), mix(p[1]), mix(p[2])]));
    }
    out
}

pub fn encode(img: &DynamicImage, format: Format, quality: Option<u8>, w: &mut dyn Write) -> io::Result<()> {
    let other = |e: image::ImageError| io::Error::new(io::ErrorKind::Other, e.to_string());
    match format {
        Format::Png => {
            let rgba = img.to_rgba8();
            PngEncoder::new(w)
                .write_image(rgba.as_raw(), rgba.width(), rgba.height(), image::ExtendedColorType::Rgba8)
                .map_err(other)
        }
        Format::Jpeg => {
            let rgb = flatten_on_white(img);
            JpegEncoder::new_with_quality(w, quality.unwrap_or(DEFAULT_JPEG_QUALITY))
                .write_image(rgb.as_raw(), rgb.width(), rgb.height(), image::ExtendedColorType::Rgb8)
                .map_err(other)
        }
        Format::WebP => {
            let rgba = img.to_rgba8();
            WebPEncoder::new_lossless(w)
                .write_image(rgba.as_raw(), rgba.width(), rgba.height(), image::ExtendedColorType::Rgba8)
                .map_err(other)
        }
        Format::Gif => {
            let rgba = img.to_rgba8();
            GifEncoder::new(w)
                .encode(rgba.as_raw(), rgba.width(), rgba.height(), image::ExtendedColorType::Rgba8)
                .map_err(other)
        }
    }
}

fn temp_prefix(dest: &Path) -> String {
    format!(".{}.image-tmp-", dest.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default())
}

/// Temp files older than this are leftovers of a killed run; younger ones may belong to a live one.
const STALE_AFTER_SECS: u64 = 10 * 60;

static COUNTER: AtomicU32 = AtomicU32::new(0);

/// Hex digits only: the clock in nanoseconds followed by a per-process counter.
fn unique_suffix() -> String {
    let nanos = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_nanos()).unwrap_or(0);
    format!("{nanos:x}{:04x}", COUNTER.fetch_add(1, Ordering::Relaxed) & 0xffff)
}

fn is_stale_temp(e: &fs::DirEntry, prefix: &str) -> bool {
    let name = e.file_name().to_string_lossy().into_owned();
    let Some(suffix) = name.strip_prefix(prefix) else { return false };
    if suffix.is_empty() || !suffix.bytes().all(|b| b.is_ascii_hexdigit()) {
        return false;
    }
    let Ok(meta) = e.metadata() else { return false };
    if !meta.is_file() {
        return false;
    }
    meta.modified()
        .ok()
        .and_then(|m| SystemTime::now().duration_since(m).ok())
        .is_some_and(|age| age.as_secs() > STALE_AFTER_SECS)
}

fn write_failed(dest: &Path, e: &io::Error) -> Fail {
    if is_denied(e) {
        return Fail::new(format!("{}: {OUTSIDE_MSG}", dest.display()));
    }
    Fail::new(format!("{}: cannot write: {e}", dest.display()))
}

/// Encode into a temp file beside `dest`, sync it and rename it into place (NFR2). Returns the bytes written.
/// Stale temp files of this output (left by a kill) are swept first.
pub fn write_atomic(dest: &Path, encode: impl FnOnce(&mut dyn Write) -> io::Result<()>) -> Result<u64, Fail> {
    let dir = dest.parent().unwrap_or_else(|| Path::new("."));
    let prefix = temp_prefix(dest);
    if let Ok(entries) = fs::read_dir(dir) {
        for e in entries.flatten() {
            if is_stale_temp(&e, &prefix) {
                let _ = fs::remove_file(e.path());
            }
        }
    }
    let mut tries = 0u32;
    let (tmp, file) = loop {
        let tmp = dir.join(format!("{prefix}{}", unique_suffix()));
        match fs::OpenOptions::new().write(true).create_new(true).open(&tmp) {
            Ok(f) => break (tmp, f),
            Err(e) if e.kind() == io::ErrorKind::AlreadyExists && tries < 1000 => tries += 1,
            Err(e) => return Err(write_failed(dest, &e)),
        }
    };
    let result = (|| -> io::Result<u64> {
        let mut w = BufWriter::new(file);
        encode(&mut w)?;
        w.flush()?;
        let file = w.into_inner().map_err(|e| e.into_error())?;
        file.sync_all()?;
        let len = file.metadata()?.len();
        drop(file);
        fs::rename(&tmp, dest)?;
        Ok(len)
    })();
    result.map_err(|e| {
        let _ = fs::remove_file(&tmp);
        write_failed(dest, &e)
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use image::{Rgba, RgbaImage};

    fn tmpdir(name: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("image-plugin-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&d);
        fs::create_dir_all(&d).unwrap();
        d
    }

    #[test]
    fn extensions_map_to_formats_case_insensitively() {
        assert_eq!(format_for(Path::new("a.PNG")), Ok(Format::Png));
        assert_eq!(format_for(Path::new("a.Jpeg")), Ok(Format::Jpeg));
        assert_eq!(format_for(Path::new("a.jpg")), Ok(Format::Jpeg));
        assert_eq!(format_for(Path::new("a.webp")), Ok(Format::WebP));
        assert_eq!(format_for(Path::new("a.gif")), Ok(Format::Gif));
        assert!(format_for(Path::new("a.bmp")).unwrap_err().message.contains(".png"));
        assert!(format_for(Path::new("noext")).is_err());
    }

    #[test]
    fn resolve_joins_relative_paths_to_cwd() {
        let cwd = Path::new("/w/dir");
        assert_eq!(resolve(cwd, "a.png").unwrap(), PathBuf::from("/w/dir/a.png"));
        assert_eq!(resolve(cwd, "./sub/../a.png").unwrap(), PathBuf::from("/w/dir/a.png"));
        assert_eq!(resolve(cwd, "/w/dir/x/a.png").unwrap(), PathBuf::from("/w/dir/x/a.png"));
    }

    #[test]
    fn resolve_refuses_paths_outside_cwd() {
        let cwd = Path::new("/w/dir");
        for p in ["../a.png", "sub/../../a.png", "/etc/passwd", "/w/dir2/a.png", "~/Pictures/a.png", "~"] {
            let e = resolve(cwd, p).unwrap_err();
            assert!(e.message.contains("inside the current folder"), "{p}: {}", e.message);
        }
    }

    #[test]
    fn png_and_jpeg_round_trip_with_magic_bytes() {
        let dir = tmpdir("roundtrip");
        let img = DynamicImage::ImageRgba8(RgbaImage::from_pixel(8, 6, Rgba([200, 100, 50, 255])));
        for (name, fmt, magic) in [("a.png", Format::Png, &b"\x89PNG"[..]), ("a.jpg", Format::Jpeg, &b"\xff\xd8\xff"[..])] {
            let p = dir.join(name);
            write_atomic(&p, |w| encode(&img, fmt, None, w)).unwrap();
            assert!(fs::read(&p).unwrap().starts_with(magic));
            let (back, d) = decode(&p).unwrap();
            assert_eq!((back.width(), back.height()), (8, 6));
            assert!(!d.animated);
            assert_eq!(d.bytes, fs::metadata(&p).unwrap().len());
        }
        let (png, _) = decode(&dir.join("a.png")).unwrap();
        assert_eq!(png.to_rgba8().get_pixel(3, 3), &Rgba([200, 100, 50, 255]));
    }

    fn gradient(w: u32, h: u32) -> DynamicImage {
        DynamicImage::ImageRgba8(RgbaImage::from_fn(w, h, |x, y| {
            Rgba([(x * 7 % 256) as u8, (y * 13 % 256) as u8, ((x * y) % 256) as u8, 255])
        }))
    }

    #[test]
    fn webp_round_trips_losslessly_with_riff_magic() {
        let dir = tmpdir("webp");
        let img = gradient(9, 7);
        let p = dir.join("a.webp");
        write_atomic(&p, |w| encode(&img, Format::WebP, None, w)).unwrap();
        let bytes = fs::read(&p).unwrap();
        assert!(bytes.starts_with(b"RIFF") && &bytes[8..12] == b"WEBP");
        let (back, d) = decode(&p).unwrap();
        assert_eq!(back.to_rgba8(), img.to_rgba8());
        assert!(!d.animated);
    }

    #[test]
    fn gif_round_trips_with_gif_magic_and_keeps_the_size() {
        let dir = tmpdir("gif");
        let img = DynamicImage::ImageRgba8(RgbaImage::from_pixel(10, 5, Rgba([200, 30, 30, 255])));
        let p = dir.join("a.gif");
        write_atomic(&p, |w| encode(&img, Format::Gif, None, w)).unwrap();
        assert!(fs::read(&p).unwrap().starts_with(b"GIF89a"));
        let (back, d) = decode(&p).unwrap();
        assert_eq!((back.width(), back.height()), (10, 5));
        assert!(!d.animated);
    }

    fn three_frame_gif(path: &Path) {
        use image::codecs::gif::GifEncoder;
        use image::Frame;
        let mut enc = GifEncoder::new(fs::File::create(path).unwrap());
        for c in [[255u8, 0, 0, 255], [0, 255, 0, 255], [0, 0, 255, 255]] {
            enc.encode_frame(Frame::new(RgbaImage::from_pixel(6, 4, Rgba(c)))).unwrap();
        }
    }

    #[test]
    fn an_animated_gif_decodes_to_its_first_frame_and_is_flagged() {
        let dir = tmpdir("anim");
        let p = dir.join("anim.gif");
        three_frame_gif(&p);
        let (img, d) = decode(&p).unwrap();
        assert!(d.animated);
        assert_eq!((img.width(), img.height()), (6, 4));
        let px = img.to_rgba8().get_pixel(0, 0).0;
        assert!(px[0] > 200 && px[1] < 50 && px[2] < 50, "{px:?}");
    }

    fn crc32(data: &[u8]) -> u32 {
        let mut c = !0u32;
        for &b in data {
            c ^= b as u32;
            for _ in 0..8 {
                c = if c & 1 != 0 { (c >> 1) ^ 0xEDB8_8320 } else { c >> 1 };
            }
        }
        !c
    }

    #[test]
    fn a_png_claiming_a_huge_size_is_refused_before_decoding() {
        let dir = tmpdir("bomb");
        let mut ihdr = b"IHDR".to_vec();
        ihdr.extend(100_000u32.to_be_bytes());
        ihdr.extend(100_000u32.to_be_bytes());
        ihdr.extend([8, 2, 0, 0, 0]);
        let mut bytes = b"\x89PNG\r\n\x1a\n".to_vec();
        bytes.extend(13u32.to_be_bytes());
        bytes.extend(&ihdr);
        bytes.extend(crc32(&ihdr).to_be_bytes());
        let idat = b"IDAT\x78\x9c\x03\x00\x00\x00\x00\x01";
        bytes.extend(4u32.to_be_bytes());
        bytes.extend(&idat[..8]);
        bytes.extend(crc32(&idat[..8]).to_be_bytes());
        let p = dir.join("bomb.png");
        fs::write(&p, bytes).unwrap();
        let e = decode(&p).err().unwrap();
        assert!(e.message.contains("too large") && e.message.contains("100000×100000"), "{}", e.message);
    }

    #[test]
    fn a_file_over_the_byte_limit_is_refused() {
        let dir = tmpdir("bigfile");
        let p = dir.join("big.png");
        fs::File::create(&p).unwrap().set_len(MAX_BYTES + 1).unwrap();
        let e = decode(&p).err().unwrap();
        assert!(e.message.contains("too large"), "{}", e.message);
    }

    #[test]
    fn a_lower_jpeg_quality_gives_a_smaller_file() {
        let img = gradient(96, 96);
        let size = |q| {
            let mut v = Vec::new();
            encode(&img, Format::Jpeg, Some(q), &mut v).unwrap();
            v.len()
        };
        assert!(size(30) < size(90));
    }

    #[test]
    fn dimensions_reads_the_header_only() {
        let dir = tmpdir("dims");
        let p = dir.join("a.png");
        write_atomic(&p, |w| encode(&gradient(12, 3), Format::Png, None, w)).unwrap();
        assert_eq!(dimensions(&p), Some((12, 3)));
        assert_eq!(dimensions(&dir.join("none.png")), None);
    }

    fn gif_bytes(frames: &[(u16, u16)]) -> Vec<u8> {
        let mut v = b"GIF89a".to_vec();
        v.extend([10, 0, 10, 0, 0x80, 0, 0, 0, 0, 0, 255, 255, 255]);
        for (w, h) in frames {
            v.extend([0x21, 0xf9, 4, 0, 0, 0, 0, 0]); // graphic control extension
            v.push(0x2c);
            v.extend([0, 0, 0, 0]);
            v.extend(w.to_le_bytes());
            v.extend(h.to_le_bytes());
            v.extend([0, 2, 2, 0x44, 1, 0]);
        }
        v.push(0x3b);
        v
    }

    #[test]
    fn gif_animation_is_detected_from_the_block_structure() {
        assert!(!gif_is_animated(&mut &gif_bytes(&[(1, 1)])[..]));
        assert!(gif_is_animated(&mut &gif_bytes(&[(1, 1), (1, 1), (1, 1)])[..]));
        assert!(gif_is_animated(&mut &gif_bytes(&[(1, 1), (1, 1)])[..]));
    }

    #[test]
    fn a_huge_second_gif_frame_is_detected_without_decoding_it() {
        assert!(gif_is_animated(&mut &gif_bytes(&[(10, 10), (60000, 60000)])[..]));
    }

    #[test]
    fn a_malformed_gif_is_not_animated() {
        let good = gif_bytes(&[(1, 1), (1, 1)]);
        assert!(!gif_is_animated(&mut &good[..20]));
        assert!(!gif_is_animated(&mut &b"not a gif at all"[..]));
        assert!(!gif_is_animated(&mut &b""[..]));
    }

    #[test]
    fn a_gif_with_a_huge_second_frame_decodes_the_first_frame_with_a_warning_flag() {
        let dir = tmpdir("hugegif");
        let p = dir.join("a.gif");
        fs::write(&p, gif_bytes(&[(10, 10), (60000, 60000)])).unwrap();
        match decode(&p) {
            Ok((_, d)) => assert!(d.animated),
            Err(f) => assert!(!f.message.is_empty()),
        }
    }

    #[test]
    fn header_dims_reads_png_gif_and_jpeg_headers() {
        let mut png = b"\x89PNG\r\n\x1a\n\0\0\0\x0dIHDR".to_vec();
        png.extend(100_000u32.to_be_bytes());
        png.extend(100_000u32.to_be_bytes());
        assert_eq!(header_dims(&png), Some((100_000, 100_000)));
        assert_eq!(header_dims(&gif_bytes(&[(1, 1)])), Some((10, 10)));
        let jpeg = [0xff, 0xd8, 0xff, 0xe0, 0, 4, 0, 0, 0xff, 0xc0, 0, 11, 8, 0x01, 0x00, 0x02, 0x00, 1, 0, 0, 0];
        assert_eq!(header_dims(&jpeg), Some((512, 256)));
        assert_eq!(header_dims(b"nope"), None);
    }

    #[test]
    fn an_oversized_png_header_gets_the_limit_message() {
        let dir = tmpdir("bigpng");
        let p = dir.join("big.png");
        let mut png = b"\x89PNG\r\n\x1a\n\0\0\0\x0dIHDR".to_vec();
        png.extend(100_000u32.to_be_bytes());
        png.extend(100_000u32.to_be_bytes());
        png.extend([8, 2, 0, 0, 0]);
        fs::write(&p, png).unwrap();
        let e = decode(&p).err().unwrap();
        assert!(e.message.contains("100000×100000, limit 64 megapixels"), "{}", e.message);
    }

    #[test]
    fn decode_sniffs_content_not_extension() {
        let dir = tmpdir("sniff");
        let img = DynamicImage::ImageRgba8(RgbaImage::from_pixel(4, 4, Rgba([1, 2, 3, 255])));
        let p = dir.join("x.jpg");
        write_atomic(&p, |w| encode(&img, Format::Png, None, w)).unwrap();
        assert!(fs::read(&p).unwrap().starts_with(b"\x89PNG"));
        assert!(decode(&p).is_ok());
    }

    #[test]
    fn decode_rejects_garbage_empty_and_missing() {
        let dir = tmpdir("garbage");
        fs::write(dir.join("t.png"), b"not an image").unwrap();
        fs::write(dir.join("e.png"), b"").unwrap();
        assert_eq!(decode(&dir.join("t.png")).err().unwrap().message, "unsupported or unreadable image");
        assert_eq!(decode(&dir.join("e.png")).err().unwrap().message, "unsupported or unreadable image");
        assert_eq!(decode(&dir.join("nope.png")).err().unwrap().message, "no such file");
    }

    #[test]
    fn transparent_pixels_become_white_in_jpeg() {
        let dir = tmpdir("alpha");
        let img = DynamicImage::ImageRgba8(RgbaImage::from_pixel(16, 16, Rgba([0, 0, 0, 0])));
        let p = dir.join("a.jpg");
        write_atomic(&p, |w| encode(&img, Format::Jpeg, None, w)).unwrap();
        let px = decode(&p).unwrap().0.to_rgb8().get_pixel(8, 8).0;
        assert!(px.iter().all(|&c| c >= 250), "{px:?}");
    }

    #[test]
    fn a_failing_encode_leaves_the_destination_unchanged_and_no_temp() {
        let dir = tmpdir("atomic-fail");
        let dest = dir.join("out.png");
        fs::write(&dest, b"original").unwrap();
        let r = write_atomic(&dest, |w| {
            w.write_all(b"partial")?;
            Err(io::Error::new(io::ErrorKind::Other, "boom"))
        });
        assert!(r.is_err());
        assert_eq!(fs::read(&dest).unwrap(), b"original");
        assert_eq!(fs::read_dir(&dir).unwrap().count(), 1);
    }

    #[test]
    fn a_good_write_replaces_the_destination_and_reports_its_size() {
        let dir = tmpdir("atomic-ok");
        let dest = dir.join("out.png");
        fs::write(&dest, b"original").unwrap();
        assert_eq!(write_atomic(&dest, |w| w.write_all(b"new data")).unwrap(), 8);
        assert_eq!(fs::read(&dest).unwrap(), b"new data");
        assert_eq!(fs::read_dir(&dir).unwrap().count(), 1);
    }

    fn age(p: &Path, secs: u64) {
        let f = fs::OpenOptions::new().write(true).open(p).unwrap();
        f.set_modified(std::time::SystemTime::now() - std::time::Duration::from_secs(secs)).unwrap();
    }

    #[test]
    fn old_matching_temp_files_of_this_output_are_swept() {
        let dir = tmpdir("sweep");
        let dest = dir.join("out.png");
        fs::write(dir.join(".out.png.image-tmp-1a2b"), b"stale").unwrap();
        age(&dir.join(".out.png.image-tmp-1a2b"), STALE_AFTER_SECS + 60);
        fs::write(dir.join(".other.png.image-tmp-1a2b"), b"keep").unwrap();
        age(&dir.join(".other.png.image-tmp-1a2b"), STALE_AFTER_SECS + 60);
        write_atomic(&dest, |w| w.write_all(b"x")).unwrap();
        assert!(!dir.join(".out.png.image-tmp-1a2b").exists());
        assert!(dir.join(".other.png.image-tmp-1a2b").exists());
    }

    #[test]
    fn a_fresh_matching_temp_file_survives_the_sweep() {
        let dir = tmpdir("sweep-fresh");
        fs::write(dir.join(".out.png.image-tmp-1a2b"), b"live").unwrap();
        write_atomic(&dir.join("out.png"), |w| w.write_all(b"x")).unwrap();
        assert_eq!(fs::read(dir.join(".out.png.image-tmp-1a2b")).unwrap(), b"live");
    }

    #[test]
    fn a_non_matching_suffix_survives_the_sweep_even_when_old() {
        let dir = tmpdir("sweep-name");
        for n in [".out.png.image-tmp-notes", ".out.png.image-tmp-", ".out.png.image-tmp-12-x"] {
            fs::write(dir.join(n), b"mine").unwrap();
            age(&dir.join(n), STALE_AFTER_SECS + 60);
        }
        write_atomic(&dir.join("out.png"), |w| w.write_all(b"x")).unwrap();
        for n in [".out.png.image-tmp-notes", ".out.png.image-tmp-", ".out.png.image-tmp-12-x"] {
            assert!(dir.join(n).exists(), "{n}");
        }
    }

    #[test]
    fn an_old_matching_directory_is_not_removed() {
        let dir = tmpdir("sweep-dir");
        fs::create_dir(dir.join(".out.png.image-tmp-1a2b")).unwrap();
        write_atomic(&dir.join("out.png"), |w| w.write_all(b"x")).unwrap();
        assert!(dir.join(".out.png.image-tmp-1a2b").is_dir());
    }

    #[test]
    fn sandbox_denials_are_recognised() {
        for code in [1, 63, 76] {
            assert!(is_denied(&io::Error::from_raw_os_error(code)), "{code}");
        }
        assert!(is_denied(&io::Error::from(io::ErrorKind::PermissionDenied)));
        assert!(!is_denied(&io::Error::from(io::ErrorKind::NotFound)));
    }

    #[test]
    fn a_denied_write_reports_the_current_folder_rule() {
        let f = write_failed(Path::new("/w/o.png"), &io::Error::from_raw_os_error(76));
        assert_eq!(f.message, format!("/w/o.png: {OUTSIDE_MSG}"));
    }

    #[test]
    fn a_missing_destination_folder_fails() {
        let dir = tmpdir("nodir");
        assert!(write_atomic(&dir.join("nope").join("o.png"), |w| w.write_all(b"x")).is_err());
    }
}
