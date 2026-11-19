//! The `download` command: http(s) with resume, and file URLs.

use crate::lock::Lock;
use crate::names::{self, is_plain_name};
use crate::rpc;
use anyhow::anyhow;
use futures_util::StreamExt;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};
use tokio::io::{AsyncSeekExt, AsyncWriteExt};
use tokio_util::sync::CancellationToken;

/// Why a command ended without a result: the JSON-RPC code and message to answer with.
#[derive(Debug)]
pub struct Failure {
    pub code: i64,
    pub message: String,
}

impl Failure {
    pub fn cancelled() -> Self {
        Failure { code: -32800, message: "cancelled".into() }
    }
    pub fn invalid(message: impl Into<String>) -> Self {
        Failure { code: -32602, message: message.into() }
    }
}

impl From<anyhow::Error> for Failure {
    fn from(e: anyhow::Error) -> Self {
        Failure { code: -32000, message: format!("{e:#}") }
    }
}

impl From<std::io::Error> for Failure {
    fn from(e: std::io::Error) -> Self {
        anyhow::Error::from(e).into()
    }
}

#[derive(Debug, Deserialize)]
pub struct Inputs {
    #[serde(default)]
    pub url: String,
    pub output: Option<String>,
    pub dir: Option<String>,
}

#[derive(Serialize, Deserialize, Clone)]
struct Sidecar {
    url: String,
    etag: Option<String>,
    last_modified: Option<String>,
    total: Option<u64>,
}

impl Sidecar {
    fn validator(&self) -> Option<&str> {
        self.etag.as_deref().or(self.last_modified.as_deref()).filter(|v| !v.is_empty())
    }
}

fn absolute(base: &Path, p: &str) -> PathBuf {
    let p = Path::new(p);
    if p.is_absolute() {
        p.to_path_buf()
    } else {
        base.join(p)
    }
}

pub async fn download(inputs: Inputs, cwd: Option<String>, cancel: CancellationToken) -> Result<Value, Failure> {
    let url = url::Url::parse(&inputs.url).map_err(|e| anyhow!("bad url {:?}: {e}", inputs.url))?;
    if !matches!(url.scheme(), "http" | "https" | "file") {
        return Err(anyhow!("unsupported scheme {:?}", url.scheme()).into());
    }
    let base = match cwd.as_deref().filter(|c| !c.is_empty()) {
        Some(c) => PathBuf::from(c),
        None => std::env::current_dir()?, // the user's directory, not ours
    };

    let (target, lock) = match inputs.dir.as_deref().filter(|d| !d.is_empty()) {
        Some(dir) => {
            let name = match inputs.output.as_deref().filter(|o| !o.is_empty()) {
                Some(o) if is_plain_name(o) => o.to_string(),
                Some(o) => return Err(Failure::invalid(format!("output {o:?} must be a plain file name, not a path (dir is set)"))),
                None => names::url_name(&url),
            };
            let dir = absolute(&base, dir);
            std::fs::create_dir_all(&dir)?;
            let picked = names::pick_target(&dir, &name, url.as_str())?;
            (picked.path, picked.lock)
        }
        None => {
            let target = match inputs.output.as_deref().filter(|o| !o.is_empty()) {
                Some(o) => absolute(&base, o),
                None => base.join(names::url_name(&url)),
            };
            let name = target.file_name().and_then(|n| n.to_str()).unwrap_or(names::FALLBACK);
            match Lock::try_acquire(&names::lock_of(&target))? {
                Some(l) => (target, l),
                None => return Err(anyhow!("already downloading {name}").into()),
            }
        }
    };
    let name = target.file_name().and_then(|n| n.to_str()).unwrap_or(names::FALLBACK).to_string();

    let result = transfer(&url, &target, &name, &cancel).await;
    lock.release(); // on cancel or error the .part file and its sidecar stay; only the lock goes
    let size = result?;

    let shown = target.to_string_lossy().into_owned();
    rpc::output(&format!("saved {shown} ({size} bytes)\n"));
    Ok(json!({"exit_code": 0, "paths": [shown]}))
}

