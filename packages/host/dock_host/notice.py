"""Where the host core says something to the user outside a plugin run: one hook, stderr by default."""

from __future__ import annotations

import sys


def default_notice(text: str) -> None:
    print(text, file=sys.stderr, flush=True)
