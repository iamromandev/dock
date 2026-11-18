//! Pure image operations.

use crate::cli::{Kind, UsageError};
use crate::io::{Fail, MAX_PIXELS};
use crate::geometry::{target_dims, CropGeom, Size};
use image::imageops::FilterType;
use image::DynamicImage;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Axis {
    H,
    V,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Op {
    Resize(Size),
    Crop(CropGeom),
    Rotate(u16),
    Flip(Axis),
}

pub const GIF_MAX_SIDE: u32 = 65535;

pub fn gif_side_failure((w, h): (u32, u32)) -> Fail {
    Fail::new(format!("result too large ({w}×{h}, limit {GIF_MAX_SIDE} pixels per side for GIF)"))
}

/// Refuse a resize whose result would exceed the pixel limit (or the GIF side limit), before allocating it.
pub fn check_resize(src: (u32, u32), op: &Op, gif: bool) -> Result<(), Fail> {
    let Op::Resize(size) = op else { return Ok(()) };
    let (w, h) = target_dims(src, size);
    if w as u64 * h as u64 > MAX_PIXELS {
        return Err(Fail::new(format!("result too large ({w}×{h}, limit {} megapixels)", MAX_PIXELS / 1_000_000)));
    }
    if gif && (w > GIF_MAX_SIDE || h > GIF_MAX_SIDE) {
        return Err(gif_side_failure((w, h)));
    }
    Ok(())
}

pub fn apply(op: &Op, img: DynamicImage) -> Result<DynamicImage, UsageError> {
    match *op {
        Op::Resize(size) => {
            let (w, h) = target_dims((img.width(), img.height()), &size);
            Ok(img.resize_exact(w, h, FilterType::Lanczos3))
        }
        Op::Rotate(90) => Ok(img.rotate90()),
        Op::Rotate(180) => Ok(img.rotate180()),
        Op::Rotate(270) => Ok(img.rotate270()),
        Op::Rotate(d) => Err(UsageError::new(Some(Kind::Rotate), format!("rotate takes 90, 180 or 270, got '{d}'"))),
        Op::Crop(g) => {
            let (w, h) = (img.width(), img.height());
            if g.x as u64 + g.w as u64 > w as u64 || g.y as u64 + g.h as u64 > h as u64 {
                return Err(UsageError::new(
                    Some(Kind::Crop),
                    format!("crop {}x{}+{}+{} is outside the image ({w}x{h})", g.w, g.h, g.x, g.y),
                ));
            }
            Ok(img.crop_imm(g.x, g.y, g.w, g.h))
        }
        Op::Flip(Axis::H) => Ok(img.fliph()),
        Op::Flip(Axis::V) => Ok(img.flipv()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use image::{Rgba, RgbaImage};

    /// 3x2 image whose pixels are all different: pixel (x, y) = [x*10, y*10, 0, 255].
    fn fixture() -> DynamicImage {
        DynamicImage::ImageRgba8(RgbaImage::from_fn(3, 2, |x, y| Rgba([x as u8 * 10, y as u8 * 10, 0, 255])))
    }

    #[test]
    fn rotate_90_is_clockwise_and_exact() {
        let r = apply(&Op::Rotate(90), fixture()).unwrap().to_rgba8();
        assert_eq!((r.width(), r.height()), (2, 3));
        // Clockwise: the old bottom-left (0,1) becomes the new top-left (0,0).
        assert_eq!(r.get_pixel(0, 0), &Rgba([0, 10, 0, 255]));
        // The old top-left (0,0) becomes the new top-right (1,0).
        assert_eq!(r.get_pixel(1, 0), &Rgba([0, 0, 0, 255]));
        // The old top-right (2,0) becomes the new bottom-right (1,2).
        assert_eq!(r.get_pixel(1, 2), &Rgba([20, 0, 0, 255]));
    }
    #[test]
    fn four_quarter_turns_reproduce_the_original() {
        let mut img = fixture();
        for _ in 0..4 {
            img = apply(&Op::Rotate(90), img).unwrap();
        }
        assert_eq!(img.to_rgba8(), fixture().to_rgba8());
    }
    #[test]
    fn rotate_180_and_270_compose() {
        let a = apply(&Op::Rotate(270), fixture()).unwrap();
        let b = apply(&Op::Rotate(90), apply(&Op::Rotate(180), fixture()).unwrap()).unwrap();
        assert_eq!(a.to_rgba8(), b.to_rgba8());
    }
    #[test]
    fn crop_takes_the_exact_region() {
        let r = apply(&Op::Crop(CropGeom { w: 2, h: 1, x: 1, y: 1 }), fixture()).unwrap().to_rgba8();
        assert_eq!((r.width(), r.height()), (2, 1));
        assert_eq!(r.get_pixel(0, 0), &Rgba([10, 10, 0, 255]));
        assert_eq!(r.get_pixel(1, 0), &Rgba([20, 10, 0, 255]));
    }
    #[test]
    fn crop_touching_the_edge_is_allowed_and_beyond_it_is_a_usage_error() {
        assert!(apply(&Op::Crop(CropGeom { w: 3, h: 2, x: 0, y: 0 }), fixture()).is_ok());
        for g in [CropGeom { w: 3, h: 2, x: 1, y: 0 }, CropGeom { w: 1, h: 3, x: 0, y: 0 }, CropGeom { w: 1, h: 1, x: 0, y: 2 }] {
            let e = apply(&Op::Crop(g), fixture()).unwrap_err();
            assert!(e.message.contains("outside the image (3x2)"), "{}", e.message);
        }
    }
    #[test]
    fn flips_are_exact_and_self_inverse() {
        let h = apply(&Op::Flip(Axis::H), fixture()).unwrap().to_rgba8();
        assert_eq!(h.get_pixel(0, 0), &Rgba([20, 0, 0, 255]));
        let v = apply(&Op::Flip(Axis::V), fixture()).unwrap().to_rgba8();
        assert_eq!(v.get_pixel(0, 0), &Rgba([0, 10, 0, 255]));
        for axis in [Axis::H, Axis::V] {
            let twice = apply(&Op::Flip(axis), apply(&Op::Flip(axis), fixture()).unwrap()).unwrap();
            assert_eq!(twice.to_rgba8(), fixture().to_rgba8());
        }
    }
    #[test]
    fn a_resize_past_64_megapixels_is_refused_before_it_runs() {
        let e = check_resize((8000, 8000), &Op::Resize(Size::Percent(1000)), false).unwrap_err();
        assert!(e.message.contains("result too large (80000×80000, limit 64 megapixels)"), "{}", e.message);
        let e = check_resize((3, 2), &Op::Resize(Size::Exact(200_000, 200_000)), false).unwrap_err();
        assert!(e.message.contains("limit 64 megapixels"));
        assert!(check_resize((3, 2), &Op::Resize(Size::Exact(8000, 8000)), false).is_ok());
    }
    #[test]
    fn a_gif_side_over_65535_is_refused() {
        let e = check_resize((3, 2), &Op::Resize(Size::Exact(70_000, 10)), true).unwrap_err();
        assert!(e.message.contains("65535"), "{}", e.message);
        assert!(check_resize((3, 2), &Op::Resize(Size::Exact(70_000, 10)), false).is_ok());
    }
    #[test]
    fn resize_uses_target_dims() {
        let r = apply(&Op::Resize(Size::Width(6)), fixture()).unwrap();
        assert_eq!((r.width(), r.height()), (6, 4));
    }
}
