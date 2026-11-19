//! One download per target at a time: an advisory lock on `<final>.part.lock`, on every OS.

use fd_lock::{RwLock, RwLockWriteGuard};
use std::fs::{File, OpenOptions};
use std::path::{Path, PathBuf};

/// Open `path` without following a symlink at its final component (O_NOFOLLOW on unix; a pre-check on windows).
pub fn open_nofollow(opts: &mut OpenOptions, path: &Path) -> std::io::Result<File> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        opts.custom_flags(libc::O_NOFOLLOW);
    }
    #[cfg(not(unix))]
    if std::fs::symlink_metadata(path).map(|m| m.file_type().is_symlink()).unwrap_or(false) {
        return Err(std::io::Error::new(std::io::ErrorKind::PermissionDenied, "refusing to open a symlink"));
    }
    opts.open(path)
}

pub struct Lock {
    // Self-referential: `guard` borrows the heap-allocated `owner`. The guard is dropped first, then the file.
    guard: Option<RwLockWriteGuard<'static, File>>,
    owner: *mut RwLock<File>,
    path: PathBuf,
}

// The raw pointer is owned by this struct alone and only touched in `release`/`drop`.
unsafe impl Send for Lock {}

impl Lock {
    /// `Ok(None)` when another process holds it.
    pub fn try_acquire(path: &Path) -> std::io::Result<Option<Lock>> {
        let file = open_nofollow(OpenOptions::new().create(true).write(true).truncate(false), path)?;
        let owner = Box::into_raw(Box::new(RwLock::new(file)));
        // SAFETY: `owner` stays allocated until `Drop` frees it, after the guard is gone.
        match unsafe { (*owner).try_write() } {
            Ok(guard) => Ok(Some(Lock { guard: Some(guard), owner, path: path.to_path_buf() })),
            Err(e) => {
                drop(unsafe { Box::from_raw(owner) });
                if e.kind() == std::io::ErrorKind::WouldBlock {
                    Ok(None)
                } else {
                    Err(e)
                }
            }
        }
    }

    /// Release the lock and remove the file (the file is closed first: windows cannot delete an open file).
    pub fn release(self) {
        let path = self.path.clone();
        drop(self);
        let _ = std::fs::remove_file(path);
    }
}

impl Drop for Lock {
    fn drop(&mut self) {
        self.guard.take();
        // SAFETY: allocated by `Box::into_raw` in `try_acquire` and freed only here.
        drop(unsafe { Box::from_raw(self.owner) });
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_held_lock_is_busy_until_released() {
        let p = std::env::temp_dir().join(format!("dock-lock-{}", std::process::id()));
        let first = Lock::try_acquire(&p).unwrap().expect("first takes it");
        assert!(Lock::try_acquire(&p).unwrap().is_none());
        first.release();
        assert!(!p.exists());
        Lock::try_acquire(&p).unwrap().expect("free again").release();
    }

    #[cfg(unix)]
    #[test]
    fn a_symlinked_lock_file_is_refused() {
        let d = std::env::temp_dir().join(format!("dock-lock-sym-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        let victim = d.join("victim");
        std::os::unix::fs::symlink(&victim, d.join("l")).unwrap();
        assert!(Lock::try_acquire(&d.join("l")).is_err());
        assert!(!victim.exists());
    }
}