enum Body {
    Http(futures_util::stream::BoxStream<'static, reqwest::Result<bytes::Bytes>>),
    File(tokio::fs::File),
}

impl Body {
    async fn next(&mut self) -> anyhow::Result<Option<bytes::Bytes>> {
        match self {
            Body::Http(s) => Ok(s.next().await.transpose()?),
            Body::File(f) => {
                use tokio::io::AsyncReadExt;
                let mut buf = vec![0u8; 64 * 1024];
                let n = f.read(&mut buf).await?;
                buf.truncate(n);
                Ok(if n == 0 { None } else { Some(buf.into()) })
            }
        }
    }
}

struct Opened {
    body: Body,
    /// bytes of the body, when known
    len: Option<u64>,
    /// the offset the body starts at (non-zero only for a resumed download)
    offset: u64,
    sidecar: Option<Sidecar>,
}

/// Fetch into `<target>.part`, then rename. Returns the size of the finished file.
async fn transfer(url: &url::Url, target: &Path, name: &str, cancel: &CancellationToken) -> Result<u64, Failure> {
    let part = names::part_of(target);
    let side_path = names::sidecar_of(target);
    let is_http = url.scheme() != "file";

    let opened = tokio::select! {
        r = open(url, &part, &side_path, is_http) => r?,
        _ = cancel.cancelled() => return Err(Failure::cancelled()),
    };
    let Opened { mut body, len, offset, sidecar } = opened;
    let total = len.map(|l| l + offset);

    let mut file = open_part(&part, offset)?;
    file.seek(std::io::SeekFrom::Start(offset)).await?;

    let mut done = offset;
    let mut meter = Meter::new();
    let mut wrote_sidecar = false;
    let outcome: Result<(), Failure> = async {
        loop {
            let chunk = tokio::select! {
                c = body.next() => match c {
                    Ok(c) => c,
                    Err(e) if is_http => {
                        let of = total.map(|t| format!(" of {t}")).unwrap_or_default();
                        return Err(anyhow!("connection closed after {done}{of} bytes ({e:#})").into());
                    }
                    Err(e) => return Err(e.into()),
                },
                _ = cancel.cancelled() => return Err(Failure::cancelled()),
            };
            let Some(chunk) = chunk else { return Ok(()) };
            if !wrote_sidecar {
                wrote_sidecar = true;
                if let Some(mut s) = sidecar.clone() {
                    s.total = total;
                    std::fs::write(&side_path, serde_json::to_vec(&s).map_err(|e| anyhow!(e))?)?;
                }
            }
            file.write_all(&chunk).await?;
            done += chunk.len() as u64;
            if let Some(speed) = meter.tick(done) {
                rpc::progress(done, total, speed, name);
            }
        }
    }
    .await;
    file.flush().await?;
    drop(file);
    outcome?;

    if let Some(len) = len {
        if is_http && done != total.unwrap_or(done) {
            return Err(anyhow!("connection closed after {done} of {} bytes", len + offset).into());
        }
    }
    rpc::progress(done, total, meter.final_speed(done), name);
    std::fs::rename(&part, target)?;
    let _ = std::fs::remove_file(&side_path);
    Ok(done)
}

/// Open `.part` for writing, never through a symlink: a fresh download removes a stale regular `.part` and
/// creates a new one exclusively; a resume opens the existing file as it is.
fn open_part(part: &Path, offset: u64) -> Result<tokio::fs::File, Failure> {
    use std::fs::OpenOptions;
    let refuse = |e: std::io::Error| Failure::from(anyhow!("{}: {e} (refusing to write through a symlink or odd file)", part.display()));
    let file = if offset == 0 {
        if let Ok(m) = std::fs::symlink_metadata(part) {
            if !m.file_type().is_file() {
                return Err(refuse(std::io::Error::from(std::io::ErrorKind::AlreadyExists)));
            }
            std::fs::remove_file(part)?;
        }
        crate::lock::open_nofollow(OpenOptions::new().write(true).create_new(true), part).map_err(refuse)?
    } else {
        crate::lock::open_nofollow(OpenOptions::new().write(true), part).map_err(refuse)?
    };
    Ok(tokio::fs::File::from_std(file))
}

/// Speed as an exponential moving average over about a second, and the 100 ms progress throttle.
struct Meter {
    start: Option<(Instant, u64)>,
    last: Option<(Instant, u64)>,
    speed: f64,
}

impl Meter {
    fn new() -> Self {
        Meter { start: None, last: None, speed: 0.0 }
    }

    /// The speed for the closing message: the moving average, or the overall average when the whole
    /// transfer was too quick for the average to start.
    fn final_speed(&self, done: u64) -> f64 {
        match self.start {
            Some((at, base)) if self.speed == 0.0 => {
                let secs = at.elapsed().as_secs_f64();
                if secs > 0.0 { done.saturating_sub(base) as f64 / secs } else { 0.0 }
            }
            _ => self.speed,
        }
    }

