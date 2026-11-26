"""Child of tests/test_term_pty.py: puts a pty into dock's terminal modes, then ends in the way named by argv[1].

  normal    leave main() normally
  raise     raise an exception from main
  wait      write READY and wait for a signal (SIGTERM, SIGHUP, Ctrl-Z)
  quiet     like wait, in quiet mode (Ctrl-C and Ctrl-Z are signals)
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.term import Terminal

mode = sys.argv[1]
term = Terminal()
term.install()
term.write("\x1b[?25l")  # a hidden cursor, as while a region is drawn
term.erase_region = lambda: term.write("<erased>")
term.on_resume = lambda: term.write("<resumed>")
context = term.quiet() if mode == "quiet" else term.raw()
with context:
    term.write("READY\n")
    if mode == "raise":
        raise RuntimeError("boom")
    if mode in ("wait", "quiet"):
        while True:
            time.sleep(0.05)
