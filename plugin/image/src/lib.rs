pub mod cli;
pub mod geometry;
pub mod io;
pub mod ops;
pub mod report;
pub mod rpc;

/// Everything the plugin says to, or asks of, dock.
pub trait Host {
    fn output(&mut self, text: &str);
    fn log(&mut self, level: &str, message: &str);
    fn progress(&mut self, percent: Option<f64>, message: &str);
    /// None = end of input.
    fn prompt(&mut self, prompt: &str) -> Option<String>;
}

use cli::{Command, Parsed, UsageError, USAGE};
use io::Fail;
use std::path::{Path, PathBuf};

fn usage_failure(host: &mut dyn Host, e: &UsageError) -> i32 {
    host.log("error", &format!("image: {}", e.message));
    host.log("error", e.op.map_or(USAGE, |k| k.usage()));
    2
}

/// `<stem>.edited.<ext>` next to the input, with the input's extension (R14).
fn default_output(input: &str) -> String {
    let p = Path::new(input);
    let stem = p.file_stem().map(|s| s.to_string_lossy().into_owned()).unwrap_or_default();
    let name = match p.extension() {
        Some(e) => format!("{stem}.edited.{}", e.to_string_lossy()),
        None => format!("{stem}.edited"),
    };
    p.with_file_name(name).to_string_lossy().into_owned()
}

/// True for anything at `p`, including a symlink (even a dangling or sandbox-escaping one).
fn exists_no_follow(p: &Path) -> bool {
    std::fs::symlink_metadata(p).is_ok()
}

/// Progress is only worth reporting for inputs of this many pixels or more (R20).
const PROGRESS_PIXELS: u64 = 4_000_000;

fn confirmed(answer: Option<String>) -> bool {
    answer.is_some_and(|a| matches!(a.trim().to_ascii_lowercase().as_str(), "y" | "yes"))
}

fn execute(cmd: &Command, cwd: &Path, host: &mut dyn Host) -> Result<String, Result<Fail, UsageError>> {
    let out_name = cmd.output.clone().unwrap_or_else(|| default_output(&cmd.input));
    let format = io::format_for(Path::new(&out_name)).map_err(Err)?;
    if cmd.quality.is_some() && format != io::Format::Jpeg {
        return Err(Err(UsageError::new(Some(cmd.kind), "--quality only applies to JPEG output")));
    }
    let input: PathBuf = io::resolve(cwd, &cmd.input).map_err(Ok)?;
    let output: PathBuf = io::resolve(cwd, &out_name).map_err(Ok)?;
    let report_progress = io::dimensions(&input).is_some_and(|(w, h)| w as u64 * h as u64 >= PROGRESS_PIXELS);
    if report_progress {
        host.progress(Some(0.0), "decoding");
    }
    let (mut img, decoded) =
        io::decode(&input).map_err(|f| Ok(Fail::new(format!("{}: {}", cmd.input, f.message))))?;
    if decoded.animated {
        host.log("warn", &format!("image: {}: animated image, using the first frame only", cmd.input));
    }
    let before = (img.width(), img.height());
    if report_progress {
        host.progress(Some(33.0), "editing");
    }
    for op in &cmd.ops {
        if let geometry_op @ ops::Op::Resize(_) = op {
            ops::check_resize((img.width(), img.height()), geometry_op, format == io::Format::Gif).map_err(Ok)?;
        }
        img = ops::apply(op, img).map_err(|mut e| {
            e.op = Some(cmd.kind);
            Err(e)
        })?;
    }
    let after = (img.width(), img.height());
    if format == io::Format::Gif && (after.0 > ops::GIF_MAX_SIDE || after.1 > ops::GIF_MAX_SIDE) {
        return Err(Ok(ops::gif_side_failure(after)));
    }
    if report_progress {
        host.progress(Some(66.0), "encoding");
    }
    // Asked last, once the input is known good and the edit can succeed. The output may be the input itself;
    // it exists either way, so the same prompt covers both.
    if exists_no_follow(&output) && !cmd.force && !confirmed(host.prompt(&format!("Overwrite {out_name}? [y/N] "))) {
        return Err(Ok(Fail::new(format!("{out_name}: exists, not overwritten (use --force to skip the question)"))));
    }
    let written = io::write_atomic(&output, |w| io::encode(&img, format, cmd.quality, w)).map_err(Ok)?;
    if report_progress {
        host.progress(Some(100.0), "done");
    }
    Ok(report::summary(&out_name, before, after, decoded.bytes, written))
}

