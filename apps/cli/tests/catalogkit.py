"""Plugin-folder builders and throwaway-DOCK_HOME cases shared by the CLI tests (a copy of the host tests' helpers,
plus `CliCase.dock`, which runs `python -m src`)."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import dockenv
from dock_host import catalog
from dock_host.store import Store




def make_plugin(
    parent,
    name,
    plugin_id,
    version="0.1.0",
    commands=(("hello", "Greet someone", ["greet"]),),
    keywords_raw=None,
):
    """A plugin folder `parent/name`: plugin.toml and a one-line Dockerfile. commands: (name, summary, keywords)."""
    root = Path(parent) / name
    root.mkdir(parents=True, exist_ok=True)
    text = (
        f'id = "{plugin_id}"\nversion = "{version}"\napi_version = "2"\ndescription = "A python sample"\n\n'
        '[run]\ncontainer = { cmd = "python3", args = ["/plugin/main.py"] }\n'
    )
    for cname, summary, kws in commands:
        text += f'\n[[commands]]\nname = "{cname}"\nsummary = "{summary}"\n'
        if keywords_raw is not None:
            text += f"keywords = {keywords_raw}\n"
        elif kws:
            text += "keywords = [" + ", ".join(json.dumps(k) for k in kws) + "]\n"
    (root / "plugin.toml").write_text(text)
    (root / "Dockerfile").write_text("FROM scratch\n")
    return root


def greeter(parent):
    """A two-command plugin folder `parent/greeter` (dev.dock.greeter 0.2.0) with keywords on both commands."""
    return make_plugin(
        parent,
        "greeter",
        "dev.dock.greeter",
        "0.2.0",
        commands=(
            ("hello", "Greet someone: hello [name]", ["greet", "welcome", "name"]),
            ("count", "Count with a progress bar: count [n]", ["progress", "number", "demo"]),
        ),
    )


class TmpCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dock-catalog-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.cat = self.tmp / "cat"
        self.cat.mkdir()
        patcher = mock.patch.object(
            catalog, "builtin_dir", return_value=None
        )  # the repo's plugins are not these tests' catalog
        patcher.start()
        self.addCleanup(patcher.stop)

    def config(self, text):
        (self.home / "config.toml").write_text(text)

    def catalog_config(self, *folders):
        self.config("[catalog]\npaths = [" + ", ".join(json.dumps(str(f)) for f in folders) + "]\n")

    def store(self):
        return Store(self.home)


FAKE_RUNTIME = "#!/bin/sh\necho \"$@\" >> '{log}'\nexit 1\n"


class CliCase(TmpCase):
    """`python -m src` in a throwaway DOCK_HOME whose container runtime is a script that records its calls."""

    def setUp(self):
        super().setUp()
        self.log = self.tmp / "runtime-calls"
        self.runtime = self.tmp / "docker"
        self.runtime.write_text(FAKE_RUNTIME.format(log=self.log))
        self.runtime.chmod(0o755)
        self.config(f'[container]\nruntime = "{self.runtime}"\n')
        self.env = {
            **os.environ,
            "DOCK_HOME": str(self.home),
            "PYTHONPATH": str(ROOT),
            "DOCK_NO_READLINE": "1",
            "HOME": str(self.tmp),
        }
        self.hello = greeter(self.cat)
        self.outside = greeter(
            self.tmp / "outside"
        )  # the same plugin, in a folder that is not in the catalog
        self.catalog_config(self.cat)

    def catalog_config(self, *folders):
        dockenv.add_config(
            self.home, "[catalog]\npaths = [" + ", ".join(json.dumps(str(f)) for f in folders) + "]\n"
        )

    def dock(self, *args, stdin="", env=None):
        return subprocess.run(
            [sys.executable, "-m", "src", *args],
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
            env=env or self.env,
            cwd=self.tmp,
            timeout=60,
            **dockenv.SIGINT_DEFAULT,
        )

    def runtime_calls(self):
        return self.log.read_text() if self.log.exists() else ""

    def index(self):
        return self.store().index()
