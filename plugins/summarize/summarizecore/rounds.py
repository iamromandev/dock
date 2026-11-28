"""Rounds of partial summaries until the text fits one call, then the final call."""

from dock_plugin.cancel import Failure

from .prompts import INSTRUCTIONS, PARTIAL
from .text import chunk, fits

MAX_ROUNDS = 3  # rounds of partial summaries before giving up


def summarize_text(text, style, ask, tick):
    """Returns (answer, first-round chunk count). `ask(instruction, text)` returns one answer; `tick(done, total)`
    tells progress. A text over LIMIT characters or BUDGET estimated tokens is summarized in chunks, round after round, until it fits; then one call with
    the style's instruction."""
    done = 0
    total = 1
    first = 1
    rounds = 0
    while not fits(text):
        if rounds == MAX_ROUNDS:
            raise Failure("input too long to summarize")
        parts = chunk(text)
        if rounds == 0:
            first = len(parts)
            total = len(parts) + 1  # the final call
        else:
            total += len(parts)
        partials = []
        for part in parts:
            partials.append(ask(PARTIAL, part).strip())
            done += 1
            tick(done, total)
        text = "\n\n".join(partials)
        rounds += 1
    answer = ask(INSTRUCTIONS[style], text).strip()
    tick(done + 1, total)
    return answer, first
