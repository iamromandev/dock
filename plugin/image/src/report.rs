//! The one-line summary of a finished operation.

/// B, KB (whole) or MB (1 decimal).
pub fn human_size(bytes: u64) -> String {
    const KB: u64 = 1024;
    const MB: u64 = 1024 * 1024;
    if bytes < KB {
        format!("{bytes} B")
    } else if bytes < MB - KB / 2 {
        format!("{} KB", (bytes + KB / 2) / KB)
    } else {
        format!("{:.1} MB", bytes as f64 / MB as f64)
    }
}

pub fn summary(out: &str, before: (u32, u32), after: (u32, u32), size0: u64, size1: u64) -> String {
    format!(
        "{out}: {}×{} → {}×{}, {} → {}",
        before.0,
        before.1,
        after.0,
        after.1,
        human_size(size0),
        human_size(size1)
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sizes_use_b_kb_mb() {
        assert_eq!(human_size(0), "0 B");
        assert_eq!(human_size(1023), "1023 B");
        assert_eq!(human_size(1024), "1 KB");
        assert_eq!(human_size(610 * 1024), "610 KB");
        assert_eq!(human_size(1_992_294), "1.9 MB");
        assert_eq!(human_size(1024 * 1024), "1.0 MB");
        assert_eq!(human_size(1024 * 1024 - 1), "1.0 MB");
    }
    #[test]
    fn summary_line_matches_the_spec() {
        assert_eq!(
            summary("shot-small.png", (2880, 1800), (1440, 900), 1_992_294, 610 * 1024),
            "shot-small.png: 2880×1800 → 1440×900, 1.9 MB → 610 KB"
        );
    }
}
