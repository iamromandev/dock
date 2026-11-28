# dev.dock.transcribe

Transcribes the speech in an audio or video file into new text and subtitle files in the current folder. ffmpeg decodes the media inside the container; the speech recognition comes from a local Whisper server that runs on the host (see [server/README.md](server/README.md)). The plugin itself is Python standard library only.

## Use

```
dock transcribe talk.mp4 [--format txt,srt,vtt] [--language en] [-o name]
```

- `file` (required): an audio or video file, resolved against the in folder (or the flow's work folder) and read only from inside it. A symlink is followed only if it ends inside; a FIFO is refused.
- `--format`: a comma list of `txt`, `srt`, `vtt` (default `txt`; order and repeats are ignored; files are written and reported in the order txt, srt, vtt). `txt` has one line per segment, `srt` numbered cues, `vtt` a `WEBVTT` header and cues. UTF-8 with a trailing newline.
- `--language`: a Whisper language code such as `en`, `de`, `yue` (2 or 3 lowercase letters; the server refuses an unknown one). Without it the language is detected on the first window that has sound and pinned for the later ones.
- `--output` / `-o`: the name the files share, without folder or extension (default: the input name without its last suffix). `talk` gives `talk.txt`, `talk.srt`, `talk.vtt`.

Every target is checked before decoding: an existing file is never replaced, and nothing is written when one exists. Files are written as `.name.part` and hard-linked into place (`os.link`); a failure or a cancel removes what this run created.

The audio (`ffprobe` for the duration, then `ffmpeg ... -ac 1 -ar 16000 -f s16le`) is cut into windows of 600 s; each later window also repeats the last 10 s of the one before, so a POST is at most 610 s. Segment times are shifted by the window start. Whisper segments each window differently, so a seam is joined by words, not times: the cues of the earlier window that end after the later window's start and the later window's cues that start before the earlier window's end are tokenised, and the longest run of words (compared case- and punctuation-blind) they share, at least 3 (2 if a side has under 6 words), is the splice point: the earlier text before it, the later window's (uncut) text from it on, with cue times interpolated over the words. With no such run the seam is cut by time at the middle of the overlap (earlier cues starting before it, later ones from it). A window is silent, and not sent, when every 1 s block of it has an RMS below about 98 (int16 scale, the server's 0.003); a few quiet seconds of speech in a mostly silent window still count. At most one window of audio is in memory.

Input longer than `DOCK_TRANSCRIBE_MAX_SECONDS` (default 14400, a positive integer) is refused, by its probed duration and again by the decoded size. A file without an audio stream, or one ffmpeg cannot decode, is `not a media file with audio: <name>`. No speech at all gives empty files (the `vtt` holds only its header) and a `no speech found` warning.

ffprobe and ffmpeg may only open `file` and `pipe` (`-protocol_whitelist file,pipe`), so a crafted playlist or `concat`/`sdp` file cannot make them reach the network, `host.docker.internal` or a metadata address.

Progress is a bar `transcribe` ("transcribing") in percent of the audio duration. Result: `{"exit_code": 0, "outputs": {"files": [paths in txt, srt, vtt order], "language": "en"}}`.

Errors: wrong input types are JSON-RPC errors (`-32602`). A bad file, format, language, output name, an unreachable or failing server (its `{"error"}` is quoted, a 503 busy included) are logged as errors and the result is `{"exit_code": 1}`. A cancel, or closing stdin, kills ffmpeg, closes the server socket (also while connecting or while the audio is still being sent), leaves no file and answers `-32800` within a moment. This holds even after the files were written: a cancel that arrives then still removes them and answers cancelled (unlike narrate, which keeps what it finished). A server that stops answering gives `the transcribe server stopped answering (<url>)`, and one silent for 1200 s (`TIMEOUT`; Whisper can need ~9 minutes for a window of hard audio, and the server cannot stream progress; the connect timeout is 10 s) gives `timed out after 1200 s waiting for the transcribe server`; `cannot reach ... start it` is for a refused or timed-out connection. If ffmpeg fails after some audio was decoded the error is `could not decode all of <name>`; nothing is written.

## Setup

Start the server on the host (Apple Silicon only):

```
uv run --project plugins/transcribe/server python plugins/transcribe/server/serve.py
```

(or `make transcribe-server`)

| Env var | Default |
|---|---|
| `DOCK_TRANSCRIBE_URL` | `http://host.docker.internal:7862` (an `http` URL with a host and a port 1-65535, else exit 1) |
| `DOCK_TRANSCRIBE_MAX_SECONDS` | `14400` |

Permissions: `network`, `fs:read:in`, `fs:write:cwd`.

## Develop

`make transcribe-test`, `make transcribe-check-ci` (tests use a local stand-in server and fake ffmpeg/ffprobe; no Docker, GPU, model or ffmpeg needed). A few tests call `main.transcribe` in the test process to reach seams a subprocess cannot: the answer timeout (`TIMEOUT`), the connect (`socket.socket.connect`) and `Outputs.publish` (a cancel right after the files are linked). The server has its own tests: `uv run --project plugins/transcribe/server pytest`.
