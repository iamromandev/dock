"""summarize plugin for dock: summarizes a text, Markdown or subtitle file with a local Ollama model (stdlib only).
Long texts are summarized in chunks first. JSON-RPC 2.0, one JSON object line on stdin/stdout. See protocol/PROTOCOL.md.
"""

from dock_plugin import rpc
from summarizecore.run import run

COMMANDS = {"summarize": run}

if __name__ == "__main__":
    rpc.run(COMMANDS, "summarize", "summarizing")
