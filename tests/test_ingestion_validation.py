"""验证下载及缓存入库的校验门禁，不依赖最终镜像导出来发现坏输入。"""

import gzip
import hashlib
import io
import json
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import artifactory_download as artifacts
import image_reader
import registry
from cache import LayerCache
from cas_store import CASStore
from errors import ArchiveError, BuildError
from image_reader import ImageArchiveReader
from image_writer import ImageArchiveWriter


TAG = "example.test/base:1"


def digest(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def truncated_layer():
    info = tarfile.TarInfo("payload")
    info.size = 20000
    return info.tobuf() + b"x" * 512


def gzip_bytes(raw):
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as target:
        target.write(raw)
    return buffer.getvalue()


def transport(raw_layer=None, compressed=False, padding=""):
    config = {"os": "linux", "architecture": "amd64", "config": {},
              "rootfs": {"type": "layers", "diff_ids": []}}
    if padding:
        config["padding"] = padding
    config_raw = json.dumps(config).encode()
    layers = []
    blobs = {}
    if raw_layer is not None:
        config["rootfs"]["diff_ids"] = [digest(raw_layer)]
        config_raw = json.dumps(config).encode()
        raw = gzip_bytes(raw_layer) if compressed else raw_layer
        entry = {"mediaType": registry.OCI_GZIP_LAYER if compressed else registry.LAYER_TYPE,
                 "digest": digest(raw), "size": len(raw)}
        layers.append(entry)
        blobs[entry["digest"]] = raw
    entry = {"mediaType": registry.CONFIG_TYPE, "digest": digest(config_raw), "size": len(config_raw)}
    blobs[entry["digest"]] = config_raw
    manifest = {"schemaVersion": 2, "mediaType": registry.MANIFEST_TYPE,
                "config": entry, "layers": layers}
    return config, config_raw, json.dumps(manifest).encode(), blobs


def client_for(manifest_raw, blobs):
    client = Mock()
    client.reference = registry.parse_reference(TAG)
    client.manifest_raw = manifest_raw
    client.manifest.return_value = (json.loads(manifest_raw), registry.MANIFEST_TYPE,
                                    digest(manifest_raw), len(manifest_raw))

    def blob(entry, path):
        Path(path).write_bytes(blobs[entry["digest"]])
        registry._check_blob(Path(path), entry)

    client.blob.side_effect = blob
    return client


class IngestionValidationTests(unittest.TestCase):
    def test_registry_cas_only_rejects_truncated_layer_before_ref(self):
        for compressed in (False, True):
            with self.subTest(compressed=compressed), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                _, _, manifest_raw, blobs = transport(truncated_layer(), compressed)
                store = CASStore(root / "store")
                with patch.object(registry, "RegistryClient", return_value=client_for(manifest_raw, blobs)):
                    with self.assertRaises(BuildError):
                        registry.pull(TAG, root / "unused.tar", cas_store=store, cas_only=True)
                self.assertFalse(store.has_ref(TAG, "linux/amd64", "registry"))

    def test_archive_reader_rejects_truncated_layer_and_old_trusted_entry(self):
        for old_entry in (False, True):
            with self.subTest(old_entry=old_entry), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                layer = root / "layer.tar"
                layer.write_bytes(truncated_layer())
                config, _, _, _ = transport(layer.read_bytes())
                source = root / "source.tar"
                ImageArchiveWriter().write_new(source, config, [layer], TAG)
                reader = ImageArchiveReader(source, root / "workspace", root / "trusted")
                if old_entry:
                    with tarfile.open(source) as archive:
                        name = json.load(archive.extractfile("manifest.json"))[0]["Layers"][0]
                    diff_id = config["rootfs"]["diff_ids"][0]
                    _, (cached, metadata) = reader._cached_layer(name, diff_id)
                    cached.parent.mkdir(parents=True)
                    metadata.parent.mkdir(parents=True)
                    cached.write_bytes(layer.read_bytes())
                    metadata.write_text(json.dumps({"version": 1, "diff_id": diff_id,
                                                   "source": reader._identity(source),
                                                   "layer": reader._identity(cached)}))
                with self.assertRaises(ArchiveError):
                    reader.read(TAG)
                if not old_entry:
                    self.assertFalse((root / "trusted").exists())

    def test_artifactory_materialize_rejects_truncated_layer_and_cleans_target(self):
        for compressed in (False, True):
            with self.subTest(compressed=compressed), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                raw = truncated_layer()
                transferred = gzip_bytes(raw) if compressed else raw
                blob, target = root / "blob", root / "layer.tar"
                blob.write_bytes(transferred)
                with self.assertRaises(RuntimeError):
                    artifacts.materialize_layer(blob, target,
                        registry.OCI_GZIP_LAYER if compressed else registry.LAYER_TYPE,
                        digest(raw), 1, 1, Mock(is_tty=False), digest(transferred))
                self.assertFalse(target.exists())

    def test_cas_open_base_rejects_truncated_new_or_existing_decoded_blob(self):
        for cached in (False, True):
            with self.subTest(cached=cached), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                raw = truncated_layer()
                _, config_raw, manifest_raw, blobs = transport(raw, True)
                manifest = json.loads(manifest_raw)
                config_path, layer_path = root / "config.json", root / "layer.gz"
                config_path.write_bytes(config_raw)
                layer_path.write_bytes(blobs[manifest["layers"][0]["digest"]])
                store = CASStore(root / "store")
                store.import_registry(manifest_raw, config_path, [layer_path], TAG, "linux/amd64")
                if cached:
                    store._put_bytes(raw)
                with self.assertRaises(ArchiveError):
                    store.open_base(TAG, source="artifactory")
                if not cached:
                    self.assertFalse(store._blob(digest(raw)).exists())

    def test_instruction_cache_rejects_truncated_layer_before_publication(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            layer = root / "layer.tar"
            layer.write_bytes(truncated_layer())
            cache = LayerCache(root / "cache")
            with self.assertRaises(BuildError):
                cache.store("instruction", digest(layer.read_bytes()), layer)
            self.assertEqual(list(cache.entries.iterdir()), [])
            self.assertEqual(list(cache.layers.iterdir()), [])

    def test_old_instruction_cache_is_not_trusted_for_tar_structure(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache = LayerCache(Path(temporary) / "cache")
            raw = truncated_layer()
            diff_id = digest(raw)
            layer = cache.layers / (diff_id.split(":")[1] + ".tar")
            layer.write_bytes(raw)
            metadata = {"version": 7, "key": "instruction", "empty": False,
                        "diff_id": diff_id, "size": len(raw), "file_identity": cache._identity(layer)}
            (cache.entries / "instruction.json").write_text(json.dumps(metadata))
            self.assertIsNone(cache.lookup("instruction"))

    def test_registry_rejects_oversized_config_descriptor_before_download(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, _, manifest_raw, blobs = transport()
            manifest = json.loads(manifest_raw)
            manifest["config"]["size"] = registry.MAX_JSON + 1
            manifest_raw = json.dumps(manifest).encode()
            client = client_for(manifest_raw, blobs)
            client.blob.side_effect = AssertionError("oversized config was downloaded")
            with patch.object(registry, "RegistryClient", return_value=client):
                with self.assertRaises(BuildError):
                    registry.pull(TAG, Path(temporary) / "image.tar")
            client.blob.assert_not_called()

    def test_artifactory_and_cas_bound_metadata_reads(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, config_raw, manifest_raw, _ = transport(padding="x" * 1024)
            config_path = root / "config.json"
            config_path.write_bytes(config_raw)
            limit = len(manifest_raw)
            self.assertGreater(len(config_raw), limit)
            with patch.object(image_reader, "MAX_JSON", limit):
                with self.assertRaises(RuntimeError):
                    artifacts.load_json_object(config_path, "config")
                store = CASStore(root / "store")
                with self.assertRaises(ArchiveError):
                    store.import_registry(manifest_raw, config_path, [], TAG, "linux/amd64")
                self.assertFalse(store.has_ref(TAG, "linux/amd64", "artifactory"))
            store = CASStore(root / "valid-store")
            store.import_registry(manifest_raw, config_path, [], TAG, "linux/amd64")
            with patch.object(image_reader, "MAX_JSON", limit):
                with self.assertRaises(ArchiveError):
                    store.resolve(TAG, source="artifactory")

    def test_remote_json_rejects_non_json_numeric_constants(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            for token in ("NaN", "Infinity", "-Infinity"):
                with self.subTest(token=token):
                    raw = ('{"invalid":' + token + '}').encode()
                    path.write_bytes(raw)
                    with self.assertRaises(BuildError):
                        registry._json(raw, "config")
                    with self.assertRaises(RuntimeError):
                        artifacts.load_json_object(path, "config")


if __name__ == "__main__":
    unittest.main()