    /// `Some(speed)` when a progress message is due.
    fn tick(&mut self, done: u64) -> Option<f64> {
        let now = Instant::now();
        match self.last {
            None => {
                self.start = Some((now, done));
                self.last = Some((now, done));
                Some(self.speed)
            }
            Some((at, was)) => {
                let dt = now.duration_since(at);
                if dt < Duration::from_millis(100) {
                    return None;
                }
                let secs = dt.as_secs_f64();
                let instant = (done - was) as f64 / secs;
                let alpha = 1.0 - (-secs).exp();
                self.speed = if self.speed == 0.0 { instant } else { self.speed + alpha * (instant - self.speed) };
                self.last = Some((now, done));
                Some(self.speed)
            }
        }
    }
}

fn client() -> anyhow::Result<reqwest::Client> {
    Ok(reqwest::Client::builder().build()?)
}

async fn open(url: &url::Url, part: &Path, side_path: &Path, is_http: bool) -> Result<Opened, Failure> {
    if !is_http {
        let path = url.to_file_path().map_err(|_| anyhow!("bad file url {url}"))?;
        let f = tokio::fs::File::open(&path).await.map_err(|e| anyhow!("{}: {e}", path.display()))?;
        let len = f.metadata().await?.len();
        return Ok(Opened { body: Body::File(f), len: Some(len), offset: 0, sidecar: None });
    }

    let client = client()?;
    let saved: Option<Sidecar> = std::fs::read_to_string(side_path).ok().and_then(|t| serde_json::from_str(&t).ok());
    let have = std::fs::metadata(part).map(|m| m.len()).unwrap_or(0);
    let mut resume: Option<(u64, String, Sidecar)> = None;
    if have > 0 {
        match saved {
            Some(s) if s.url == url.as_str() => match s.validator().map(str::to_string) {
                Some(v) => resume = Some((have, v, s)),
                None => rpc::log("info", "restarting: no validator"),
            },
            _ => {}
        }
    }

    let mut req = client.get(url.clone());
    if let Some((have, validator, _)) = &resume {
        req = req.header("Range", format!("bytes={have}-")).header("If-Range", validator);
    }
    let mut resp = req.send().await.map_err(|e| anyhow!(e))?;
    let mut status = resp.status();

    if let Some((have, _, s)) = &resume {
        let restart = |why: &str| rpc::log("info", &format!("restarting: {why}"));
        let start = resp
            .headers()
            .get("content-range")
            .and_then(|v| v.to_str().ok())
            .and_then(|v| v.strip_prefix("bytes "))
            .and_then(|v| v.split(['-', '/']).next())
            .and_then(|v| v.parse::<u64>().ok());
        if status.as_u16() == 206 && start == Some(*have) {
            let sidecar = Sidecar {
                url: url.to_string(),
                etag: header(&resp, "etag").or_else(|| s.etag.clone()),
                last_modified: header(&resp, "last-modified").or_else(|| s.last_modified.clone()),
                total: None,
            };
            return Ok(Opened {
                len: resp.content_length(),
                body: Body::Http(resp.bytes_stream().boxed()),
                offset: *have,
                sidecar: Some(sidecar),
            });
        }
        // anything else means the bytes we hold cannot be extended
        let changed = status.as_u16() == 416
            || (s.etag.is_some() && header(&resp, "etag").is_some() && header(&resp, "etag") != s.etag)
            || (s.etag.is_none() && header(&resp, "last-modified").is_some() && header(&resp, "last-modified") != s.last_modified);
        restart(if changed { "file changed on server" } else { "server ignored range" });
        if status.as_u16() != 200 {
            resp = client.get(url.clone()).send().await.map_err(|e| anyhow!(e))?; // a plain request, from byte 0
            status = resp.status();
        }
    }

    if !status.is_success() {
        return Err(anyhow!("server said {status}").into());
    }
    let sidecar = Sidecar {
        url: url.to_string(),
        etag: header(&resp, "etag"),
        last_modified: header(&resp, "last-modified"),
        total: None,
    };
    Ok(Opened { len: resp.content_length(), body: Body::Http(resp.bytes_stream().boxed()), offset: 0, sidecar: Some(sidecar) })
}

fn header(resp: &reqwest::Response, name: &str) -> Option<String> {
    resp.headers().get(name).and_then(|v| v.to_str().ok()).map(str::to_string)
}
