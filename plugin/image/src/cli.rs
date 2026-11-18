//! Command line: argv -> Command. Hand-written so flags keep their meaning anywhere after the operation.

use crate::geometry::{parse_crop, parse_size, Size};
use crate::ops::{Axis, Op};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    Resize,
    Crop,
    Rotate,
    Flip,
    Convert,
    Compress,
    Edit,
}

impl Kind {
    fn from_name(s: &str) -> Option<Kind> {
        Some(match s {
            "resize" => Kind::Resize,
            "crop" => Kind::Crop,
            "rotate" => Kind::Rotate,
            "flip" => Kind::Flip,
            "convert" => Kind::Convert,
            "compress" => Kind::Compress,
            "edit" => Kind::Edit,
            _ => return None,
        })
    }

    /// The usage line of this operation.
    pub fn usage(self) -> &'static str {
        match self {
            Kind::Resize => "image resize   <in> <size>            [-o out] [--force]   size: W | xH | WxH | WxH! | N%",
            Kind::Crop => "image crop     <in> <W>x<H>+<X>+<Y>   [-o out] [--force]",
            Kind::Rotate => "image rotate   <in> 90|180|270        [-o out] [--force]",
            Kind::Flip => "image flip     <in> h|v               [-o out] [--force]",
            Kind::Convert => "image convert  <in> <out>                      [--force]",
            Kind::Compress => "image compress <in> [--quality N]     [-o out] [--force]   N: 1-100, default 80",
            Kind::Edit => "image edit     <in> (--resize S | --crop G | --rotate D | --flip h|v)... [--quality N] -o out [--force]",
        }
    }
}

pub const USAGE: &str = "\
usage:
  image resize   <in> <size>            [-o out] [--force]   size: W | xH | WxH | WxH! | N%
  image crop     <in> <W>x<H>+<X>+<Y>   [-o out] [--force]
  image rotate   <in> 90|180|270        [-o out] [--force]
  image flip     <in> h|v               [-o out] [--force]
  image convert  <in> <out>                      [--force]
  image compress <in> [--quality N]     [-o out] [--force]   N: 1-100, default 80
  image edit     <in> (--resize S | --crop G | --rotate D | --flip h|v)... [--quality N] -o out [--force]

example: image resize photo.jpg 50% -o small.jpg

Reads and writes PNG, JPEG, WebP and GIF inside the current folder only. Without -o the result is
<name>.edited.<ext> next to the input. Metadata is dropped and EXIF rotation is not applied.
";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UsageError {
    pub op: Option<Kind>,
    pub message: String,
}

