"""Fakes for the flow tests of the CLI: a copy of the helpers in packages/host/tests/test_flow.py."""

import tempfile
from pathlib import Path

from dock_host.inputs import InputSpec
from dock_host.manifest import Command, ContainerRun, Manifest, ManifestError

GOOD = """
[params.url]
type = "string"
required = true

[[steps]]
plugin = "dev.dock.dm"
command = "dm"
inputs = { url = "{params.url}", dir = "{work}" }
label = "download"

[[steps]]
plugin = "dev.dock.unzip"
command = "unzip"
inputs = { file = "{steps.1.files[0]}", count = 2 }
"""


def manifest(pid, commands, permissions=(), env=()):
    return Manifest(
        pid, "1.0", "2", "", Path("/x"), ContainerRun("run"), list(commands), list(permissions), list(env)
    )


class FakeStore:
    def __init__(self, *manifests, home=None):
        self.home = Path(home) if home is not None else Path(tempfile.mkdtemp(prefix="dock-home-"))
        self.by_id = {m.id: m for m in manifests}
        self.dirs = None

    def index(self):
        return {pid: {} for pid in self.by_id}

    def get(self, plugin_id, any_api=False):
        if plugin_id not in self.by_id:
            raise ManifestError(f"{plugin_id} is not installed")
        return self.by_id[plugin_id]


def make_store(home=None):
    dm = Command("dm", "", [InputSpec("url", "string", True), InputSpec("dir", "string")])
    unzip = Command("unzip", "", [InputSpec("file", "string", True), InputSpec("count", "int")])
    free = Command("free")  # inputs undeclared
    return FakeStore(
        manifest("dev.dock.dm", [dm, free], ["network"], ["TOKEN"]),
        manifest("dev.dock.unzip", [unzip]),
        home=home,
    )


class Recorder:
    """A fake StepRunner: records calls, answers from a script."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, plugin_id, command, inputs):
        self.calls.append((plugin_id, command, inputs))
        a = self.answers.pop(0) if self.answers else {"exit_code": 0}
        if isinstance(a, BaseException):
            raise a
        return a
