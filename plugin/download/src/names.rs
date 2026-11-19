//! Where a download lands: the `name (n)` rule, shared by every command that saves into a folder.

use crate::lock::Lock;
use std::path::{Path, PathBuf};

pub const FALLBACK: &str = "download.out";
const MAX_CANDIDATES: u32 = 999;

const RESERVED: [&str; 4] = ["CON", "PRN", "AUX", "NUL"];

fn is_reserved(name: &str) -> bool {
    let stem = name.split('.').next().unwrap_or("").trim_end_matches(' ').to_ascii_uppercase();
    RESERVED.contains(&stem.as_str())
        || ((stem.starts_with("COM") || stem.starts_with("LPT")) && stem.len() == 4 && matches!(stem.as_bytes()[3], b'1'..=b'9'))
}

/// A name that is a single, portable path segment, checked the same way on every OS: no separator, drive
/// colon or stream marker, not `.`/`..`, not empty, no control or Windows-forbidden characters, no trailing
/// dot or space, and not a Windows device name (`CON`, `NUL`, `COM1`... with or without an extension).
pub fn is_plain_name(name: &str) -> bool {
    !name.is_empty()
        && name != "."
        && name != ".."
        && !name.chars().any(|c| c.is_control() || "/\\:*?\"<>|".contains(c))
        && !name.ends_with(['.', ' '])
        && !is_reserved(name)
}

/// The last non-empty path segment of a URL, percent-decoded and cut at any decoded separator (Go's
/// `path.Base`), or `download.out` when there is none or the result is not a plain name.
pub fn url_name(url: &url::Url) -> String {
    let last = url.path_segments().and_then(|s| s.filter(|p| !p.is_empty()).last()).unwrap_or("");
    let decoded = percent_decode(last);
    let base = decoded.rsplit(['/', '\\']).find(|p| !p.is_empty()).unwrap_or("");
    if is_plain_name(base) {
        base.to_string()
    } else {
        FALLBACK.to_string()
    }
}