impl UsageError {
    pub fn new(op: Option<Kind>, message: impl Into<String>) -> Self {
        UsageError { op, message: message.into() }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Command {
    pub input: String,
    pub output: Option<String>,
    pub ops: Vec<Op>,
    pub quality: Option<u8>,
    pub force: bool,
    pub kind: Kind,
}

#[derive(Debug, PartialEq, Eq)]
pub enum Parsed {
    Help,
    Run(Command),
}

const DEFAULT_COMPRESS_QUALITY: u8 = 80;

/// The value of `--flag v` or `--flag=v`.
fn take_value<'a>(
    inline: Option<String>,
    name: &str,
    rest: &mut impl Iterator<Item = &'a String>,
) -> Result<String, String> {
    match inline {
        Some(v) => Ok(v),
        None => rest.next().cloned().ok_or_else(|| format!("{name} needs a value")),
    }
}

fn parse_rotate(v: &str) -> Result<Op, String> {
    match v {
        "90" | "180" | "270" => Ok(Op::Rotate(v.parse().unwrap())),
        other => Err(format!("rotate takes 90, 180 or 270, got '{other}'")),
    }
}

fn parse_flip(v: &str) -> Result<Op, String> {
    match v {
        "h" => Ok(Op::Flip(Axis::H)),
        "v" => Ok(Op::Flip(Axis::V)),
        other => Err(format!("flip takes h or v, got '{other}'")),
    }
}

pub fn parse(args: &[String]) -> Result<Parsed, UsageError> {
    let Some(first) = args.first() else {
        return Err(UsageError::new(None, "missing operation"));
    };
    if matches!(first.as_str(), "help" | "--help" | "-h") {
        return Ok(Parsed::Help);
    }
    let Some(kind) = Kind::from_name(first) else {
        return Err(UsageError::new(None, format!("unknown operation '{first}'")));
    };
    let err = |m: String| UsageError::new(Some(kind), m);

    let mut positional: Vec<&str> = Vec::new();
    let mut output: Option<String> = None;
    let mut force = false;
    let mut quality: Option<u8> = None;
    let mut ops: Vec<Op> = Vec::new();
    let mut rest = args[1..].iter();
    while let Some(arg) = rest.next() {
        if arg == "--" {
            positional.extend(rest.by_ref().map(String::as_str));
            break;
        }
        if !arg.starts_with('-') || arg == "-" {
            positional.push(arg);
            continue;
        }
        let (name, inline) = match arg.split_once('=') {
            Some((n, v)) if n.starts_with("--") => (n, Some(v.to_string())),
            _ => (arg.as_str(), None),
        };
        match name {
            "--force" if inline.is_none() => force = true,
            "-o" | "--output" if kind != Kind::Convert => {
                let value = take_value(inline, name, &mut rest).map_err(err)?;
                if output.replace(value).is_some() {
                    return Err(err("-o given more than once".into()));
                }
            }
            "--quality" if matches!(kind, Kind::Compress | Kind::Edit) => {
                let v = take_value(inline, name, &mut rest).map_err(err)?;
                match v.parse::<u8>() {
                    Ok(q @ 1..=100) if v.bytes().all(|b| b.is_ascii_digit()) => quality = Some(q),
                    _ => return Err(err(format!("--quality takes 1-100, got '{v}'"))),
                }
            }
            "--quality" => return Err(err("--quality is not available here (use compress or edit)".into())),
            "--resize" | "--crop" | "--rotate" | "--flip" if kind == Kind::Edit => {
                let v = take_value(inline, name, &mut rest).map_err(err)?;
                let op = match name {
                    "--resize" => parse_size(&v).map(Op::Resize),
                    "--crop" => parse_crop(&v).map(Op::Crop),
                    "--rotate" => parse_rotate(&v),
                    _ => parse_flip(&v),
                };
                ops.push(op.map_err(err)?);
            }
            _ => return Err(err(format!("unknown flag '{name}'"))),
        }
    }

    let need = |n: usize| -> Result<(), UsageError> {
        match positional.len().cmp(&n) {
            std::cmp::Ordering::Less => Err(err("missing argument".into())),
            std::cmp::Ordering::Greater => Err(err(format!("unexpected argument '{}'", positional[n]))),
            std::cmp::Ordering::Equal => Ok(()),
        }
    };
    let mut cmd = Command { input: String::new(), output, ops, quality, force, kind };
    match kind {
        Kind::Resize => {
            need(2)?;
            let size: Size = parse_size(positional[1]).map_err(err)?;
            cmd.ops.push(Op::Resize(size));
        }
        Kind::Rotate => {
            need(2)?;
            cmd.ops.push(parse_rotate(positional[1]).map_err(err)?);
        }
        Kind::Crop => {
            need(2)?;
            cmd.ops.push(Op::Crop(parse_crop(positional[1]).map_err(err)?));
        }
        Kind::Flip => {
            need(2)?;
            cmd.ops.push(parse_flip(positional[1]).map_err(err)?);
        }
        Kind::Compress => {
            need(1)?;
            cmd.quality.get_or_insert(DEFAULT_COMPRESS_QUALITY);
        }
        Kind::Edit => {
            need(1)?;
            if cmd.ops.is_empty() {
                return Err(err("give at least one of --resize, --crop, --rotate, --flip".into()));
            }
            if cmd.output.is_none() {
                return Err(err("edit needs -o <out>".into()));
            }
        }
        Kind::Convert => {
            need(2)?;
            cmd.output = Some(positional[1].to_string());
        }
    }
    cmd.input = positional[0].to_string();
    Ok(Parsed::Run(cmd))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::geometry::CropGeom;

    fn p(args: &[&str]) -> Result<Parsed, UsageError> {
        parse(&args.iter().map(|s| s.to_string()).collect::<Vec<_>>())
    }
    fn cmd(args: &[&str]) -> Command {
        match p(args) {
            Ok(Parsed::Run(c)) => c,
            other => panic!("expected a command, got {other:?}"),
        }
    }

    #[test]
    fn help_forms() {
        for a in ["help", "--help", "-h"] {
            assert_eq!(p(&[a]), Ok(Parsed::Help));
        }
    }
    #[test]
    fn bare_is_a_usage_error() {
        assert_eq!(p(&[]).unwrap_err().op, None);
    }
    #[test]
    fn unknown_operation() {
        let e = p(&["blur", "a.png"]).unwrap_err();
        assert_eq!(e.op, None);
        assert!(e.message.contains("blur"));
    }
    #[test]
    fn usage_lists_every_operation() {
        for k in ["resize", "crop", "rotate", "flip", "convert", "compress", "edit"] {
            assert!(USAGE.contains(&format!("image {k}")), "{k}");
        }
    }
    #[test]
    fn resize_parses_with_output_and_force() {
        let c = cmd(&["resize", "a.png", "50%", "-o", "b.png", "--force"]);
        assert_eq!(c.input, "a.png");
        assert_eq!(c.output.as_deref(), Some("b.png"));
        assert!(c.force);
        assert_eq!(c.ops, vec![Op::Resize(Size::Percent(50))]);
    }
    #[test]
    fn flags_may_come_first_and_use_equals() {
        let c = cmd(&["resize", "--output=b.png", "a.png", "800"]);
        assert_eq!(c.output.as_deref(), Some("b.png"));
        assert_eq!(c.ops, vec![Op::Resize(Size::Width(800))]);
        assert_eq!(cmd(&["resize", "--output", "b.png", "a.png", "800"]).output.as_deref(), Some("b.png"));
    }
    #[test]
    fn double_dash_ends_flags() {
        let c = cmd(&["resize", "--", "-a.png", "50%"]);
        assert_eq!(c.input, "-a.png");
    }
    #[test]
    fn rotate_accepts_only_right_angles() {
        assert_eq!(cmd(&["rotate", "a.png", "270"]).ops, vec![Op::Rotate(270)]);
        assert_eq!(p(&["rotate", "a.png", "45"]).unwrap_err().op, Some(Kind::Rotate));
    }
    #[test]
    fn convert_takes_output_positionally() {
        let c = cmd(&["convert", "a.png", "a.jpg"]);
        assert_eq!(c.output.as_deref(), Some("a.jpg"));
        assert!(c.ops.is_empty());
        assert!(p(&["convert", "a.png", "-o", "x.jpg"]).is_err());
    }
    #[test]
    fn missing_and_extra_positionals() {
        assert!(p(&["resize", "a.png"]).is_err());
        assert!(p(&["resize"]).is_err());
        assert!(p(&["resize", "a.png", "50%", "extra"]).is_err());
    }
    #[test]
    fn unknown_flags_and_repeated_output() {
        assert!(p(&["resize", "a.png", "50%", "--bogus"]).is_err());
        assert!(p(&["resize", "a.png", "50%", "-o", "a", "-o", "b"]).is_err());
        assert!(p(&["resize", "a.png", "50%", "-o"]).is_err());
        assert!(p(&["resize", "a.png", "50%", "--force=1"]).is_err());
    }
    #[test]
    fn bad_size_is_a_usage_error() {
        assert!(p(&["resize", "a.png", "0%"]).is_err());
    }
    #[test]
    fn crop_and_flip_parse() {
        let c = cmd(&["crop", "a.png", "10x20+3+4", "-o", "c.png"]);
        assert_eq!(c.ops, vec![Op::Crop(CropGeom { w: 10, h: 20, x: 3, y: 4 })]);
        assert_eq!(cmd(&["flip", "a.png", "h"]).ops, vec![Op::Flip(Axis::H)]);
        assert_eq!(cmd(&["flip", "a.png", "v"]).ops, vec![Op::Flip(Axis::V)]);
        assert_eq!(p(&["flip", "a.png", "x"]).unwrap_err().op, Some(Kind::Flip));
        assert_eq!(p(&["crop", "a.png", "10x20"]).unwrap_err().op, Some(Kind::Crop));
    }
    #[test]
    fn compress_defaults_to_quality_80_and_takes_1_to_100() {
        assert_eq!(cmd(&["compress", "a.jpg"]).quality, Some(80));
        assert_eq!(cmd(&["compress", "a.jpg", "--quality", "30"]).quality, Some(30));
        assert_eq!(cmd(&["compress", "a.jpg", "--quality=100"]).quality, Some(100));
        for bad in ["0", "101", "x", "-5", ""] {
            assert_eq!(p(&["compress", "a.jpg", "--quality", bad]).unwrap_err().op, Some(Kind::Compress), "{bad}");
        }
        assert!(p(&["compress", "a.jpg", "--quality"]).is_err());
    }
    #[test]
    fn quality_is_refused_on_the_other_operations() {
        for a in [&["resize", "a.png", "50%"][..], &["crop", "a.png", "1x1+0+0"], &["rotate", "a.png", "90"], &["flip", "a.png", "h"]] {
            let mut v = a.to_vec();
            v.extend(["--quality", "50"]);
            assert!(p(&v).is_err(), "{v:?}");
        }
    }
    #[test]
    fn edit_keeps_the_order_and_allows_repeats() {
        let c = cmd(&["edit", "a.png", "--resize", "50%", "--crop=2x2+0+0", "--rotate", "90", "--flip", "h", "--resize", "10", "-o", "o.png"]);
        assert_eq!(
            c.ops,
            vec![
                Op::Resize(Size::Percent(50)),
                Op::Crop(CropGeom { w: 2, h: 2, x: 0, y: 0 }),
                Op::Rotate(90),
                Op::Flip(Axis::H),
                Op::Resize(Size::Width(10)),
            ]
        );
        assert_eq!(c.output.as_deref(), Some("o.png"));
        assert_eq!(c.quality, None);
        assert_eq!(cmd(&["edit", "a.png", "--rotate", "90", "-o", "o.jpg", "--quality", "70"]).quality, Some(70));
    }
    #[test]
    fn edit_needs_an_operation_and_an_output_and_valid_values() {
        assert_eq!(p(&["edit", "a.png", "-o", "o.png"]).unwrap_err().op, Some(Kind::Edit));
        assert_eq!(p(&["edit", "a.png", "--rotate", "90"]).unwrap_err().op, Some(Kind::Edit));
        assert!(p(&["edit", "a.png", "--rotate", "45", "-o", "o.png"]).is_err());
        assert!(p(&["edit", "a.png", "--flip", "z", "-o", "o.png"]).is_err());
        assert!(p(&["edit", "a.png", "--resize", "0%", "-o", "o.png"]).is_err());
        assert!(p(&["edit", "a.png", "--crop", "bad", "-o", "o.png"]).is_err());
        assert!(p(&["edit", "a.png", "--resize", "-o", "o.png"]).is_err());
        assert!(p(&["edit", "a.png", "b.png", "--rotate", "90", "-o", "o.png"]).is_err());
    }
    #[test]
    fn op_flags_are_refused_outside_edit() {
        assert!(p(&["resize", "a.png", "50%", "--rotate", "90"]).is_err());
    }
}
