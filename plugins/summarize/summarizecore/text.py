"""The text to summarize: decoding, subtitle cleaning and cutting it into chunks that fit one call."""

import re

from dock_plugin.cancel import Failure

LIMIT = 20_000  # characters of text sent in one call
BUDGET = 5_000  # estimated tokens of text sent in one call (the model's context is client.NUM_CTX)
VTT_TAG = re.compile(r"</?(c|v|b|i|u|ruby|rt|lang)(\.[\w.-]*)?(\s[^>]*)?>|<\d{2}:\d{2}(:\d{2})?\.\d{3}>")


def decode(raw):
    """The text of a file's bytes: UTF-8 without a leading BOM, with LF newlines. Raises Failure."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise Failure("file is not UTF-8 text") from None
    return text.removeprefix("﻿").replace("\r\n", "\n").replace("\r", "\n")


def subtitles(text, vtt):
    """The spoken words of an SRT or WebVTT text: cue texts joined with spaces, a cue that repeats the one before
    kept once."""
    cues: list[str] = []
    for block in re.split(r"\n[ \t]*\n", text):
        lines = [x for x in block.split("\n") if x.strip()]
        if not lines:
            continue
        if vtt and re.match(r"(WEBVTT|NOTE|STYLE|REGION)(\s|$)", lines[0]):
            if not (lines[0].startswith("WEBVTT") and any("-->" in x for x in lines)):
                continue
            lines = lines[1:]  # a cue right after the header line: drop the header only
        # lines before the first time line are the cue number or id; later time lines are cut-and-paste leftovers
        arrow = next((i for i, x in enumerate(lines) if "-->" in x), -1)
        body = [x for x in lines[arrow + 1:] if "-->" not in x]
        if vtt:
            body = [VTT_TAG.sub("", x) for x in body]
        cue = " ".join(" ".join(body).split())
        if cue and cues and cue == cues[-1]:
            continue
        if cue and cues:  # a rolling caption repeats the end of the one before: keep its new words only
            old, new = cues[-1].split(), cue.split()
            k = max((k for k in range(min(len(old), len(new)), 1, -1) if old[-k:] == new[:k]), default=0)
            cue = " ".join(new[k:])
        if cue:
            cues.append(cue)
    return " ".join(cues)


def clean(text, suffix):
    """The text to summarize. Raises Failure when no letter or digit is left."""
    suffix = suffix.lower()
    if suffix in (".srt", ".vtt"):
        text = subtitles(text, suffix == ".vtt")
    text = text.strip()
    if not any(c.isalnum() for c in text):
        raise Failure("nothing to summarize")
    return text


def estimate(text):
    """A cautious count of the tokens in `text`: 1 per 4 ASCII characters, 1 per other character."""
    ascii_chars = sum(c < "\x80" for c in text)
    return ascii_chars / 4 + (len(text) - ascii_chars)


def reach(text, limit, budget):
    """How many leading characters of `text` fit in `limit` characters and `budget` estimated tokens."""
    cost = 0.0
    for i, c in enumerate(text[:limit]):
        cost += 0.25 if c < "\x80" else 1.0
        if cost > budget:
            return max(i, 1)
    return min(len(text), limit)


def fits(text, limit=LIMIT, budget=BUDGET):
    return len(text) <= limit and estimate(text) <= budget


def chunk(text, limit=LIMIT, budget=BUDGET):
    """Cuts `text` into parts of at most `limit` characters and `budget` estimated tokens, each cut at the last
    paragraph break in the window, else the last sentence end, else the last space (each only past half the window,
    so no part is tiny), else hard at the end of the window."""
    parts = []
    rest = text.strip()
    while not fits(rest, limit, budget):
        size = reach(rest, limit, budget)
        window = rest[:size]
        half = size / 2
        cut = window.rfind("\n\n")
        if cut <= half:
            cut = max((m.start() + 1 for m in re.finditer(r"[.!?]\s", window)), default=0)
        if cut <= half:
            cut = max((m.start() for m in re.finditer(r"\s", window)), default=0)
        cut = cut if cut > half else size
        parts.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    if rest:
        parts.append(rest)
    return parts
