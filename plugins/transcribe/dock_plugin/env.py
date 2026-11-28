"""Reading settings from the environment."""

import os
import re

from .cancel import Failure


def positive_int(var, default):
    """The integer in the environment variable `var` (>= 1), or `default` when it is unset or empty."""
    raw = os.environ.get(var)
    if raw is None or raw == "":
        return default
    if not re.fullmatch(r"[0-9]+", raw) or int(raw) < 1:
        raise Failure(f"{var} must be a positive integer, not {raw[:30]!r}")
    return int(raw)
