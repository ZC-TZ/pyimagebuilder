import base64
import contextlib
import gzip
import hashlib
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import tarfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from builder import build
from cas_store import CASStore, docker_archive_tag
from errors import BuildError
from image_cli import list_images, save_archive
from image_reader import ImageArchiveReader
from image_store import _archive_path, pull_image
from main import main as builder_main
from oci_writer import OCIImageWriter
from registry import OCI_GZIP_LAYER, parse_reference, pull, push
from rootfs import RootFSIndex


class RegistryHandler(BaseHTTPRequestHandler):
    blobs = {}
    manifests = {}
    token_requests = 0

    def log_message(self, *args):
        pass

    def reply(self, status, body=b"", headers=None):
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def authorized(self):
        if self.path.startswith("/service/token"):
            self.__class__.token_requests += 1
            expected = b"robot$test:secret"
            if self.headers.get("Authorization") != "Basic " + base64.b64encode(expected).decode():
                self.reply(401)
                return False
            self.reply(200, b'{"token":"test-token"}', {"Content-Type": "application/json"})
            return False
        if self.headers.get("Authorization") != "Bearer test-token":
            realm = "http://127.0.0.1:{}/service/token".format(self.server.server_port)
            self.reply(401, headers={"WWW-Authenticate":
                'Bearer realm="{}",service="harbor",scope="repository:p/app:pull,push"'.format(realm)})
            return False
        return True

    def do_HEAD(self):
        if not self.authorized():
            return
        digest = self.path.rsplit("/", 1)[-1]
        values = self.manifests if "/manifests/" in self.path else self.blobs
        self.reply(200 if digest in values else 404)

    def do_POST(self):
        if not self.authorized():
            return
        self.reply(202, headers={"Location": "/v2/p/app/blobs/uploads/1"})

    def do_PUT(self):
        if not self.authorized():
            return
        parsed = urlsplit(self.path)
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        digest = "sha256:" + hashlib.sha256(body).hexdigest()
        if "/blobs/uploads/" in parsed.path:
            expected = parse_qs(parsed.query)["digest"][0]
            if expected != digest:
                self.reply(400)
                return
            self.blobs[digest] = body
        else:
            self.manifests[parsed.path.rsplit("/", 1)[-1]] = body
        self.reply(201, headers={"Docker-Content-Digest": digest})

    def do_GET(self):
        if not self.authorized():
            return
        parsed = urlsplit(self.path)
        name = parsed.path.rsplit("/", 1)[-1]
        if "/manifests/" in parsed.path:
            body = self.manifests.get(name)
            if body is None:
                self.reply(404)
                return
            self.reply(200, body, {"Content-Type": "application/vnd.oci.image.manifest.v1+json",
                                   "Docker-Content-Digest": "sha256:" + hashlib.sha256(body).hexdigest()})
        else:
            body = self.blobs.get(name)
            self.reply(200, body) if body is not None else self.reply(404)


