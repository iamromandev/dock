//! JSON-RPC 2.0 over stdin/stdout, one object per line (protocol/PROTOCOL.md): framing and the notification helpers.

use serde_json::{json, Value};
use std::io::Write;
use std::sync::Mutex;

static OUT: Mutex<()> = Mutex::new(());

/// Write one line. Stdout is shared by every task, so a line is never interleaved with another.
pub fn send(mut message: Value) {
    message["jsonrpc"] = json!("2.0");
    let mut line = message.to_string();
    line.push('\n');
    let _guard = OUT.lock().unwrap_or_else(|e| e.into_inner());
    let mut out = std::io::stdout().lock();
    let _ = out.write_all(line.as_bytes());
    let _ = out.flush();
}

pub fn reply(id: &Value, result: Value) {
    send(json!({"id": id, "result": result}));
}

pub fn reply_error(id: &Value, code: i64, message: &str) {
    send(json!({"id": id, "error": {"code": code, "message": message}}));
}

pub fn notify(method: &str, params: Value) {
    send(json!({"method": method, "params": params}));
}

pub fn output(text: &str) {
    notify("output", json!({"text": text}));
}

pub fn log(level: &str, message: &str) {
    notify("log", json!({"level": level, "message": message}));
}

/// `percent` and `total` are null when the size is unknown; `speed` is bytes per second.
pub fn progress(done: u64, total: Option<u64>, speed: f64, name: &str) {
    let percent = match total {
        Some(t) if t > 0 => json!(done as f64 * 100.0 / t as f64),
        _ => Value::Null,
    };
    let message = if total.is_some() { "downloading".to_string() } else { format!("{done} bytes") };
    notify(
        "progress",
        json!({"percent": percent, "message": message, "done": done, "total": total, "speed": speed, "name": name}),
    );
}
