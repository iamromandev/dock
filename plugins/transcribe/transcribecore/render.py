"""Times and the text of the output files."""


def clock(seconds):
    seconds = int(seconds)
    h, rest = divmod(seconds, 3600)
    return f"{h}:{rest // 60:02d}:{rest % 60:02d}" if h else f"{rest // 60}:{rest % 60:02d}"


def stamp(seconds, sep):
    ms = round(seconds * 1000)
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    return f"{h:02d}:{m:02d}:{ms // 1000:02d}{sep}{ms % 1000:03d}"


def render(fmt, cues):
    """The text of one output file from cues [(start, end, text)] in seconds."""
    if fmt == "txt":
        return "".join(f"{text}\n" for _, _, text in cues)
    sep = "," if fmt == "srt" else "."
    blocks = []
    for i, (start, end, text) in enumerate(cues, 1):
        head = f"{i}\n" if fmt == "srt" else ""
        blocks.append(f"{head}{stamp(start, sep)} --> {stamp(end, sep)}\n{text.replace('-->', '->')}\n")
    if fmt == "srt":
        return "\n".join(blocks)
    return "WEBVTT\n" + "".join(f"\n{b}" for b in blocks)
