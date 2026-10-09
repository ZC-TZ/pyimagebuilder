"""仅使用 localhost，验证 Fast 的 Artifactory 鉴权下载与缓存复用。"""

import base64
import contextlib
import gzip
import hashlib
import io
import json
import os
import sys
import tarfile
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fast
import image_store
import main
from cas_store import CASStore
from artifactory_download import ProgressDisplay, materialize_layer, safe_relative_name
from image_reader import ImageArchiveReader
from rootfs import RootFSIndex


def _tar_bytes():
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        data = b"base"
        member = tarfile.TarInfo("app/base.txt")
        member.size = len(data)
        archive.addfile(member, io.BytesIO(data))
    return stream.getvalue()


class FastArtifactoryTests(unittest.TestCase):
    def test_directory_links_reject_windows_path_escape(self):
        for href in ("..%5Coutside", "%2e%2e%5csecret", "C:%5Coutside",
                     "folder%5C..%5Coutside", "file%00name", "."):
            with self.subTest(href=href):
                self.assertIsNone(safe_relative_name(href))
        self.assertEqual(safe_relative_name("blobs/sha256__abc"), "blobs/sha256__abc")

    def test_conversion_verifies_compressed_digest_in_one_pass(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = _tar_bytes()
            compressed = gzip.compress(raw)
            blob = root / "blob"
            blob.write_bytes(compressed)
            diff_id = "sha256:" + hashlib.sha256(raw).hexdigest()
            blob_digest = "sha256:" + hashlib.sha256(compressed).hexdigest()
            result = materialize_layer(blob, root / "layer.tar", "+gzip", diff_id,
                                       1, 1, ProgressDisplay(), blob_digest)
            self.assertEqual(result, diff_id)
            with self.assertRaisesRegex(RuntimeError, "blob SHA-256"):
                materialize_layer(blob, root / "bad.tar", "+gzip", diff_id,
                                  1, 1, ProgressDisplay(), "sha256:" + "0" * 64)

    def test_authenticated_download_then_cached_build(self):
        layer = _tar_bytes()
        compressed = gzip.compress(layer)
        config = json.dumps({"architecture": "amd64", "os": "linux", "config": {},
                             "rootfs": {"type": "layers", "diff_ids": [
                                 "sha256:" + hashlib.sha256(layer).hexdigest()]},
                             "history": [{"created_by": "base"}]},
                            separators=(",", ":")).encode()
        config_hash = hashlib.sha256(config).hexdigest()
        layer_hash = hashlib.sha256(compressed).hexdigest()
        manifest = json.dumps({"schemaVersion": 2,
                               "config": {"digest": "sha256:" + config_hash},
                               "layers": [{"digest": "sha256:" + layer_hash,
                                           "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip"}]},
                              separators=(",", ":")).encode()
        files = {"manifest.json": manifest, "sha256__" + config_hash: config,
                 "sha256__" + layer_hash: compressed}
        counts = []
        authorization = "Basic " + base64.b64encode(b"test-user:test-password").decode()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_HEAD(self):
                self.handle_file(False)

            def do_GET(self):
                self.handle_file(True)

            def handle_file(self, body):
                counts.append(self.path)
                if self.headers.get("Authorization") != authorization:
                    self.send_response(401)
                    self.end_headers()
                    return
                name = self.path.rsplit("/", 1)[-1]
                if not name:
                    data = ("<html>" + "".join('<a href="{}">{}</a>'.format(key, key)
                                                 for key in files) + "</html>").encode()
                    content_type = "text/html"
                elif name in files:
                    data = files[name]
                    content_type = "application/octet-stream"
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if body:
                    self.wfile.write(data)

        try:
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        except OSError as exc:
            self.skipTest("Local HTTP server unavailable: " + str(exc))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                profile = root / "config.json"
                reference = "127.0.0.1:{}/repo/base:1".format(server.server_port)
                options = ["init", "--base-url", "http://" + reference,
                           "--flavor", "tomcat", "--owner", "1000:1000",
                           "--username", "test-user", "--password-env", "TEST_ARTIFACTORY_PASSWORD",
                           "--base-cache-dir", str(root / "store"),
                           "--config", str(profile)]
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(fast.main(options), 0)
                saved = json.loads(profile.read_text(encoding="utf-8"))
                self.assertEqual(saved["fast"]["profiles"]["default"]["baseImage"], reference)
                self.assertEqual(saved["repositories"]["127.0.0.1:{}".format(server.server_port)]["username"],
                                 "test-user")
                self.assertNotIn("test-password", profile.read_text(encoding="utf-8"))
                self.assertEqual(saved["cache"]["imageStore"], str(root / "store"))
                self.assertNotIn("baseCacheDir", saved["fast"]["profiles"]["default"])
                war = root / "report.war"
                war.write_bytes(b"war")
                with patch.dict(os.environ, {"TEST_ARTIFACTORY_PASSWORD": "test-password"}):
                    with contextlib.redirect_stdout(io.StringIO()):
                        first, tag = fast.quick_build(war, "sfcx", "1.3.4", profile)
                self.assertEqual(tag, "sfcx_back:1.3.4")
                self.assertTrue(first.is_file())
                count_after_first = len(counts)
                stored = image_store._archive_path(root / "store", reference,
                                                   "linux/amd64", "artifactory")
                self.assertFalse(stored.exists())  # 受管理的构建直接消费 CAS，不需要基础 tar 适配文件。
                cas_image = CASStore(root / "store").resolve(reference, "linux/amd64", "artifactory")
                self.assertEqual(cas_image["layers"][0]["digest"], "sha256:" + layer_hash)
                self.assertTrue(cas_image["layers"][0]["mediaType"].endswith("+gzip"))
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(main.main(["images", "--config", str(profile)]), 0)
                self.assertEqual(json.loads(output.getvalue())["images"][0]["tags"], [reference])
                exported = root / "saved-base.tar"
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(main.main(["save", reference, "--source", "artifactory",
                                                "--config", str(profile), "-o", str(exported)]), 0)
                self.assertEqual(exported.read_bytes(), stored.read_bytes())
                stored.unlink()  # 后续构建也不能重新生成基础 tar，防止重复打包和读取。
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(main.main(["images", "--config", str(profile)]), 0)
                self.assertEqual(json.loads(output.getvalue())["images"][0]["storage"], "cas")
                context = root / "ordinary"
                context.mkdir()
                (context / "Dockerfile").write_text("FROM " + reference + "\nENV TEST=1\n")
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(main.main(["build", str(context), "--offline",
                                                "--config", str(profile), "-t", "test/app:1",
                                                "-o", str(root / "ordinary.tar")]), 0)
                self.assertEqual(len(counts), count_after_first)
                self.assertFalse(stored.exists())
                with tempfile.TemporaryDirectory() as extracted:
                    image = ImageArchiveReader(first, Path(extracted)).read(tag)
                    index = RootFSIndex()
                    for path in image.layers:
                        index.apply_layer(path)
                    self.assertEqual(index.kind("soft/tomcat/webapps/sfcx.war"), "file")
                with contextlib.redirect_stdout(io.StringIO()):
                    second, _ = fast.quick_build(war, "sfcx", "1.3.4", profile,
                                                 output=root / "second.tar")
                self.assertTrue(second.is_file())
                self.assertEqual(len(counts), count_after_first)
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(main.main(["rmi", reference, "--source", "artifactory",
                                                "--config", str(profile)]), 0)
                self.assertFalse(stored.exists())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
