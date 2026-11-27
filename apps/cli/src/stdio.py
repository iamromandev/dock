"""The terminal side of asking a yes/no question: the CLI's answer to what the host core leaves to its caller."""

from __future__ import annotations

import sys


def ask_stdin(question: str) -> str | None:
    print(question, end="", file=sys.stderr, flush=True)
    try:
        answer = sys.stdin.readline()
    except UnicodeDecodeError:  # an answer that is not text is not a yes
        return None
    return answer if answer else None


def tty() -> bool:
    """Whether a prompt can be asked: stdin and stderr (where it is shown) are terminals."""
    return sys.stdin.isatty() and sys.stderr.isatty()
