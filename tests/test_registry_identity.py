"""验证 Registry 原始 manifest 身份及 CAS 导出的输出所有权。"""

import hashlib
import json
import sys
import tarfile
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cas_store import CASStore
from image_store import pull_image
from image_writer import ImageArchiveWriter
from oci_writer import CONFIG_TYPE, INDEX_TYPE, LAYER_TYPE, MANIFEST_TYPE
from registry import pull
from test_phase1 import make_base


def digest(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


class RegistryIdentityTests(unittest.TestCase):
    def test_original_manifest_bytes_survive_pull_and_offline_reuse(self):
        """使用真实本地 HTTP transport，覆盖 tag、digest 和 index 子 manifest。"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = make_base(root)
            with tarfile.open(base) as archive:
                entry = json.load(archive.extractfile("manifest.json"))[0]
                config = archive.extractfile(entry["Config"]).read()
                blobs = {digest(config): config}
                layers = []
                for name in entry["Layers"]:
                    raw = archive.extractfile(name).read()
                    blobs[digest(raw)] = raw
                    layers.append({"mediaType": LAYER_TYPE, "digest": digest(raw), "size": len(raw)})
            manifest = {"schemaVersion": 2, "mediaType": MANIFEST_TYPE,
                        "config": {"mediaType": CONFIG_TYPE, "digest": digest(config), "size": len(config)},
                        "layers": layers, "annotations": {"example.description": "中文注释"}}
            raw_manifest = (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode()
            child_digest = digest(raw_manifest)
            index = {"schemaVersion": 2, "mediaType": INDEX_TYPE,
                     "manifests": [{"mediaType": MANIFEST_TYPE, "digest": child_digest,
                                    "size": len(raw_manifest),
                                    "platform": {"os": "linux", "architecture": "amd64"}}]}
            raw_index = json.dumps(index, indent=4).encode()
            manifests = {"1": raw_manifest, child_digest: raw_manifest, "multi": raw_index}

            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *_args):
                    pass

                def do_GET(self):
                    name = self.path.rsplit("/", 1)[-1]
                    raw = manifests.get(name) if "/manifests/" in self.path else blobs.get(name)
                    if raw is None:
                        self.send_error(404)
                        return
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(raw)))
                    if "/manifests/" in self.path:
                        self.send_header("Content-Type", INDEX_TYPE if name == "multi" else MANIFEST_TYPE)
                        self.send_header("Docker-Content-Digest", digest(raw))
                    self.end_headers()
                    self.wfile.write(raw)

            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                for suffix in (":1", "@" + child_digest, ":multi"):
                    with self.subTest(reference=suffix):
                        reference = "127.0.0.1:{}/p/app{}".format(server.server_port, suffix)
                        store = root / ("store-" + hashlib.sha256(suffix.encode()).hexdigest()[:8])
                        source, downloaded = pull_image(reference, store=store, insecure_http=True,
                                                       materialize_tar=False)
                        self.assertTrue(downloaded)
                        cas = CASStore(store)
                        ref = cas.resolve(reference, source="registry")
                        self.assertEqual(ref["manifest"], child_digest)
                        self.assertEqual(cas._blob(child_digest).read_bytes(), raw_manifest)
                        cached, downloaded = pull_image(reference, store=store, offline=True,
                                                        materialize_tar=False)
                        self.assertFalse(downloaded)
                        self.assertEqual(source, cached)
                direct_reference = "127.0.0.1:{}/p/app@{}".format(server.server_port, child_digest)
                direct_store = CASStore(root / "direct-store")
                pull(direct_reference, root / "not-written.tar", insecure_http=True,
                     cas_store=direct_store, cas_only=True)
                self.assertEqual(direct_store.resolve(direct_reference, source="registry")["manifest"],
                                 child_digest)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

    def test_cas_export_creation_race_preserves_existing_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = make_base(root)
            store = CASStore(root / "store")
            store.import_docker(archive, "example/base:1", "linux/amd64")
            output = root / "saved.tar"
            writer = ImageArchiveWriter()
            raced = []

            def competing_writer():
                if not raced:
                    output.write_bytes(b"other export")
                    raced.append(True)
                return writer

            with patch("cas_store.ImageArchiveWriter", side_effect=competing_writer):
                with self.assertRaises(FileExistsError):
                    store.export_docker("example/base:1", output)
            self.assertEqual(output.read_bytes(), b"other export")

    def test_cas_export_write_failure_removes_only_its_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = make_base(root)
            store = CASStore(root / "store")
            store.import_docker(archive, "example/base:1", "linux/amd64")
            output = root / "saved.tar"

            def interrupted(path, _config, _layers, _tag, **kwargs):
                stream = kwargs.get("fileobj")
                if stream is None:
                    Path(path).write_bytes(b"partial")
                else:
                    stream.write(b"partial")
                raise OSError("export interrupted")

            with patch.object(ImageArchiveWriter, "write", side_effect=interrupted):
                with self.assertRaisesRegex(OSError, "export interrupted"):
                    store.export_docker("example/base:1", output)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