class PhaseElevenTests(unittest.TestCase):
    def test_pull_downloads_layers_concurrently_and_preserves_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            blobs = {}
            descriptors = []
            diff_ids = []
            for index in range(3):
                payload = ("value-{}".format(index)).encode()
                stream = io.BytesIO()
                with tarfile.open(fileobj=stream, mode="w") as archive:
                    member = tarfile.TarInfo("app/value")
                    member.size = len(payload)
                    archive.addfile(member, io.BytesIO(payload))
                raw = stream.getvalue()
                compressed = gzip.compress(raw)
                digest = "sha256:" + hashlib.sha256(compressed).hexdigest()
                blobs[digest] = compressed
                descriptors.append({"mediaType": OCI_GZIP_LAYER, "digest": digest,
                                    "size": len(compressed)})
                diff_ids.append("sha256:" + hashlib.sha256(raw).hexdigest())
            config = {"os": "linux", "architecture": "amd64",
                      "rootfs": {"type": "layers", "diff_ids": diff_ids}, "config": {}}
            config_raw = json.dumps(config).encode()
            config_digest = "sha256:" + hashlib.sha256(config_raw).hexdigest()
            blobs[config_digest] = config_raw
            manifest = {"schemaVersion": 2,
                        "config": {"mediaType": "application/vnd.oci.image.config.v1+json",
                                   "digest": config_digest, "size": len(config_raw)},
                        "layers": descriptors}
            manifest_raw = json.dumps(manifest).encode()
            manifest_digest = "sha256:" + hashlib.sha256(manifest_raw).hexdigest()
            lock = threading.Lock()
            active = 0
            peak = 0

            class FakeClient:
                def __init__(self, reference, *args, **kwargs):
                    self.reference = parse_reference(reference)
                    self.manifest_raw = manifest_raw

                def blob(self, descriptor, destination):
                    nonlocal active, peak
                    with lock:
                        active += 1
                        peak = max(peak, active)
                    try:
                        time.sleep(0.05)
                        Path(destination).write_bytes(blobs[descriptor["digest"]])
                    finally:
                        with lock:
                            active -= 1

            reference = "registry.example/p/app:test"
            output = root / "pulled.tar"
            from cas_store import CASStore
            store = CASStore(root / "store")
            with patch("registry.RegistryClient", FakeClient), \
                 patch("registry._selected_manifest", return_value=(manifest, manifest_digest)):
                pull(reference, output, workers=3, cas_store=store)
            self.assertGreaterEqual(peak, 2)
            ref = store.resolve(reference, source="registry")
            self.assertEqual([item["digest"] for item in ref["layers"]],
                             [item["digest"] for item in descriptors])
            store.export_docker(reference, root / "from-cas.tar", source="registry")
            direct_store = CASStore(root / "direct-store")
            direct_output = root / "not-written.tar"
            with patch("registry.RegistryClient", FakeClient), \
                 patch("registry._selected_manifest", return_value=(manifest, manifest_digest)):
                pull(reference, direct_output, workers=3, cas_store=direct_store,
                     cas_only=True)
            self.assertFalse(direct_output.exists())
            self.assertEqual(len(direct_store.open_base(reference, source="registry").layers), 3)
            direct_store.prune_orphans()
            self.assertTrue(all(direct_store._blob(diff_id).is_file() for diff_id in diff_ids))
            with tempfile.TemporaryDirectory() as unpacked:
                image = ImageArchiveReader(output, Path(unpacked)).read(reference)
                self.assertEqual(image.config["rootfs"]["diff_ids"], diff_ids)
                index = RootFSIndex()
                for layer in image.layers:
                    index.apply_layer(layer)
                self.assertEqual(index.read_file("app/value"), b"value-2")

    def test_reference_validation(self):
        self.assertEqual(parse_reference("harbor.local:8443/p/app:1").repository, "p/app")
        self.assertEqual(parse_reference("harbor.local/p/app").version, "latest")
        self.assertTrue(parse_reference("harbor.local/p/app@sha256:" + "a" * 64).digest)
        for value in ("harbor.local/P/app:1", "harbor.local/p/../app:1",
                      "user:secret@harbor.local/p/app:1"):
            with self.assertRaises(BuildError):
                parse_reference(value)

    def test_push_pull_and_digest_rejection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = root / "context"
            context.mkdir()
            (context / "payload").write_bytes(b"phase11")
            dockerfile = context / "Dockerfile"
            dockerfile.write_text("FROM scratch\nCOPY payload /payload\n")
            source = root / "source.tar"
            build(dockerfile, context, None, None, "example/local:11", source)
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
                reference = "127.0.0.1:{}/p/app:11".format(server.server_port)
                digest = push(source, reference, "robot$test", "secret", insecure_http=True)
                self.assertTrue(digest.startswith("sha256:"))
                with self.assertRaises(BuildError):
                    push(source, reference, "robot$test", "secret", insecure_http=True)
                self.assertGreater(RegistryHandler.token_requests, 0)
                manifest = json.loads(RegistryHandler.manifests["11"])
                self.assertEqual(manifest["layers"][0]["mediaType"], OCI_GZIP_LAYER)
                with self.assertRaises(BuildError):
                    pull(reference, root / "wrong-auth.tar", "robot$test", "bad",
                         insecure_http=True)
                output = root / "pulled.tar"
                pull(reference, output, "robot$test", "secret", insecure_http=True,
                     image_format="both")
                with tempfile.TemporaryDirectory() as unpacked:
                    image = ImageArchiveReader(output, Path(unpacked)).read(reference)
                    index = RootFSIndex()
                    for layer in image.layers:
                        index.apply_layer(layer)
                    self.assertEqual(index.read_file("payload"), b"phase11")
                OCIImageWriter().verify(root / "pulled.oci.tar", reference)
                RegistryHandler.manifests[digest] = RegistryHandler.manifests["11"]
                by_digest = root / "by-digest.tar"
                digest_reference = "127.0.0.1:{}/p/app@{}".format(server.server_port, digest)
                pull(digest_reference,
                     by_digest, "robot$test", "secret", insecure_http=True)
                self.assertTrue(by_digest.is_file())
                managed_store = root / "digest-store"
                with patch.dict(os.environ, {"PYIMAGEBUILDER_REGISTRY_PASSWORD": "secret"}):
                    base_source, downloaded = pull_image(
                        digest_reference, store=managed_store, username="robot$test",
                        insecure_http=True, materialize_tar=False)
                    self.assertTrue(downloaded)
                    self.assertFalse(_archive_path(managed_store, digest_reference,
                                                  "linux/amd64", "registry").exists())
                    self.assertEqual(CASStore(managed_store).resolve(
                        digest_reference, source="registry")["reference"], digest_reference)
                    derived_context = root / "derived-context"
                    derived_context.mkdir()
                    (derived_context / "Dockerfile").write_text(
                        "FROM " + digest_reference + "\nENV AUDIT=1\n")
                    build(derived_context / "Dockerfile", derived_context, base_source, None,
                          "example/derived:1", root / "derived-digest.tar")
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(builder_main([
                            "build", str(derived_context), "--offline", "--image-store",
                            str(managed_store), "-t", "example/derived-cli:1", "-o",
                            str(root / "derived-cli.tar")]), 0)
                    saved = root / "saved-digest.tar"
                    save_archive(digest_reference, saved, managed_store, source="registry")
                    with tempfile.TemporaryDirectory() as unpacked:
                        ImageArchiveReader(saved, Path(unpacked)).read(
                            docker_archive_tag(digest_reference))
                    archive, refreshed = pull_image(
                        digest_reference, store=managed_store, username="robot$test",
                        insecure_http=True, refresh=True)
                    self.assertTrue(refreshed)
                    self.assertTrue(archive.is_file())
                    self.assertTrue(any(digest_reference in item.get("tags", [])
                                        for item in list_images(managed_store)["images"]))
                child = RegistryHandler.manifests["11"]
                RegistryHandler.manifests["multi"] = json.dumps({
                    "schemaVersion": 2,
                    "mediaType": "application/vnd.oci.image.index.v1+json",
                    "manifests": [{"mediaType": "application/vnd.oci.image.manifest.v1+json",
                                   "digest": digest, "size": len(child),
                                   "platform": {"os": "linux", "architecture": "amd64"}}]
                }, separators=(",", ":")).encode()
                pull("127.0.0.1:{}/p/app:multi".format(server.server_port),
                     root / "multi.tar", "robot$test", "secret", insecure_http=True)
                self.assertTrue((root / "multi.tar").is_file())
                oci_source = root / "source.oci.tar"
                build(dockerfile, context, None, None, "example/local:12", oci_source,
                      image_format="oci")
                second_digest = push(oci_source,
                    "127.0.0.1:{}/p/app:12".format(server.server_port),
                    "robot$test", "secret", insecure_http=True)
                self.assertTrue(second_digest.startswith("sha256:"))
                arm_source = root / "arm.oci.tar"
                build(dockerfile, context, None, None, "example/local:arm", arm_source,
                      image_format="oci", target_platform="linux/arm64")
                arm_reference = "127.0.0.1:{}/p/app:arm".format(server.server_port)
                push(arm_source, arm_reference, "robot$test", "secret", insecure_http=True)
                arm_output = root / "arm-pulled.tar"
                pull(arm_reference, arm_output, "robot$test", "secret", insecure_http=True,
                     target_platform="linux/arm64")
                with tempfile.TemporaryDirectory() as arm_unpack:
                    arm_image = ImageArchiveReader(arm_output, Path(arm_unpack)).read(
                        arm_reference, "linux/arm64")
                    self.assertEqual(arm_image.config["architecture"], "arm64")
                with self.assertRaises(BuildError):
                    pull(arm_reference, root / "wrong-arm.tar", "robot$test", "secret",
                         insecure_http=True)
                layer_digest = manifest["layers"][0]["digest"]
                original = RegistryHandler.blobs[layer_digest]
                RegistryHandler.blobs[layer_digest] = original + b"tampered"
                with self.assertRaises(BuildError):
                    pull(reference, root / "bad.tar", "robot$test", "secret", insecure_http=True)
                self.assertFalse((root / "bad.tar").exists())
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
