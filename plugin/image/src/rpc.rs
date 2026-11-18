//! JSON-RPC over lines: framing, dispatch and the `Host` that talks to dock.

use crate::Host;
use serde_json::{json, Value};
use std::io::{BufRead, Write};
use std::path::PathBuf;

pub const METHOD_NOT_FOUND: i64 = -32601;
pub const INVALID_PARAMS: i64 = -32602;

/// Talks to dock over a reader and a writer (stdin and stdout in the real plugin).
pub struct StdioHost<R: BufRead, W: Write> {
    reader: R,
    writer: W,
    next_id: u64,
}

impl<R: BufRead, W: Write> StdioHost<R, W> {
    pub fn new(reader: R, writer: W) -> Self {
        StdioHost { reader, writer, next_id: 1 }
    }

    /// The next message, skipping blank and unparsable lines; None at end of input.
    fn read(&mut self) -> Option<Value> {
        loop {
            let mut line = String::new();
            match self.reader.read_line(&mut line) {
                Ok(0) | Err(_) => return None,
                Ok(_) => {}
            }
            if let Ok(v) = serde_json::from_str::<Value>(line.trim()) {
                return Some(v);
            }
        }
    }

    fn send(&mut self, msg: Value) {
        let _ = writeln!(self.writer, "{msg}");
        let _ = self.writer.flush();
    }

    fn notify(&mut self, method: &str, params: Value) {
        self.send(json!({"jsonrpc": "2.0", "method": method, "params": params}));
    }

    fn reply(&mut self, id: Value, result: Value) {
        self.send(json!({"jsonrpc": "2.0", "id": id, "result": result}));
    }