fn percent_decode(s: &str) -> String {
    let b = s.as_bytes();
    let mut out = Vec::with_capacity(b.len());
    let mut i = 0;
    while i < b.len() {
        if b[i] == b'%' && i + 2 < b.len() {
            if let Some(v) = std::str::from_utf8(&b[i + 1..i + 3]).ok().and_then(|h| u8::from_str_radix(h, 16).ok()) {
                out.push(v);
                i += 3;
                continue;
            }
        }
        out.push(b[i]);
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

fn candidate(name: &str, n: u32) -> String {
    if n == 0 {
        return name.to_string();
    }
    let p = Path::new(name);
    match (p.file_stem().and_then(|s| s.to_str()), p.extension().and_then(|s| s.to_str())) {
        (Some(stem), Some(ext)) => format!("{stem} ({n}).{ext}"),
        _ => format!("{name} ({n})"),
    }
}

pub fn part_of(target: &Path) -> PathBuf {
    with_suffix(target, ".part")
}

pub fn sidecar_of(target: &Path) -> PathBuf {
    with_suffix(target, ".part.json")
}

pub fn lock_of(target: &Path) -> PathBuf {
    with_suffix(target, ".part.lock")
}

fn with_suffix(target: &Path, suffix: &str) -> PathBuf {
    let mut s = target.as_os_str().to_os_string();
    s.push(suffix);
    PathBuf::from(s)
}

fn sidecar_url(target: &Path) -> Option<String> {
    let text = std::fs::read_to_string(sidecar_of(target)).ok()?;
    let v: serde_json::Value = serde_json::from_str(&text).ok()?;
    v.get("url")?.as_str().map(str::to_string)
}

/// A path that exists in any form, a dangling symlink included (a symlink must never be written through).
fn taken(p: &Path) -> bool {
    std::fs::symlink_metadata(p).is_ok()
}

fn is_regular(p: &Path) -> bool {
    std::fs::symlink_metadata(p).map(|m| m.file_type().is_file()).unwrap_or(false)
}

/// A chosen target and the lock that guards it.
pub struct Picked {
    pub path: PathBuf,
    pub lock: Lock,
}

/// Pick and lock the file to write in `dir`: `name`, `stem (1).ext`, `stem (2).ext`, up to 999.
/// A candidate whose regular `.part` has a sidecar naming the same `url` is the resume candidate (busy lock
/// there means this URL is already downloading); otherwise the first candidate where neither the file nor its
/// `.part` exists and whose lock is free. The lock is taken inside the loop, so two downloads cannot pick one name.
pub fn pick_target(dir: &Path, name: &str, url: &str) -> anyhow::Result<Picked> {
    for n in 0..=MAX_CANDIDATES {
        let path = dir.join(candidate(name, n));
        let part = part_of(&path);
        let lock_path = lock_of(&path);
        if taken(&lock_path) && !is_regular(&lock_path) {
            continue;
        }
        let resume = is_regular(&part) && sidecar_url(&path).as_deref() == Some(url);
        if !resume && (taken(&path) || taken(&part)) {
            continue;
        }
        match Lock::try_acquire(&lock_path)? {
            Some(lock) => {
                // re-check under the lock: another download may have finished or started in between
                if resume || (!taken(&path) && !taken(&part)) {
                    return Ok(Picked { path, lock });
                }
                lock.release();
            }
            None if resume => anyhow::bail!("already downloading {}", path.file_name().and_then(|n| n.to_str()).unwrap_or(name)),
            None => {}
        }
    }
    anyhow::bail!("no free name for {name} in {}", dir.display())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tmp(tag: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("dock-names-{tag}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    #[test]
    fn free_name_is_used_as_is() {
        let d = tmp("free");
        assert_eq!(pick_target(&d, "a.iso", "u").unwrap().path, d.join("a.iso"));
    }

    #[test]
    fn existing_file_or_part_gives_a_numbered_name() {
        let d = tmp("num");
        std::fs::write(d.join("a.iso"), "x").unwrap();
        std::fs::write(d.join("a (1).iso.part"), "x").unwrap();
        assert_eq!(pick_target(&d, "a.iso", "u").unwrap().path, d.join("a (2).iso"));
    }

    #[test]
    fn name_without_extension_appends_the_number() {
        let d = tmp("noext");
        std::fs::write(d.join("README"), "x").unwrap();
        assert_eq!(pick_target(&d, "README", "u").unwrap().path, d.join("README (1)"));
    }

    #[test]
    fn a_part_with_the_same_url_is_reused() {
        let d = tmp("reuse");
        std::fs::write(d.join("a.iso.part"), "x").unwrap();
        std::fs::write(d.join("a.iso.part.json"), r#"{"url":"u"}"#).unwrap();
        assert_eq!(pick_target(&d, "a.iso", "u").unwrap().path, d.join("a.iso"));
        assert_eq!(pick_target(&d, "a.iso", "other").unwrap().path, d.join("a (1).iso"));
    }

    #[test]
    fn gives_up_after_999() {
        let d = tmp("many");
        std::fs::write(d.join("a"), "x").unwrap();
        for n in 1..=999 {
            std::fs::write(d.join(format!("a ({n})")), "x").unwrap();
        }
        assert!(pick_target(&d, "a", "u").is_err());
    }

    #[test]
    fn url_names() {
        let n = |s: &str| url_name(&url::Url::parse(s).unwrap());
        assert_eq!(n("http://h/x/file%20one.bin?q=1"), "file one.bin");
        assert_eq!(n("http://h/"), FALLBACK);
        assert_eq!(n("http://h"), FALLBACK);
        assert_eq!(n("http://h/a%2Fb"), "b");
        assert_eq!(n("http://h/dir/"), "dir");
        assert_eq!(n("http://h/%zz"), "%zz");
        assert_eq!(n("http://h/a/%2e%2e"), FALLBACK);
        assert_eq!(n("http://h/..%2f"), FALLBACK);
        assert_eq!(n("http://h/C:foo"), FALLBACK);
        assert_eq!(n("http://h/a%3Ab"), FALLBACK);
        assert_eq!(n("http://h/NUL.txt"), FALLBACK);
    }

    #[test]
    fn unsafe_names_are_rejected_on_every_os() {
        for bad in ["C:foo", "a:b", "CON", "con", "NUL.txt", "com1", "LPT9.tar.gz", "AUX ", "x.", "x ", "a\nb", "a\x07b", "a/b", "a\\b", "", ".", "..", "a*b", "a?b", "a|b", "a<b", "a\"b"] {
            assert!(!is_plain_name(bad), "{bad:?} should be rejected");
        }
        for ok in ["a.iso", "file one.bin", "console.txt", "com10", "nul2", "%zz", ".hidden", "a (1).iso"] {
            assert!(is_plain_name(ok), "{ok:?} should be accepted");
        }
    }

    #[cfg(unix)]
    #[test]
    fn a_dangling_symlink_counts_as_taken() {
        let d = tmp("dangle");
        std::os::unix::fs::symlink("/nonexistent/x", d.join("a.iso")).unwrap();
        std::os::unix::fs::symlink("/nonexistent/y", d.join("a (1).iso.part")).unwrap();
        let p = pick_target(&d, "a.iso", "u").unwrap();
        assert_eq!(p.path, d.join("a (2).iso"));
    }

    #[cfg(unix)]
    #[test]
    fn a_symlinked_lock_file_is_taken() {
        let d = tmp("symlock");
        std::os::unix::fs::symlink("/nonexistent/z", d.join("a.iso.part.lock")).unwrap();
        assert_eq!(pick_target(&d, "a.iso", "u").unwrap().path, d.join("a (1).iso"));
    }

    #[cfg(unix)]
    #[test]
    fn a_symlinked_part_with_the_same_sidecar_is_not_resumed() {
        let d = tmp("sympart");
        std::fs::write(d.join("real"), "x").unwrap();
        std::os::unix::fs::symlink(d.join("real"), d.join("a.iso.part")).unwrap();
        std::fs::write(d.join("a.iso.part.json"), r#"{"url":"u"}"#).unwrap();
        assert_eq!(pick_target(&d, "a.iso", "u").unwrap().path, d.join("a (1).iso"));
    }

    #[test]
    fn a_busy_candidate_is_skipped_but_the_same_url_resume_is_busy() {
        let d = tmp("busy");
        let first = pick_target(&d, "a.iso", "u1").unwrap();
        let second = pick_target(&d, "a.iso", "u2").unwrap();
        assert_eq!(first.path, d.join("a.iso"));
        assert_eq!(second.path, d.join("a (1).iso"));
        // the first download left a resumable part for u1 and still holds its lock
        std::fs::write(d.join("a.iso.part"), "x").unwrap();
        std::fs::write(d.join("a.iso.part.json"), r#"{"url":"u1"}"#).unwrap();
        assert!(pick_target(&d, "a.iso", "u1").is_err());
    }
}
