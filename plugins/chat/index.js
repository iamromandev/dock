#!/usr/bin/env node
// chat plugin for dock, written against the raw protocol (no SDK):
// JSON-RPC 2.0, one JSON object per line on stdin/stdout. See protocol/PROTOCOL.md.
"use strict";

const fs = require("fs");
const path = require("path");
const readline = require("readline");

// ---- protocol plumbing --------------------------------------------------
const send = (msg) => process.stdout.write(JSON.stringify({ jsonrpc: "2.0", ...msg }) + "\n");
const notify = (method, params) => send({ method, params });
const output = (text) => notify("output", { text });
const log = (message, level = "info") => notify("log", { level, message });
const progress = (percent, message) => notify("progress", { percent, message });

class Cancelled extends Error {}
let nextId = 1;
const pending = new Map(); // our requests to the host, waiting for replies

function request(method, params) {
  const id = `chat-${nextId++}`;
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject });
    send({ id, method, params });
  });
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ---- the "model" ----------------------------------------------------------
// Stand-in for a real LLM. provision() is where you would load weights or
// open a client; unprovision() is where you release them.
class ToyModel {
  reply(text) {
    const t = text.toLowerCase();
    if (/^(hi|hello|hey)\b/.test(t)) return "Hello! I'm a toy model running inside a dock plugin.";
    if (t.endsWith("?")) return `Good question. A real model would answer "${text}" here.`;
    const words = text.split(/\s+/).length;
    return `You said ${words} word${words === 1 ? "" : "s"}. Plug a real LLM into provision() to do better.`;
  }
}

let ctx = {};
let model = null;
const lockFile = () => path.join(ctx.data_dir, "model.lock");

// ---- handlers for host -> plugin requests ---------------------------------
const handlers = {
  initialize(params) {
    ctx = params;
    return { name: "chat" };
  },

  async provision() {
    for (const pct of [25, 60, 100]) {
      progress(pct, "loading model");
      await sleep(80);
    }
    model = new ToyModel();
    fs.writeFileSync(lockFile(), String(process.pid)); // the "resource" we must release
    log("model loaded", "debug");
    return {};
  },

  // Idempotent and works in a fresh process: the host calls it for crash recovery.
  unprovision() {
    if (fs.existsSync(lockFile())) fs.unlinkSync(lockFile());
    model = null;
    log("model released", "debug");
    return {};
  },

  async invoke({ command }) {
    if (command !== "chat") throw Object.assign(new Error(`unknown command ${command}`), { code: -32601 });
    output("chat ready, type /exit to quit\n");
    for (;;) {
      const res = await request("prompt_user", { prompt: "you> " });
      if (res.eof) break;
      const text = res.text.trim();
      if (!text) continue;
      if (text === "/exit") break;
      output("bot> ");
      for (const word of model.reply(text).split(" ")) {   // stream token by token
        output(word + " ");
        await sleep(15);
      }
      output("\n");
    }
    return { exit_code: 0 };
  },

  shutdown() {
    setImmediate(() => process.exit(0));
    return {};
  },
};

// ---- dispatch -------------------------------------------------------------
const rl = readline.createInterface({ input: process.stdin });

rl.on("line", async (line) => {
  if (!line.trim()) return;
  let msg;
  try { msg = JSON.parse(line); } catch { return; }

  if (msg.method === undefined) {              // reply to one of our requests
    const p = pending.get(msg.id);
    if (!p) return;
    pending.delete(msg.id);
    msg.error ? p.reject(new Error(msg.error.message)) : p.resolve(msg.result);
    return;
  }
  if (msg.method === "cancel") {               // Ctrl-C in the host
    for (const p of pending.values()) p.reject(new Cancelled());
    pending.clear();
    return;
  }
  if (msg.id === undefined) return;            // other notifications: ignore

  const handler = handlers[msg.method];
  if (!handler) return send({ id: msg.id, error: { code: -32601, message: `unknown method ${msg.method}` } });
  try {
    send({ id: msg.id, result: await handler(msg.params || {}) });
  } catch (e) {
    const cancelled = e instanceof Cancelled;
    send({ id: msg.id, error: { code: cancelled ? -32800 : e.code ?? -32000, message: cancelled ? "cancelled" : e.message } });
  }
});

// Host went away without unprovision: best-effort cleanup.
rl.on("close", () => {
  if (ctx.data_dir && fs.existsSync(lockFile())) fs.unlinkSync(lockFile());
  process.exit(0);
});