    fn reply_error(&mut self, id: Value, code: i64, message: &str) {
        self.send(json!({"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message}}));
    }
}

impl<R: BufRead, W: Write> Host for StdioHost<R, W> {
    fn output(&mut self, text: &str) {
        self.notify("output", json!({"text": text}));
    }

    fn log(&mut self, level: &str, message: &str) {
        self.notify("log", json!({"level": level, "message": message}));
    }

    fn progress(&mut self, percent: Option<f64>, message: &str) {
        self.notify("progress", json!({"percent": percent, "message": message}));
    }

    fn prompt(&mut self, prompt: &str) -> Option<String> {
        let id = self.next_id;
        self.next_id += 1;
        self.send(json!({"jsonrpc": "2.0", "id": id, "method": "prompt_user", "params": {"prompt": prompt}}));
        loop {
            let msg = self.read()?;
            if msg.get("id") == Some(&json!(id)) && msg.get("method").is_none() {
                let result = msg.get("result")?;
                if result.get("eof").and_then(Value::as_bool) == Some(true) {
                    return None;
                }
                return Some(result.get("text").and_then(Value::as_str).unwrap_or("").to_string());
            }
            // Anything else (a cancel notification, a stray message) is skipped.
        }
    }
}

/// Serve requests until `shutdown` or end of input. `run` handles `invoke` of the command `image`.
pub fn serve<R, W, F>(host: &mut StdioHost<R, W>, command: &str, mut run: F)
where
    R: BufRead,
    W: Write,
    F: FnMut(&[String], &std::path::Path, &mut dyn Host) -> i32,
{
    while let Some(msg) = host.read() {
        let (Some(method), Some(id)) = (msg.get("method").and_then(Value::as_str), msg.get("id").cloned()) else {
            continue; // notifications (cancel, ...) and stray responses are ignored
        };
        let params = msg.get("params").cloned().unwrap_or(Value::Null);
        match method {
            "initialize" => host.reply(id, json!({"name": command})),
            "shutdown" => {
                host.reply(id, json!({}));
                return;
            }
            "invoke" => {
                if params.get("command").and_then(Value::as_str) != Some(command) {
                    host.reply_error(id, METHOD_NOT_FOUND, "unknown command");
                    continue;
                }
                let args: Vec<String> = params
                    .get("args")
                    .and_then(Value::as_array)
                    .map(|a| a.iter().filter_map(|v| v.as_str().map(String::from)).collect())
                    .unwrap_or_default();
                let Some(cwd) = params.get("cwd").and_then(Value::as_str) else {
                    host.reply_error(id, INVALID_PARAMS, "invoke needs a cwd");
                    continue;
                };
                let code = run(&args, &PathBuf::from(cwd), host);
                host.reply(id, json!({"exit_code": code}));
            }
            _ => host.reply_error(id, METHOD_NOT_FOUND, "unknown method"),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    fn lines(out: &[u8]) -> Vec<Value> {
        String::from_utf8_lossy(out).lines().map(|l| serde_json::from_str(l).unwrap()).collect()
    }

    fn run_serve(input: &str, code: i32) -> Vec<Value> {
        let mut out = Vec::new();
        {
            let mut host = StdioHost::new(Cursor::new(input.as_bytes().to_vec()), &mut out);
            serve(&mut host, "image", |args, cwd, h| {
                h.output(&format!("{}|{}", args.join(","), cwd.display()));
                code
            });
        }
        lines(&out)
    }

    #[test]
    fn initialize_returns_the_plugin_name() {
        let out = run_serve("{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{}}\n", 0);
        assert_eq!(out[0]["id"], 1);
        assert_eq!(out[0]["result"]["name"], "image");
    }

    #[test]
    fn invoke_of_an_unknown_command_is_method_not_found() {
        let out = run_serve("{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"invoke\",\"params\":{\"command\":\"nope\",\"args\":[],\"cwd\":\"/w\"}}\n", 0);
        assert_eq!(out[0]["error"]["code"], -32601);
    }

    #[test]
    fn invoke_runs_the_command_and_returns_its_exit_code() {
        let out = run_serve("{\"jsonrpc\":\"2.0\",\"id\":3,\"method\":\"invoke\",\"params\":{\"command\":\"image\",\"args\":[\"a\",\"b\"],\"cwd\":\"/w\"}}\n", 2);
        assert_eq!(out[0]["method"], "output");
        assert_eq!(out[0]["params"]["text"], "a,b|/w");
        assert_eq!(out[1]["id"], 3);
        assert_eq!(out[1]["result"]["exit_code"], 2);
    }

    #[test]
    fn shutdown_replies_and_stops_reading() {
        let out = run_serve("{\"jsonrpc\":\"2.0\",\"id\":4,\"method\":\"shutdown\",\"params\":{}}\n{\"jsonrpc\":\"2.0\",\"id\":5,\"method\":\"initialize\"}\n", 0);
        assert_eq!(out.len(), 1);
        assert_eq!(out[0]["id"], 4);
    }

    #[test]
    fn notifications_are_ignored() {
        let out = run_serve("{\"jsonrpc\":\"2.0\",\"method\":\"cancel\",\"params\":{}}\n", 0);
        assert!(out.is_empty());
    }

    #[test]
    fn prompt_skips_a_cancel_notification_and_returns_the_text() {
        let input = "{\"jsonrpc\":\"2.0\",\"method\":\"cancel\",\"params\":{}}\n{\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{\"text\":\"yes\"}}\n";
        let mut out = Vec::new();
        let mut host = StdioHost::new(Cursor::new(input.as_bytes().to_vec()), &mut out);
        assert_eq!(host.prompt("Overwrite x? [y/N]"), Some("yes".to_string()));
        drop(host);
        let sent = lines(&out);
        assert_eq!(sent[0]["method"], "prompt_user");
        assert_eq!(sent[0]["id"], 1);
        assert_eq!(sent[0]["params"]["prompt"], "Overwrite x? [y/N]");
    }

    #[test]
    fn prompt_returns_none_on_eof_answer_and_on_end_of_input() {
        let mut out = Vec::new();
        let mut host = StdioHost::new(Cursor::new(b"{\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{\"eof\":true}}\n".to_vec()), &mut out);
        assert_eq!(host.prompt("p"), None);
        let mut host = StdioHost::new(Cursor::new(Vec::new()), &mut out);
        assert_eq!(host.prompt("p"), None);
    }

    #[test]
    fn log_and_progress_are_notifications() {
        let mut out = Vec::new();
        {
            let mut host = StdioHost::new(Cursor::new(Vec::new()), &mut out);
            host.log("error", "boom");
            host.progress(Some(33.0), "edit");
        }
        let sent = lines(&out);
        assert_eq!(sent[0]["method"], "log");
        assert_eq!(sent[0]["params"]["level"], "error");
        assert_eq!(sent[1]["params"]["percent"], 33.0);
    }
}