/// Run one `image` invocation; the result is the exit code.
pub fn run(args: &[String], cwd: &Path, host: &mut dyn Host) -> i32 {
    let cmd = match cli::parse(args) {
        Ok(Parsed::Help) => {
            host.output(USAGE);
            return 0;
        }
        Ok(Parsed::Run(c)) => c,
        Err(e) => return usage_failure(host, &e),
    };
    match execute(&cmd, cwd, host) {
        Ok(line) => {
            host.output(&format!("{line}\n"));
            0
        }
        Err(Ok(fail)) => {
            host.log("error", &format!("image: {}", fail.message));
            1
        }
        Err(Err(usage)) => usage_failure(host, &usage),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use image::{DynamicImage, Rgba, RgbaImage};
    use std::fs;

    #[derive(Default)]
    struct FakeHost {
        out: Vec<String>,
        logs: Vec<(String, String)>,
        progress: Vec<(Option<f64>, String)>,
        prompts: Vec<String>,
        answers: Vec<Option<String>>,
    }
    impl Host for FakeHost {
        fn output(&mut self, t: &str) {
            self.out.push(t.to_string());
        }
        fn log(&mut self, l: &str, m: &str) {
            self.logs.push((l.to_string(), m.to_string()));
        }
        fn progress(&mut self, p: Option<f64>, m: &str) {
            self.progress.push((p, m.to_string()));
        }
        fn prompt(&mut self, p: &str) -> Option<String> {
            self.prompts.push(p.to_string());
            if self.answers.is_empty() { None } else { self.answers.remove(0) }
        }
    }

    fn work(name: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("image-plugin-run-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&d);
        fs::create_dir_all(&d).unwrap();
        d
    }
    fn make_png(path: &Path, w: u32, h: u32) {
        let img = DynamicImage::ImageRgba8(RgbaImage::from_pixel(w, h, Rgba([9, 9, 9, 255])));
        img.save_with_format(path, image::ImageFormat::Png).unwrap();
    }
    fn go(cwd: &Path, args: &[&str]) -> (i32, FakeHost) {
        let mut h = FakeHost::default();
        let a: Vec<String> = args.iter().map(|s| s.to_string()).collect();
        (run(&a, cwd, &mut h), h)
    }
    fn go_answering(cwd: &Path, args: &[&str], answers: &[Option<&str>]) -> (i32, FakeHost) {
        let mut h = FakeHost { answers: answers.iter().map(|a| a.map(String::from)).collect(), ..Default::default() };
        let a: Vec<String> = args.iter().map(|s| s.to_string()).collect();
        (run(&a, cwd, &mut h), h)
    }
    fn names(d: &Path) -> Vec<String> {
        let mut v: Vec<String> = fs::read_dir(d).unwrap().map(|e| e.unwrap().file_name().to_string_lossy().into_owned()).collect();
        v.sort();
        v
    }
    fn dims(p: &Path) -> (u32, u32) {
        image::image_dimensions(p).unwrap()
    }

    #[test]
    fn help_prints_usage_and_exits_0() {
        let (code, h) = go(Path::new("/nowhere"), &["--help"]);
        assert_eq!(code, 0);
        assert!(h.out[0].contains("image edit"));
        assert!(h.logs.is_empty());
    }
    #[test]
    fn bare_prints_usage_via_log_and_exits_2_with_nothing_on_stdout() {
        let (code, h) = go(Path::new("/nowhere"), &[]);
        assert_eq!(code, 2);
        assert!(h.out.is_empty());
        assert!(h.logs.iter().all(|(l, _)| l == "error"));
        assert!(h.logs.iter().any(|(_, m)| m.starts_with("image: ")));
    }
    #[test]
    fn usage_error_creates_no_file() {
        let d = work("usage");
        make_png(&d.join("a.png"), 4, 4);
        for args in [&["resize", "a.png"][..], &["blur", "a.png"], &["rotate", "a.png", "45"], &["convert", "a.png", "a.bmp"]] {
            let (code, h) = go(&d, args);
            assert_eq!(code, 2, "{args:?}");
            assert!(h.logs[0].1.starts_with("image: "));
        }
        assert_eq!(fs::read_dir(&d).unwrap().count(), 1);
    }
    #[test]
    fn resize_writes_default_name_and_one_summary_line() {
        let d = work("resize");
        make_png(&d.join("a.png"), 100, 50);
        let (code, h) = go(&d, &["resize", "a.png", "50%"]);
        assert_eq!(code, 0, "{:?}", h.logs);
        assert_eq!(dims(&d.join("a.edited.png")), (50, 25));
        assert_eq!(h.out.len(), 1);
        assert!(h.out[0].starts_with("a.edited.png: 100×50 → 50×25, "), "{}", h.out[0]);
        assert!(h.out[0].ends_with('\n'));
    }
    #[test]
    fn default_output_name_keeps_directory_and_extension() {
        assert_eq!(default_output("photo.jpg"), "photo.edited.jpg");
        assert_eq!(default_output("sub/a.b.PNG"), "sub/a.b.edited.PNG");
    }
    #[test]
    fn rotate_swaps_dimensions() {
        let d = work("rotate");
        make_png(&d.join("a.png"), 6, 2);
        let (code, _) = go(&d, &["rotate", "a.png", "90", "-o", "r.png"]);
        assert_eq!(code, 0);
        assert_eq!(dims(&d.join("r.png")), (2, 6));
    }
    #[test]
    fn convert_to_jpeg_writes_jpeg_magic() {
        let d = work("convert");
        make_png(&d.join("a.png"), 8, 8);
        let (code, _) = go(&d, &["convert", "a.png", "a.jpg"]);
        assert_eq!(code, 0);
        assert!(fs::read(d.join("a.jpg")).unwrap().starts_with(b"\xff\xd8\xff"));
    }
    #[test]
    fn existing_output_without_force_exits_1_and_is_untouched() {
        let d = work("exists");
        make_png(&d.join("a.png"), 8, 8);
        fs::write(d.join("o.png"), b"keep").unwrap();
        let (code, h) = go(&d, &["resize", "a.png", "50%", "-o", "o.png"]);
        assert_eq!(code, 1);
        assert!(h.logs[0].1.contains("exists"));
        assert_eq!(fs::read(d.join("o.png")).unwrap(), b"keep");
        let (code, _) = go(&d, &["resize", "a.png", "50%", "-o", "o.png", "--force"]);
        assert_eq!(code, 0);
        assert_eq!(dims(&d.join("o.png")), (4, 4));
    }
    #[test]
    fn paths_outside_cwd_exit_1_and_write_nothing() {
        let d = work("outside");
        let sub = d.join("sub");
        fs::create_dir_all(&sub).unwrap();
        make_png(&d.join("a.png"), 8, 8);
        make_png(&sub.join("b.png"), 8, 8);
        let (code, h) = go(&sub, &["resize", "../a.png", "50%"]);
        assert_eq!(code, 1);
        assert!(h.logs[0].1.contains("inside the current folder"));
        let (code, _) = go(&sub, &["resize", "b.png", "50%", "-o", "../out.png"]);
        assert_eq!(code, 1);
        assert!(!d.join("out.png").exists());
    }
    #[cfg(unix)]
    #[test]
    fn a_symlink_output_counts_as_existing_even_when_dangling() {
        let d = work("symlink");
        std::os::unix::fs::symlink(d.join("nowhere"), d.join("l.png")).unwrap();
        assert!(!d.join("l.png").exists());
        assert!(exists_no_follow(&d.join("l.png")));
        assert!(!exists_no_follow(&d.join("none.png")));
        make_png(&d.join("a.png"), 8, 8);
        let (code, h) = go(&d, &["resize", "a.png", "50%", "-o", "l.png"]);
        assert_eq!(code, 1);
        assert!(h.logs[0].1.contains("exists"));
        assert!(!d.join("nowhere").exists());
    }
    #[test]
    fn crop_writes_the_region_and_an_out_of_bounds_crop_exits_2_with_no_file() {
        let d = work("crop");
        make_png(&d.join("a.png"), 20, 10);
        let (code, _) = go(&d, &["crop", "a.png", "5x4+15+6", "-o", "c.png"]);
        assert_eq!(code, 0);
        assert_eq!(dims(&d.join("c.png")), (5, 4));
        for g in ["6x4+15+6", "5x5+15+6"] {
            let (code, h) = go(&d, &["crop", "a.png", g, "-o", "bad.png"]);
            assert_eq!(code, 2, "{g}");
            assert!(h.logs[0].1.contains("outside the image (20x10)"), "{:?}", h.logs);
            assert!(h.logs[1].1.starts_with("image crop"));
        }
        assert_eq!(names(&d), ["a.png", "c.png"]);
    }
    #[test]
    fn flip_keeps_the_size() {
        let d = work("flip");
        make_png(&d.join("a.png"), 6, 2);
        assert_eq!(go(&d, &["flip", "a.png", "h"]).0, 0);
        assert_eq!(dims(&d.join("a.edited.png")), (6, 2));
    }
    #[test]
    fn convert_to_webp_and_gif_and_back() {
        let d = work("webpgif");
        make_png(&d.join("a.png"), 8, 4);
        assert_eq!(go(&d, &["convert", "a.png", "a.webp"]).0, 0);
        assert_eq!(go(&d, &["convert", "a.webp", "b.gif"]).0, 0);
        assert_eq!(go(&d, &["convert", "b.gif", "c.png"]).0, 0);
        assert_eq!(dims(&d.join("c.png")), (8, 4));
    }
    #[test]
    fn an_animated_gif_warns_and_converts_its_first_frame() {
        let d = work("animwarn");
        {
            use image::codecs::gif::GifEncoder;
            let mut enc = GifEncoder::new(fs::File::create(d.join("a.gif")).unwrap());
            for _ in 0..3 {
                enc.encode_frame(image::Frame::new(RgbaImage::from_pixel(5, 5, Rgba([9, 9, 9, 255])))).unwrap();
            }
        }
        let (code, h) = go(&d, &["convert", "a.gif", "a.png"]);
        assert_eq!(code, 0);
        assert!(h.logs.iter().any(|(l, m)| l == "warn" && m.contains("first frame")), "{:?}", h.logs);
        assert_eq!(h.out.len(), 1);
    }
    #[test]
    fn compress_makes_a_smaller_jpeg_and_uses_quality_80_by_default() {
        let d = work("compress");
        let img = DynamicImage::ImageRgba8(RgbaImage::from_fn(96, 96, |x, y| Rgba([(x * 7) as u8, (y * 13) as u8, (x * y) as u8, 255])));
        img.save_with_format(d.join("a.png"), image::ImageFormat::Png).unwrap();
        assert_eq!(go(&d, &["convert", "a.png", "a.jpg"]).0, 0);
        assert_eq!(go(&d, &["compress", "a.jpg", "-o", "q30.jpg", "--quality", "30"]).0, 0);
        assert_eq!(go(&d, &["compress", "a.jpg", "-o", "q90.jpg", "--quality", "90"]).0, 0);
        assert_eq!(go(&d, &["compress", "a.jpg"]).0, 0);
        let len = |n: &str| fs::metadata(d.join(n)).unwrap().len();
        assert!(len("q30.jpg") < len("q90.jpg"));
        assert!(len("q30.jpg") < len("a.edited.jpg") && len("a.edited.jpg") < len("q90.jpg"));
    }
    #[test]
    fn quality_with_a_non_jpeg_output_exits_2_and_writes_nothing() {
        let d = work("quality-fmt");
        make_png(&d.join("a.png"), 8, 8);
        for args in [
            &["compress", "a.png", "--quality", "50"][..],
            &["compress", "a.png"],
            &["compress", "a.png", "-o", "o.webp", "--quality", "50"],
            &["edit", "a.png", "--rotate", "90", "-o", "o.webp", "--quality", "50"],
            &["edit", "a.png", "--rotate", "90", "-o", "o.gif", "--quality", "50"],
        ] {
            let (code, h) = go(&d, args);
            assert_eq!(code, 2, "{args:?}");
            assert_eq!(h.logs[0].1, "image: --quality only applies to JPEG output");
        }
        assert_eq!(names(&d), ["a.png"]);
        let (code, _) = go(&d, &["resize", "a.png", "50%", "--quality", "50"]);
        assert_eq!(code, 2);
    }
    #[test]
    fn edit_applies_operations_in_order_and_checks_crop_against_the_current_size() {
        let d = work("edit");
        make_png(&d.join("a.png"), 100, 50);
        // Crop is only valid after the resize doubles the width.
        let ok = ["edit", "a.png", "--resize", "200", "--crop", "150x10+40+0", "-o", "o.png"];
        let (code, _) = go(&d, &ok);
        assert_eq!(code, 0);
        assert_eq!(dims(&d.join("o.png")), (150, 10));
        let (code, h) = go(&d, &["edit", "a.png", "--crop", "150x10+40+0", "--resize", "200", "-o", "p.png"]);
        assert_eq!(code, 2);
        assert!(h.logs[0].1.contains("outside the image (100x50)"));
        assert!(!d.join("p.png").exists());
        // Order matters for the result: rotate then resize-to-width vs the reverse.
        go(&d, &["edit", "a.png", "--rotate", "90", "--resize", "20", "-o", "r1.png"]);
        go(&d, &["edit", "a.png", "--resize", "20", "--rotate", "90", "-o", "r2.png"]);
        assert_eq!(dims(&d.join("r1.png")), (20, 40));
        assert_eq!(dims(&d.join("r2.png")), (10, 20));
    }
    #[test]
    fn edit_summary_reports_first_and_last_sizes() {
        let d = work("edit-sum");
        make_png(&d.join("a.png"), 100, 50);
        let (_, h) = go(&d, &["edit", "a.png", "--resize", "50%", "--flip", "h", "-o", "o.png"]);
        assert!(h.out[0].starts_with("o.png: 100×50 → 50×25, "), "{}", h.out[0]);
    }
    #[test]
    fn a_missing_input_fails_before_the_overwrite_prompt() {
        let d = work("noprompt-missing");
        fs::write(d.join("o.png"), b"keep").unwrap();
        let (code, h) = go_answering(&d, &["resize", "nope.png", "50%", "-o", "o.png"], &[Some("y")]);
        assert_eq!(code, 1);
        assert!(h.prompts.is_empty());
        assert!(h.logs.iter().any(|(_, m)| m.contains("no such file")));
        assert_eq!(fs::read(d.join("o.png")).unwrap(), b"keep");
    }
    #[test]
    fn a_crop_outside_the_image_fails_before_the_overwrite_prompt() {
        let d = work("noprompt-crop");
        make_png(&d.join("a.png"), 8, 8);
        fs::write(d.join("o.png"), b"keep").unwrap();
        let (code, h) = go_answering(&d, &["crop", "a.png", "20x20+0+0", "-o", "o.png"], &[Some("y")]);
        assert_eq!(code, 2);
        assert!(h.prompts.is_empty());
        assert_eq!(fs::read(d.join("o.png")).unwrap(), b"keep");
    }
    #[test]
    fn overwrite_prompt_yes_overwrites_and_other_answers_keep_the_file() {
        let d = work("prompt");
        make_png(&d.join("a.png"), 8, 8);
        for ans in [Some("n"), Some(""), None, Some("maybe")] {
            fs::write(d.join("o.png"), b"keep").unwrap();
            let (code, h) = go_answering(&d, &["resize", "a.png", "50%", "-o", "o.png"], &[ans]);
            assert_eq!(code, 1, "{ans:?}");
            assert_eq!(h.prompts, ["Overwrite o.png? [y/N] "]);
            assert_eq!(fs::read(d.join("o.png")).unwrap(), b"keep");
            assert_eq!(names(&d), ["a.png", "o.png"]);
        }
        for ans in ["y", "YES", " yes "] {
            fs::write(d.join("o.png"), b"keep").unwrap();
            let (code, _) = go_answering(&d, &["resize", "a.png", "50%", "-o", "o.png"], &[Some(ans)]);
            assert_eq!(code, 0, "{ans}");
            assert_eq!(dims(&d.join("o.png")), (4, 4));
        }
    }
    #[test]
    fn force_skips_the_prompt() {
        let d = work("force");
        make_png(&d.join("a.png"), 8, 8);
        fs::write(d.join("o.png"), b"keep").unwrap();
        let (code, h) = go_answering(&d, &["resize", "a.png", "50%", "-o", "o.png", "--force"], &[]);
        assert_eq!(code, 0);
        assert!(h.prompts.is_empty());
    }
    #[test]
    fn writing_over_the_input_asks_first() {
        let d = work("inplace");
        make_png(&d.join("a.png"), 8, 8);
        let (code, h) = go_answering(&d, &["resize", "a.png", "50%", "-o", "./a.png"], &[Some("n")]);
        assert_eq!(code, 1);
        assert_eq!(h.prompts.len(), 1);
        assert_eq!(dims(&d.join("a.png")), (8, 8));
        let (code, _) = go_answering(&d, &["resize", "a.png", "50%", "-o", "a.png"], &[Some("y")]);
        assert_eq!(code, 0);
        assert_eq!(dims(&d.join("a.png")), (4, 4));
    }
    #[test]
    fn a_dangling_symlink_output_prompts() {
        #[cfg(unix)]
        {
            let d = work("symprompt");
            std::os::unix::fs::symlink(d.join("nowhere"), d.join("l.png")).unwrap();
            make_png(&d.join("a.png"), 8, 8);
            let (code, h) = go_answering(&d, &["resize", "a.png", "50%", "-o", "l.png"], &[Some("n")]);
            assert_eq!(code, 1);
            assert_eq!(h.prompts.len(), 1);
        }
    }
    #[test]
    fn progress_is_reported_only_for_inputs_of_4_megapixels_or_more() {
        let d = work("progress");
        make_png(&d.join("big.png"), 2000, 2000);
        make_png(&d.join("small.png"), 100, 100);
        let (code, h) = go(&d, &["resize", "big.png", "10%"]);
        assert_eq!(code, 0);
        let got: Vec<(Option<f64>, &str)> = h.progress.iter().map(|(p, m)| (*p, m.as_str())).collect();
        assert_eq!(
            got,
            [(Some(0.0), "decoding"), (Some(33.0), "editing"), (Some(66.0), "encoding"), (Some(100.0), "done")]
        );
        let (code, h) = go(&d, &["resize", "small.png", "10%"]);
        assert_eq!(code, 0);
        assert!(h.progress.is_empty());
    }
    #[test]
    fn an_oversized_image_exits_1_with_a_clear_message() {
        let d = work("toobig");
        fs::File::create(d.join("big.png")).unwrap().set_len(io::MAX_BYTES + 1).unwrap();
        let (code, h) = go(&d, &["resize", "big.png", "50%"]);
        assert_eq!(code, 1);
        assert!(h.logs[0].1.starts_with("image: big.png: image too large"), "{:?}", h.logs);
    }
    #[test]
    fn bad_inputs_exit_1() {
        let d = work("bad");
        fs::write(d.join("t.png"), b"nope").unwrap();
        let (code, h) = go(&d, &["rotate", "t.png", "90"]);
        assert_eq!(code, 1);
        assert_eq!(h.logs[0].1, "image: t.png: unsupported or unreadable image");
        let (code, h) = go(&d, &["rotate", "missing.png", "90"]);
        assert_eq!(code, 1);
        assert_eq!(h.logs[0].1, "image: missing.png: no such file");
    }
}
