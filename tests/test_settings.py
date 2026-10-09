"""验证 Fast、统一 CLI 和独立客户端共用配置。"""

import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fast
import main
import registry
from settings import load_settings
from errors import BuildError
from test_phase1 import make_base
from test_phase11 import RegistryHandler


class SettingsTests(unittest.TestCase):
    def test_saved_config_defaults_to_project_image_store(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "config.json"
            path.write_text(json.dumps({"schemaVersion": 2, "fast": {},
                                        "cache": {}, "repositories": {}}), encoding="utf-8")
            settings, _ = load_settings(path)
            self.assertEqual(settings["cache"]["imageStore"], root / "data" / "image-store")

    def test_fast_profiles_share_cache_and_preserve_other_sections(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = make_base(root)
            path = root / "config.json"
            path.write_text(json.dumps({"schemaVersion": 2,
                "cache": {"layerCache": "./layers", "imageStore": "./images"},
                "repositories": {"harbor.internal": {"source": "registry"}},
                "fast": {"profiles": {}}}), encoding="utf-8")
            options = ["init", "--base-tar", str(base), "--base-image", "example/base:1",
                       "--flavor", "tomcat", "--profile", "tomcat", "--config", str(path)]
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(fast.main(options), 0)
            settings, _ = load_settings(path)
            self.assertEqual(settings["cache"]["layerCache"], root / "layers")
            self.assertIn("harbor.internal", settings["repositories"])
            self.assertEqual(fast.load_profile(path, "tomcat")["baseImage"], "example/base:1")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(fast.main([*options[:1], *options[1:3],
                    "--base-image", "example/base:1", "--flavor", "tomcat",
                    "--profile", "tomcat10", "--config", str(path)]), 0)
            self.assertEqual(len(load_settings(path)[0]["fast"]["profiles"]), 2)

    def test_legacy_fast_base_cache_field_requires_shared_setting(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "config.json"
            path.write_text(json.dumps({"schemaVersion": 2, "cache": {}, "repositories": {},
                "fast": {"profiles": {"tomcat": {"flavor": "tomcat",
                    "baseImage": "example/base:1", "baseUrl": "https://example/base:1",
                    "baseCacheDir": "./old-cache"}}}}), encoding="utf-8")
            with self.assertRaisesRegex(BuildError, "cache.imageStore"):
                fast.load_profile(path, "tomcat")

    def test_registry_credentials_and_store_are_shared(self):
        RegistryHandler.blobs = {}
        RegistryHandler.manifests = {}
        RegistryHandler.token_requests = 0
        try:
            server = ThreadingHTTPServer(("127.0.0.1", 0), RegistryHandler)
        except OSError as exc:
            self.skipTest("Local socket unavailable: " + str(exc))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                base = make_base(root)
                host = "127.0.0.1:{}".format(server.server_port)
                path = root / "config.json"
                path.write_text(json.dumps({"schemaVersion": 2,
                    "cache": {"imageStore": "./images", "layerCache": "./layers"},
                    "repositories": {host: {"source": "registry", "username": "robot$test",
                                           "passwordEnv": "TEST_REGISTRY_PASSWORD",
                                           "insecureHttp": True}},
                    "fast": {"profiles": {}}}), encoding="utf-8")
                with patch.dict(os.environ, {"TEST_REGISTRY_PASSWORD": "secret"}):
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(main.main(["push", str(base), host + "/p/app:1",
                                                    "--config", str(path)]), 0)
                        self.assertEqual(main.main(["manifest", "inspect", host + "/p/app:1",
                                                    "--config", str(path)]), 0)
                        self.assertEqual(main.main(["pull", host + "/p/app:1",
                                                    "--config", str(path)]), 0)
                        context = root / "context"
                        context.mkdir()
                        (context / "Dockerfile").write_text(
                            "FROM " + host + "/p/app:1\nENV MODE=shared\n", encoding="utf-8")
                        self.assertEqual(main.main(["build", "-t", "local/app:1", "-o",
                                                    str(root / "built.tar"), "--config", str(path),
                                                    str(context)]), 0)
                        self.assertEqual(registry.main(["push", host + "/p/app:2",
                                                         "--archive", str(base), "--config", str(path)]), 0)
                self.assertEqual(len(list((root / "images").glob("*.tar"))), 1)
                self.assertTrue((root / "built.tar").is_file())
                self.assertIn("2", RegistryHandler.manifests)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
