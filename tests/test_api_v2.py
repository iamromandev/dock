"""Protocol v2 (AC1): v1 manifests are refused, but can still be read to compare an old install. No runtime needed."""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import test_catalog as tc  # noqa: E402
from cli import API_VERSION, approve  # noqa: E402
from cli.manifest import Manifest, ManifestError  # noqa: E402
from cli.store import Store  # noqa: E402


def make_v(parent: Path, name: str, api: str, version: str, permissions=()) -> Path:
    root = tc.make_plugin(parent, name, "dev.t.p", version)
    text = (root / "plugin.toml").read_text().replace('api_version = "2"', f'api_version = "{api}"')
    if permissions:
        text += "\n[capabilities]\npermissions = [" + ", ".join(f'"{p}"' for p in permissions) + "]\n"
    (root / "plugin.toml").write_text(text)
    return root


class ApiVersion(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dock-api-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_dock_speaks_v2(self):
        self.assertEqual(API_VERSION, "2")

    def test_v2_manifest_loads(self):
        self.assertEqual(Manifest.load(make_v(self.tmp, "a", "2", "1.0.0")).api_version, "2")

    def test_v1_manifest_is_refused_with_the_protocol_message(self):
        root = make_v(self.tmp, "a", "1", "1.0.0")
        with self.assertRaises(ManifestError) as cm:
            Manifest.load(root)
        self.assertEqual(str(cm.exception), "dev.t.p needs protocol v1, this dock speaks v2")

    def test_any_api_reads_a_v1_manifest(self):
        m = Manifest.load(make_v(self.tmp, "a", "1", "1.0.0", ["network"]), any_api=True)
        self.assertEqual((m.api_version, m.permissions), ("1", ["network"]))

    def test_installing_a_v1_plugin_is_refused(self):
        store = Store(self.tmp / "home")
        with self.assertRaises(ManifestError):
            store.install(make_v(self.tmp, "a", "1", "1.0.0"))

    def test_update_from_an_installed_v1_copy_works_and_approval_sees_old_permissions(self):
        store = Store(self.tmp / "home")
        old_src = make_v(self.tmp, "old", "2", "1.0.0", ["network"])
        store.install(old_src)
        # the installed copy is a v1 one: rewrite it in place, as an install from before the upgrade would be
        installed = Path(store.index()["dev.t.p"]["path"]) / "plugin.toml"
        installed.write_text(installed.read_text().replace('api_version = "2"', 'api_version = "1"'))
        with self.assertRaises(ManifestError):
            store.get("dev.t.p")  # invoking an installed v1 plugin still fails
        old = store.get("dev.t.p", any_api=True)
        new_src = make_v(self.tmp, "new", "2", "1.1.0", ["network", "fs:write:cwd"])
        self.assertEqual(approve.added(Manifest.load(new_src), old), ["fs:write:cwd"])
        old_version, manifest = store.update("dev.t.p", new_src)
        self.assertEqual((old_version, manifest.version), ("1.0.0", "1.1.0"))
        self.assertFalse((store.plugin_dir / "dev.t.p" / "1.0.0").exists())

    def test_remove_works_on_a_v1_install(self):
        store = Store(self.tmp / "home")
        store.install(make_v(self.tmp, "a", "2", "1.0.0"))
        installed = Path(store.index()["dev.t.p"]["path"]) / "plugin.toml"
        installed.write_text(installed.read_text().replace('api_version = "2"', 'api_version = "1"'))
        from unittest import mock
        with mock.patch("cli.store.isolation.forget_image") as forget:
            store.remove("dev.t.p")
        self.assertEqual(forget.call_args.args[1].api_version, "1")  # the image was named from the old manifest


class ShippedPluginsMoveToV2(unittest.TestCase):
    """AC14: every plugin in the repository is v2, and `plugin update` works from the v1 installs of hello and dm."""

    PLUGINS = (("hello", "dev.dock.hello", "0.1.0"), ("dm", "dev.dock.dm", "0.4.0"))
    FIXTURES = ("probe", "jail", "pidprobe")

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dock-api-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_every_plugin_and_fixture_declares_v2(self):
        for folder in [ROOT / "plugin" / n for n, _, _ in self.PLUGINS] + [ROOT / "tests" / "fixtures" / f
                                                                          for f in self.FIXTURES]:
            with self.subTest(folder=folder.name):
                self.assertEqual(Manifest.load(folder).api_version, "2")

    def test_update_from_the_v1_install_succeeds_and_asks_nothing_new(self):
        import re
        from unittest import mock
        for name, plugin_id, old_version in self.PLUGINS:
            with self.subTest(plugin=name):
                home = self.tmp / name / "home"
                store = Store(home)
                old = self.tmp / name / "old"
                shutil.copytree(ROOT / "plugin" / name, old)
                text = (old / "plugin.toml").read_text()
                text = re.sub(r'(?m)^version = ".*"$', f'version = "{old_version}"', text, count=1)
                (old / "plugin.toml").write_text(text)
                store.install(old)  # a v2 manifest at the old version ...
                installed = Path(store.index()[plugin_id]["path"]) / "plugin.toml"
                installed.write_text(installed.read_text().replace('api_version = "2"', 'api_version = "1"'))
                new = Manifest.load(ROOT / "plugin" / name)  # ... turned into the v1 install of before
                self.assertNotEqual(new.version, old_version)
                from cli.main import installed_manifest
                self.assertEqual(approve.added(new, installed_manifest(store, plugin_id)), [])
                with mock.patch("cli.store.isolation.forget_image"):
                    self.assertEqual(store.update(plugin_id, ROOT / "plugin" / name)[0], old_version)
                self.assertEqual(store.get(plugin_id).version, new.version)


if __name__ == "__main__":
    unittest.main()
