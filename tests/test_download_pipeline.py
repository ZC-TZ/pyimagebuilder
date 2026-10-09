"""验证下载边界、tar/CAS 一致性和基础镜像更新后的构建缓存。"""

import hashlib
import contextlib
import gzip
import io
import json
import shutil
import sys
import tempfile
import tarfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import artifactory_download as artifacts
import registry
from builder import build
from cas_store import CASStore
from errors import BuildError
from image_store import _archive_path, pull_image
from image_reader import ImageArchiveReader
from main import main
from rootfs import RootFSIndex
from test_phase1 import make_base


class Response(io.BytesIO):
    def __init__(self, raw, status=200, headers=None):
        super().__init__(raw)
        self.status = status
        self.headers = headers or {}

    def getcode(self):
        return self.status


class DownloadPipelineTests(unittest.TestCase):
    def test_valid_resume_and_truncated_body(self):
        for truncated in (False, True):
            with self.subTest(truncated=truncated), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / "blob").write_bytes(b"ABC")
                stats = artifacts.TransferStats()
                stats.add(3)
                first = Response(b"D" if truncated else b"DEF", 206,
                                 {"Content-Range": "bytes 3-5/6", "Content-Length": "3"})
                with patch.object(artifacts, "request",
                                  side_effect=[first, Response(b"ABCDEF")]) as request, \
                        patch.object(artifacts.time, "sleep"):
                    artifacts.download_one(object(), "https://example/repo/", "blob", root,
                                           None, artifacts.AdaptiveLimiter(1), stats, Mock())
                self.assertEqual((root / "blob").read_bytes(), b"ABCDEF")
                self.assertEqual(request.call_count, 2 if truncated else 1)
                self.assertEqual(stats.snapshot(), 6)

    def test_zero_byte_artifact_is_created(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(artifacts, "request", return_value=Response(b"")):
                size = artifacts.download_one(object(), "https://example/repo/", "empty",
                                              root, 0, artifacts.AdaptiveLimiter(1),
                                              artifacts.TransferStats(), Mock())
            self.assertEqual(size, 0)
            self.assertTrue((root / "empty").is_file())

    def test_wrong_range_response_restarts_without_corrupt_prefix(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "blob").write_bytes(b"abc")
            stats = artifacts.TransferStats()
            stats.add(3)
            replies = [Response(b"XYZ", 206, {"Content-Range": "bytes 0-2/6"}),
                       Response(b"ABCDEF")]
            with patch.object(artifacts, "request", side_effect=replies) as request, \
                    patch.object(artifacts.time, "sleep"):
                artifacts.download_one(object(), "https://example/repo/", "blob", root,
                                       6, artifacts.AdaptiveLimiter(1), stats, Mock())
            self.assertEqual((root / "blob").read_bytes(), b"ABCDEF")
            self.assertEqual(request.call_count, 2)
            self.assertEqual(stats.snapshot(), 6)

    def test_cached_tar_and_cas_disagreement_is_rejected(self):
        for source in ("local", "registry", "artifactory"):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                original = make_base(root)
                context = root / "context"
                context.mkdir()
                (context / "Dockerfile").write_text("FROM scratch\nENV BASE=changed\n")
                newer = root / "newer.tar"
                reference = "example/base:1"
                build(context / "Dockerfile", context, None, None, reference, newer)
                store = root / "store"
                CASStore(store).import_docker(original, reference, "linux/amd64", source)
                shutil.copyfile(newer, _archive_path(store, reference, "linux/amd64", source))
                with self.assertRaisesRegex(BuildError, "disagree"):
                    pull_image(reference, store=store, offline=True,
                               source="registry" if source == "local" else source)

    def test_cached_tar_layers_are_checked_even_when_config_matches_cas(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = make_base(root)
            store = root / "store"
            reference = "example/base:1"
            CASStore(store).import_docker(original, reference, "linux/amd64", "registry")
            cached = _archive_path(store, reference, "linux/amd64", "registry")
            with tarfile.open(original) as source, tarfile.open(cached, "w") as target:
                for member in source:
                    stream = source.extractfile(member) if member.isfile() else None
                    raw = stream.read() if stream is not None else None
                    if member.name == "one/layer.tar":
                        raw = raw.replace(b"base", b"evil", 1)
                    target.addfile(member, io.BytesIO(raw) if raw is not None else None)
            with self.assertRaisesRegex(BuildError, "DiffID"):
                pull_image(reference, store=store, offline=True)

    def test_registry_config_rejected_before_layer_download(self):
        for rootfs in ([], None, {"type": "bad", "diff_ids": []},
                       {"type": "layers", "diff_ids": ["not-a-digest"]}):
            with self.subTest(rootfs=rootfs), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                raw = json.dumps({"os": "linux", "architecture": "amd64",
                                  "rootfs": rootfs}).encode()
                descriptor = {"mediaType": registry.CONFIG_TYPE,
                              "size": len(raw),
                              "digest": "sha256:" + hashlib.sha256(raw).hexdigest()}
                layers = ([{"mediaType": registry.LAYER_TYPE, "size": 0,
                            "digest": "sha256:" + "0" * 64}]
                          if isinstance(rootfs, dict) and rootfs.get("diff_ids") else [])
                manifest = {"schemaVersion": 2, "config": descriptor, "layers": layers}
                client = Mock()
                client.reference = registry.parse_reference("example/p/base:1")
                client.manifest_raw = json.dumps(manifest).encode()
                manifest_digest = "sha256:" + hashlib.sha256(client.manifest_raw).hexdigest()
                client.blob.side_effect = lambda _desc, dest: Path(dest).write_bytes(raw)
                with patch.object(registry, "RegistryClient", return_value=client), \
                        patch.object(registry, "_selected_manifest", return_value=(manifest, manifest_digest)):
                    with self.assertRaises(BuildError):
                        registry.pull("example/p/base:1", root / "bad.tar")
                self.assertEqual(client.blob.call_count, 1)
                self.assertFalse((root / "bad.tar").exists())

    def test_registry_cas_to_build_cache_refresh_and_failure(self):
        """无需 daemon，贯通真实适配流程，并验证可变基础 tag、缓存命中与失败恢复。"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = make_base(root)
            reference = "example/base:1"
            base_context = root / "base-context"
            base_context.mkdir()
            (base_context / "version").write_bytes(b"updated-base")
            (base_context / "Dockerfile").write_text(
                "FROM example/base:1\nENV BASE=2\nCOPY version /base-version\n")
            updated = root / "updated.tar"
            build(base_context / "Dockerfile", base_context, original, None, reference, updated)

            def transport(archive_path):
                with tarfile.open(archive_path) as archive:
                    entry = json.load(archive.extractfile("manifest.json"))[0]
                    raw_config = archive.extractfile(entry["Config"]).read()
                    config_digest = "sha256:" + hashlib.sha256(raw_config).hexdigest()
                    blobs = {config_digest: raw_config}
                    layers = []
                    for name in entry["Layers"]:
                        raw = gzip.compress(archive.extractfile(name).read(), mtime=0)
                        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
                        blobs[digest] = raw
                        layers.append({"mediaType": registry.OCI_GZIP_LAYER,
                                       "digest": digest, "size": len(raw)})
                    manifest = {"schemaVersion": 2, "mediaType": registry.MANIFEST_TYPE,
                                "config": {"mediaType": registry.CONFIG_TYPE,
                                           "digest": config_digest, "size": len(raw_config)},
                                "layers": layers}
                    raw_manifest = json.dumps(manifest).encode()
                    return manifest, blobs, "sha256:" + hashlib.sha256(raw_manifest).hexdigest(), len(raw_manifest)

            current = transport(original)
            fail_layers = False
            calls = []

            class Client:
                def __init__(self, ref, *args, **kwargs):
                    self.reference = registry.parse_reference(ref)

                def manifest(self, _version):
                    self.manifest_raw = json.dumps(current[0]).encode()
                    return current[0], registry.MANIFEST_TYPE, current[2], current[3]

                def blob(self, descriptor, path):
                    calls.append(descriptor["digest"])
                    if fail_layers and descriptor["mediaType"] == registry.OCI_GZIP_LAYER:
                        raise BuildError("interrupted layer download")
                    Path(path).write_bytes(current[1][descriptor["digest"]])
                    registry._check_blob(Path(path), descriptor)

            store = root / "store"
            cache = root / "cache"
            context = root / "app"
            context.mkdir()
            (context / "Dockerfile").write_text("FROM example/base:1\nCOPY app.war /app/app.war\n")
            (context / "app.war").write_bytes(b"application")

            def build_and_read(number):
                output = root / ("app-{}.tar".format(number))
                with contextlib.redirect_stdout(io.StringIO()), \
                        (patch("cache.LayerCache.store", side_effect=AssertionError("cache missed"))
                         if number == 2 else contextlib.nullcontext()), \
                        patch("cas_store.gzip.open", side_effect=AssertionError("decoded twice")):
                    self.assertEqual(main(["build", "--offline", "--image-store", str(store),
                                           "--cache-dir", str(cache), "-t", "example/app:1",
                                           "-o", str(output), str(context)]), 0)
                image = ImageArchiveReader(output, root / ("read-{}".format(number))).read("example/app:1")
                index = RootFSIndex()
                for layer in image.layers:
                    index.apply_layer(layer)
                self.assertEqual(index.read_file("app/app.war"), b"application")
                self.assertIsNone(index.kind("etc/old.conf"))
                return image, index

            with patch.object(registry, "RegistryClient", Client):
                source, downloaded = pull_image(reference, store=store, materialize_tar=False)
                self.assertTrue(downloaded)
                self.assertEqual(source["type"], "cas")
                cas = CASStore(store)
                old = cas.resolve(reference, source="registry")
                self.assertTrue(all(cas._blob(item["diff_id"]).is_file() for item in old["layers"]))
                count = len(calls)
                first, _ = build_and_read(1)
                cached, _ = build_and_read(2)
                self.assertEqual(first.config["rootfs"], cached.config["rootfs"])
                self.assertEqual(len(list((cache / "entries").glob("*.json"))), 1)
                self.assertEqual(len(calls), count)
                current = transport(updated)
                pull_image(reference, store=store, refresh=True, materialize_tar=False)
                newer = cas.resolve(reference, source="registry")
                self.assertNotEqual(old["config"], newer["config"])
                self.assertEqual(old["layers"], newer["layers"][:2])
                image, index = build_and_read(3)
                self.assertIn("BASE=2", image.config["config"]["Env"])
                self.assertEqual(index.read_file("base-version"), b"updated-base")
                self.assertEqual(len(list((cache / "entries").glob("*.json"))), 2)
                fail_layers = True
                with self.assertRaisesRegex(BuildError, "interrupted"):
                    pull_image(reference, store=store, refresh=True, materialize_tar=False)
                self.assertEqual(cas.resolve(reference, source="registry"), newer)
                build_and_read(4)
                self.assertEqual(list(store.glob("*.tar")), [])
                self.assertEqual(list(store.rglob("*.part")), [])
                self.assertEqual(list(store.glob("pull-*")), [])


if __name__ == "__main__":
    unittest.main()
