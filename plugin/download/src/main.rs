//! download plugin for dock, written against the raw protocol (PROTOCOL.md).
//! JSON-RPC 2.0, one JSON object per line on stdin/stdout.

mod http;
mod lock;
mod names;
mod rpc;

use serde::Deserialize;
use serde_json::{json, Value};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::task::JoinHandle;
use tokio_util::sync::CancellationToken;

#[derive(Deserialize)]
struct Invoke {
    command: String,
    cwd: Option<String>,
    #[serde(default)]
    inputs: Value,
}

type Running = Arc<Mutex<Vec<(u64, CancellationToken)>>>;
static NEXT: AtomicU64 = AtomicU64::new(0);

fn cancel_all(running: &Running) {
    for token in running.lock().unwrap_or_else(|e| e.into_inner()).iter() {
        token.1.cancel();
    }
}

fn invoke(id: Value, params: Value, running: &Running) -> JoinHandle<()> {
    // Each invoke gets its own token, so a `cancel` reaches the running one and nothing else is shared.
    let token = CancellationToken::new();
    let key = NEXT.fetch_add(1, Ordering::Relaxed);
    running.lock().unwrap_or_else(|e| e.into_inner()).push((key, token.clone()));
    let running = running.clone();
    tokio::spawn(async move {
        let outcome = run(params, token.clone()).await;
        running.lock().unwrap_or_else(|e| e.into_inner()).retain(|(k, _)| *k != key);
        match outcome {
            Ok(result) => rpc::reply(&id, result),
            Err(f) => rpc::reply_error(&id, f.code, &f.message),
        }
    })
}

async fn run(params: Value, cancel: CancellationToken) -> Result<Value, http::Failure> {
    let p: Invoke = serde_json::from_value(params).map_err(|e| http::Failure::invalid(e.to_string()))?;
    match p.command.as_str() {
        "download" => {
            let inputs: http::Inputs =
                serde_json::from_value(p.inputs).map_err(|e| http::Failure::invalid(e.to_string()))?;
            http::download(inputs, p.cwd, cancel).await
        }
        other => Err(http::Failure { code: -32601, message: format!("unknown command {other}") }),
    }
}

#[tokio::main]
async fn main() {
    let running: Running = Arc::default();
    let mut tasks: Vec<JoinHandle<()>> = Vec::new();
    let mut lines = BufReader::new(tokio::io::stdin()).lines();

    while let Ok(Some(line)) = lines.next_line().await {
        let Ok(m) = serde_json::from_str::<Value>(&line) else { continue };
        let id = m.get("id").cloned().unwrap_or(Value::Null);
        let params = m.get("params").cloned().unwrap_or(Value::Null);
        match m.get("method").and_then(Value::as_str) {
            Some("initialize") => rpc::reply(&id, json!({"name": "download"})),
            Some("cancel") => cancel_all(&running), // notification from Ctrl-C
            Some("invoke") => tasks.push(invoke(id, params, &running)),
            Some("shutdown") => {
                cancel_all(&running);
                for t in tasks.drain(..) {
                    let _ = t.await;
                }
                rpc::reply(&id, json!({}));
                return;
            }
            Some(other) if !id.is_null() => rpc::reply_error(&id, -32601, &format!("unknown method {other}")),
            _ => {}
        }
    }
    // stdin closed: stop what runs, as for a shutdown
    cancel_all(&running);
    for t in tasks {
        let _ = t.await;
    }
}
