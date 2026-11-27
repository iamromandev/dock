"""R5: what the host core has to say to the user goes through one hook, stderr by default."""

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import dockenv
from dock_host.store import Store

TEXT = "dock: invalid [shell] idle_timeout -1; using 300"


def store(**kw) -> Store:
    home = tempfile.mkdtemp(prefix="dock-notice-")
    dockenv.add_config(home, "[shell]\nidle_timeout = -1\n")
    return Store(Path(home), **kw)


def test_message_goes_to_the_hook_and_by_default_to_stderr(capfd):
    # bug: a core module still prints to stderr itself, so an embedding host cannot take the text;
    # or the default hook drops the text or writes it to stdout
    seen = []
    store(notice=seen.append).idle_timeout()
    assert seen == [TEXT]
    assert capfd.readouterr().err == ""
    store().idle_timeout()
    out = capfd.readouterr()
    assert (out.out, out.err) == ("", TEXT + "\n")
