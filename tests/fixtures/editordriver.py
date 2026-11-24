"""Child of tests/test_editor.py: reads lines with the LineEditor until EOF.

argv[1:] is the history. Completion offers plugin, start, status and stop, matched on the word being typed.
With DOCK_EDITOR_HOSTILE set it offers candidates full of control characters instead (every C0 and C1 control,
DEL, lone surrogates, escape sequences), drawn with the random seed DOCK_EDITOR_SEED; they all start with "ho".
"""

import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from cli.editor import LineEditor  # noqa: E402
from cli.term import Terminal  # noqa: E402

WORDS = ["plugin", "start", "status", "stop"]
if os.environ.get("DOCK_EDITOR_HOSTILE"):  # candidates a plugin could have put in the index
    rnd = random.Random(int(os.environ.get("DOCK_EDITOR_SEED", "0")))
    pieces = [*map(chr, range(0x20)), "\x7f", *map(chr, range(0x80, 0xA0)), "\ud800", "\udfff", "\x1b]0;pwned\x07",
              "\x1b[2J", "\x9b2J", "ab", "é"]
    WORDS = ["ho" + "".join(rnd.choice(pieces) for _ in range(rnd.randint(1, 6))) for _ in range(6)]


def complete(line, word):
    return [w for w in WORDS if w.startswith(word)]


term = Terminal()
term.install()
editor = LineEditor(term, list(sys.argv[1:]), complete)
while True:
    try:
        line = editor.readline("ready> ")
    except EOFError:
        term.write("eof\n")
        break
    except KeyboardInterrupt:
        term.write("\ninterrupted\n")
        continue
    term.write(f"got: {line!r}\n")
term.close()
