#!/usr/bin/env node
// chat plugin for dock, written against the raw protocol:
// JSON-RPC 2.0, one JSON object per line on stdin/stdout. See protocol/PROTOCOL.md.
//
// chat owns the conversation (history, prompts, display). The model itself is
// another plugin: chat calls the `llm` capability through the host and
// receives the reply as streamed `call_event` notifications.
"use strict";

const readline = require("readline");

// ---- protocol plumbing --------------------------------------------------
const send = (msg) => process.stdout.write(JSON.stringify({ jsonrpc: "2.0", ...msg }) + "\n");
const notify = (method, params) => send({ method, params });
const output = (text) => notify("output", { text });
const log = (message, level = "info") => notify("log", { level, message });

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

const callLlm = (method, params) => request("call", { capability: "llm", method, params });

// ---- handlers for host -> plugin requests ---------------------------------
const handlers = {
  initialize() {
    return { name: "chat" };
  },

  async invoke({ command }) {
    if (command !== "chat") throw Object.assign(new Error(`unknown command ${command}`), { code: -32601 });

    const model = await callLlm("info", {});
    output(`chat ready (${model.backend}: ${model.model}), /exit to quit, /reset to clear\n`);

    let history = [];
    for (;;) {
      const res = await request("prompt_user", { prompt: "you> " });
      if (res.eof) break;
      const text = res.text.trim();
      if (!text) continue;
      if (text === "/exit") break;
      if (text === "/reset") {
        history = [];
        output("history cleared\n");
        continue;
      }

      history.push({ role: "user", content: text });
      output("bot> ");
      try {
        const reply = await callLlm("generate", { messages: history }); // tokens arrive as call_event
        history.push({ role: "assistant", content: reply.text });
        output("\n");
      } catch (e) {
        if (e instanceof Cancelled) throw e;
        history.pop(); // keep history consistent with what the model actually answered
        output("\n");
        log(e.message, "error");
      }
    }
    return { exit_code: 0 };
  },

  shutdown() {
    setImmediate(() => process.exit(0));
    return {};
  },
};

// ---- host -> plugin notifications -------------------------------------------
const notifications = {
  call_event({ capability, data }) {
    if (capability === "llm" && data && data.token) output(data.token); // stream to the user
  },
  cancel() {
    for (const p of pending.values()) p.reject(new Cancelled());
    pending.clear();
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
    if (!msg.error) return p.resolve(msg.result);
    p.reject(msg.error.code === -32800 ? new Cancelled() : new Error(msg.error.message));
    return;
  }
  if (msg.id === undefined) {                  // notification
    const fn = notifications[msg.method];
    if (fn) fn(msg.params || {});
    return;
  }

  const handler = handlers[msg.method];
  if (!handler) return send({ id: msg.id, error: { code: -32601, message: `unknown method ${msg.method}` } });
  try {
    send({ id: msg.id, result: await handler(msg.params || {}) });
  } catch (e) {
    const cancelled = e instanceof Cancelled;
    send({ id: msg.id, error: { code: cancelled ? -32800 : e.code ?? -32000, message: cancelled ? "cancelled" : e.message } });
  }
});

rl.on("close", () => process.exit(0));
