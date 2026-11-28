"""transcribe plugin for dock: turns the speech in an audio or video file into text and subtitle files. ffmpeg
decodes the media in the container; windows of PCM go to a local Whisper server on the host (stdlib only).
JSON-RPC 2.0, one JSON object line on stdin/stdout. See protocol/PROTOCOL.md.
"""

from dock_plugin import rpc
from transcribecore.run import run

COMMANDS = {"transcribe": run}

if __name__ == "__main__":
    rpc.run(COMMANDS, "transcribe", "transcribing")
