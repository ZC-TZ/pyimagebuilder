"""验证统一入口和独立脚本的镜像管理行为。"""

import contextlib
import io
import json
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import image_cli
import main as builder_main
from cache import LayerCache
from errors import BuildError
from test_phase1 import make_base
from test_phase11 import RegistryHandler


class ImageCommandTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.archive = make_base(self.root)

    def command(self, *args, standalone=False):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = (image_cli.main if standalone else builder_main.main)(list(args))
        self.assertEqual(code, 0)
        return json.loads(output.getvalue())

    def test_inspect_verify_history_and_retag(self):
        archive = str(self.archive)
        inspected = self.command("inspect", archive)
        self.assertEqual(inspected["selected_tag"], "example/base:1")
        self.assertEqual(inspected["platform"], "linux/amd64")
        self.assertEqual(len(inspected["layers"]), 2)
        verified = self.command("verify", archive, standalone=True)
        self.assertEqual(verified["layers_verified"], 2)
        history = self.command("history", archive)
        self.assertEqual(history["history"][0]["created_by"], "second")
        new_archive = self.root / "retagged.tar"
        tagged = self.command("tag", archive, "example/renamed:2", "-o", str(new_archive))
        self.assertEqual(tagged["tag"], "example/renamed:2")
        self.assertEqual(self.command("verify", str(new_archive))["tag"], "example/renamed:2")
        self.assertFalse(self.command("inspect", str(new_archive))["selected_tag"] == "example/base:1")
        with self.assertRaises(BuildError):
            image_cli.tag_archive(self.archive, "invalid", self.root / "invalid.tar")

    def test_images_and_cache(self):
        store = self.root / "store"
        store.mkdir()
        shutil.copyfile(self.archive, store / "base.tar")
        images = self.command("images", "--image-store", str(store), standalone=True)
        self.assertEqual(images["images"][0]["tags"], ["example/base:1"])
        cache = LayerCache(self.root / "cache")
        orphan = cache.layers / "orphan.tar"
        orphan.write_bytes(b"unused")
        report = self.command("cache", "ls", "--cache-dir", str(cache.root))
        self.assertEqual(len(report["orphan_layers"]), 1)
        pruned = self.command("cache", "prune", "--cache-dir", str(cache.root), standalone=True)
        self.assertTrue(pruned["pruned"])
        self.assertFalse(orphan.exists())

    def test_remote_commands_use_registry_client(self):
        with patch.object(image_cli, "inspect_manifest", return_value={"digest": "sha256:test"}) as inspect:
            self.assertEqual(self.command("manifest", "inspect", "host.test/app:1")["digest"], "sha256:test")
            inspect.assert_called_once()
        with patch.object(image_cli, "push", return_value="sha256:test") as upload:
            result = self.command("push", str(self.archive), "host.test/app:1")
            self.assertEqual(result["digest"], "sha256:test")
            upload.assert_called_once()

    def test_push_and_manifest_inspect_against_local_registry(self):
        RegistryHandler.blobs = {}
        RegistryHandler.manifests = {}
        RegistryHandler.token_requests = 0
        try:
            server = ThreadingHTTPServer(("127.0.0.1", 0), RegistryHandler)
        except OSError as exc:
            self.skipTest("Local socket unavailable: " + str(exc))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            reference = "127.0.0.1:{}/p/app:1".format(server.server_port)
            credentials = ["--username", "robot$test", "--insecure-http"]
            with patch.dict("os.environ", {"PYIMAGEBUILDER_REGISTRY_PASSWORD": "secret"}):
                pushed = self.command("push", str(self.archive), reference, *credentials)
                inspected = self.command("manifest", "inspect", reference, *credentials)
            self.assertEqual(inspected["digest"], pushed["digest"])
            self.assertEqual(inspected["manifest"]["schemaVersion"], 2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
