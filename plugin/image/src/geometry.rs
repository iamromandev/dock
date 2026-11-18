//! Size and crop geometry: parsing and resize math, all pure.

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Size {
    Width(u32),
    Height(u32),
    Fit(u32, u32),
    Exact(u32, u32),
    Percent(u32),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct CropGeom {
    pub w: u32,
    pub h: u32,
    pub x: u32,
    pub y: u32,
}

fn dim(s: &str) -> Option<u32> {
    if s.is_empty() || !s.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    s.parse::<u32>().ok().filter(|&n| n >= 1)
}

pub fn parse_size(s: &str) -> Result<Size, String> {
    let bad = || format!("bad size '{s}' (use W, xH, WxH, WxH! or N%)");
    if let Some(p) = s.strip_suffix('%') {
        let n = dim(p).ok_or_else(bad)?;
        if !(1..=1000).contains(&n) {
            return Err(format!("percent must be 1-1000, got '{s}'"));
        }
        return Ok(Size::Percent(n));
    }
    let Some((w, rest)) = s.split_once('x') else {
        return dim(s).map(Size::Width).ok_or_else(bad);
    };
    let (h, exact) = match rest.strip_suffix('!') {
        Some(h) => (h, true),
        None => (rest, false),
    };
    match (w.is_empty(), dim(w), dim(h)) {
        (true, _, Some(h)) if !exact => Ok(Size::Height(h)),
        (false, Some(w), Some(h)) => Ok(if exact { Size::Exact(w, h) } else { Size::Fit(w, h) }),
        _ => Err(bad()),
    }
}

pub fn parse_crop(s: &str) -> Result<CropGeom, String> {
    let bad = || format!("bad crop '{s}' (use WxH+X+Y)");
    let (size, offs) = s.split_once('+').ok_or_else(bad)?;
    let (w, h) = size.split_once('x').ok_or_else(bad)?;
    let (x, y) = offs.split_once('+').ok_or_else(bad)?;
    let off = |v: &str| {
        if v.is_empty() || !v.bytes().all(|b| b.is_ascii_digit()) { None } else { v.parse::<u32>().ok() }
    };
    match (dim(w), dim(h), off(x), off(y)) {
        (Some(w), Some(h), Some(x), Some(y)) => Ok(CropGeom { w, h, x, y }),
        _ => Err(bad()),
    }
}

fn scaled(v: u32, num: u64, den: u64) -> u32 {
    let r = (v as u64 * num * 2 + den) / (den * 2); // round half up
    r.clamp(1, u32::MAX as u64) as u32
}

/// The output size for a source size, rounded to whole pixels and at least 1x1.
pub fn target_dims(src: (u32, u32), s: &Size) -> (u32, u32) {
    let (sw, sh) = (src.0.max(1), src.1.max(1));
    match *s {
        Size::Width(w) => (w.max(1), scaled(sh, w as u64, sw as u64)),
        Size::Height(h) => (scaled(sw, h as u64, sh as u64), h.max(1)),
        Size::Exact(w, h) => (w.max(1), h.max(1)),
        Size::Percent(n) => (scaled(sw, n as u64, 100), scaled(sh, n as u64, 100)),
        Size::Fit(w, h) => {
            // Compare w/sw with h/sh without floats: the smaller ratio limits the size.
            if (w as u64) * (sh as u64) <= (h as u64) * (sw as u64) {
                (w.max(1), scaled(sh, w as u64, sw as u64))
            } else {
                (scaled(sw, h as u64, sh as u64), h.max(1))
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const SRC: (u32, u32) = (1000, 500);

    #[test]
    fn ac3_width_gives_800x400() {
        assert_eq!(target_dims(SRC, &parse_size("800").unwrap()), (800, 400));
    }
    #[test]
    fn ac3_height_gives_200x100() {
        assert_eq!(target_dims(SRC, &parse_size("x100").unwrap()), (200, 100));
    }
    #[test]
    fn ac3_fit_gives_300x150() {
        assert_eq!(target_dims(SRC, &parse_size("300x300").unwrap()), (300, 150));
    }
    #[test]
    fn ac3_exact_gives_300x300() {
        assert_eq!(target_dims(SRC, &parse_size("300x300!").unwrap()), (300, 300));
    }
    #[test]
    fn ac3_percent_gives_100x50() {
        assert_eq!(target_dims(SRC, &parse_size("10%").unwrap()), (100, 50));
    }
    #[test]
    fn fit_limited_by_height() {
        assert_eq!(target_dims((500, 1000), &Size::Fit(300, 300)), (150, 300));
    }
    #[test]
    fn tiny_results_are_at_least_1x1() {
        assert_eq!(target_dims((10, 3), &Size::Percent(1)), (1, 1));
        assert_eq!(target_dims((1000, 1), &Size::Width(10)), (10, 1));
    }
    #[test]
    fn percent_bounds() {
        assert!(parse_size("0%").is_err());
        assert!(parse_size("1001%").is_err());
        assert_eq!(parse_size("1000%"), Ok(Size::Percent(1000)));
        assert_eq!(parse_size("1%"), Ok(Size::Percent(1)));
    }
    #[test]
    fn garbage_sizes_are_rejected() {
        for s in ["", "x", "0", "0x5", "5x0", "abc", "-5", "10x", "x!", "x5!", "1.5", "5%%", "%", "3x3!!", "99999999999"] {
            assert!(parse_size(s).is_err(), "{s} should fail");
        }
    }
    #[test]
    fn crop_parses() {
        assert_eq!(parse_crop("50x40+10+20"), Ok(CropGeom { w: 50, h: 40, x: 10, y: 20 }));
        assert_eq!(parse_crop("5x5+0+0"), Ok(CropGeom { w: 5, h: 5, x: 0, y: 0 }));
    }
    #[test]
    fn garbage_crops_are_rejected() {
        for s in ["", "50x50", "50x50+1", "0x5+0+0", "ax5+0+0", "5x5+-1+0", "5x5+0+0+0", "+0+0"] {
            assert!(parse_crop(s).is_err(), "{s} should fail");
        }
    }
}
