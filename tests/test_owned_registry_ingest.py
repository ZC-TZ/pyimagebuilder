"""验证下载临时层的所有权移交只减少复制，不削弱摘要、引用与文件隔离契约。"""

import errno
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cas_store
from cas_store import CASStore
from errors import ArchiveError
from test_cas_ingest_dedup import digest, registry_input
from test_artifactory_publication import artifact_files, download, layer_bytes, REFERENCE


class OwnedRegistryIngestTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = CASStore(self.root / "store")

    def ingest(self, manifest, config, paths, **kwargs):
        return self.store.import_registry(manifest, config, paths, "base:1", "linux/amd64",
                                          owned_layers=True, **kwargs)

    def test_same_disk_transfers_inode_and_preserves_raw_identity(self):
        for compressed in (False, True):
            with self.subTest(compressed=compressed):
                case = self.root / str(compressed)
                case.mkdir()
                manifest, config, blob, encoded = registry_input(case, b"original", compressed)
                before = blob.stat()
                ref = self.ingest(manifest, config, [blob], replace=True)
                target = self.store._blob(digest(encoded))
                self.assertFalse(blob.exists())
                self.assertEqual((target.stat().st_dev, target.stat().st_ino),
                                 (before.st_dev, before.st_ino))
                self.assertEqual(target.read_bytes(), encoded)
                self.assertEqual(self.store._blob(ref["manifest"]).read_bytes(), manifest)
                image = self.store.open_base("base:1", source="artifactory")
                self.assertEqual(image.config_raw, config.read_bytes())
                blob.write_bytes(b"caller recreated original path")
                self.assertEqual(target.read_bytes(), encoded)

    def test_default_import_keeps_input_independent(self):
        manifest, config, blob, encoded = registry_input(self.root, b"default", True)
        self.store.import_registry(manifest, config, [blob], "base:1", "linux/amd64")
        target = self.store._blob(digest(encoded))
        self.assertFalse(os.path.samefile(blob, target))
        blob.write_bytes(b"changed by caller")
        self.assertEqual(target.read_bytes(), encoded)

    def test_transfer_and_copy_export_identical_docker_archives(self):
        manifest, config, blob, _ = registry_input(self.root, b"export", True)
        copied = CASStore(self.root / "copied-store")
        copied.import_registry(manifest, config, [blob], "base:1", "linux/amd64")
        self.ingest(manifest, config, [blob])
        outputs = []
        for name, store in (("copy", copied), ("transfer", self.store)):
            output = self.root / (name + ".tar")
            store.export_docker("base:1", output, source="artifactory")
            outputs.append(output.read_bytes())
        self.assertEqual(outputs[0], outputs[1])

    def test_cross_device_falls_back_to_verified_copy(self):
        manifest, config, blob, encoded = registry_input(self.root, b"cross-device", True)
        replace = os.replace

        def cross_device(source, destination):
            if Path(source) == blob:
                raise OSError(errno.EXDEV, "cross-device")
            return replace(source, destination)

        with patch.object(cas_store.os, "replace", side_effect=cross_device):
            self.ingest(manifest, config, [blob])
        target = self.store._blob(digest(encoded))
        self.assertTrue(blob.exists())
        self.assertFalse(os.path.samefile(blob, target))
        self.assertEqual(target.read_bytes(), encoded)
        self.assertFalse(list(self.store.blobs.glob("*.part")))

    def test_cached_blob_cannot_hide_bad_owned_input_or_replace_ref(self):
        manifest, config, blob, encoded = registry_input(self.root, b"warm", True)
        self.store.import_registry(manifest, config, [blob], "base:1", "linux/amd64")
        previous = self.store.snapshot_ref("base:1", "linux/amd64", "artifactory")
        blob.write_bytes(b"!" + encoded[1:])
        with self.assertRaises(ArchiveError):
            self.ingest(manifest, config, [blob], replace=True)
        self.assertEqual(self.store._blob(digest(encoded)).read_bytes(), encoded)
        self.assertEqual(self.store.snapshot_ref("base:1", "linux/amd64", "artifactory"), previous)
        self.assertFalse(list(self.store.blobs.glob("*.part")))

    def repeat_layer(self, manifest, config, conflicting=False):
        value = json.loads(config.read_bytes())
        value["rootfs"]["diff_ids"] *= 2
        config.write_text(json.dumps(value), encoding="utf-8")
        value = json.loads(manifest)
        value["config"] = {"digest": digest(config.read_bytes()), "size": config.stat().st_size}
        value["layers"].append(dict(value["layers"][0]))
        if conflicting:
            value["layers"][1]["digest"] = "sha256:" + "0" * 64
        return json.dumps(value).encode()

    def test_repeated_descriptor_consumes_shared_source_once(self):
        manifest, config, blob, encoded = registry_input(self.root, b"repeated", True)
        manifest = self.repeat_layer(manifest, config)
        with patch.object(self.store, "_store_owned_blob", wraps=self.store._store_owned_blob) as take:
            ref = self.ingest(manifest, config, [blob, blob.parent / "." / blob.name])
        self.assertEqual(take.call_count, 1)
        self.assertEqual(len(ref["layers"]), 2)
        self.assertEqual(ref["layers"][0], ref["layers"][1])
        self.assertEqual(self.store._blob(digest(encoded)).read_bytes(), encoded)
        self.assertEqual(len(self.store.open_base("base:1", source="artifactory").layers), 2)

    def test_conflicting_shared_source_fails_before_consuming_any_input(self):
        manifest, config, blob, encoded = registry_input(self.root, b"conflict", True)
        manifest = self.repeat_layer(manifest, config, conflicting=True)
        with self.assertRaisesRegex(ArchiveError, "Conflicting Registry descriptors"):
            self.ingest(manifest, config, [blob, blob])
        self.assertEqual(blob.read_bytes(), encoded)
        self.assertFalse(self.store.has_ref("base:1", "linux/amd64", "artifactory"))

    def test_declared_size_is_checked_before_consuming(self):
        manifest, config, blob, encoded = registry_input(self.root, b"size", True)
        value = json.loads(manifest)
        value["layers"][0]["size"] += 1
        with self.assertRaisesRegex(ArchiveError, "size mismatch"):
            self.ingest(json.dumps(value).encode(), config, [blob])
        self.assertEqual(blob.read_bytes(), encoded)

    def test_cas_file_cannot_be_transferred_as_owned_input(self):
        raw = b"existing blob"
        target = self.store._put_stream(io.BytesIO(raw), digest(raw), len(raw))
        with self.assertRaisesRegex(ArchiveError, "private temporary file"):
            self.store._store_owned_blob(target, digest(raw), len(raw))
        with self.assertRaisesRegex(ArchiveError, "private temporary file"):
            self.store._store_owned_blob(target, digest(b"different identity"), len(raw))
        self.assertEqual(target.read_bytes(), raw)

    def test_artifactory_cas_path_uses_transfer_and_keeps_valid_image(self):
        files = artifact_files(layer_bytes(b"downloaded"))
        manifest = json.loads(files["manifest.json"])
        layer_digest = manifest["layers"][0]["digest"]
        incoming = self.store.import_registry
        original_inode = []

        def import_owned(raw, config, paths, *args, **kwargs):
            self.assertIs(kwargs.get("owned_layers"), True)
            original_inode.append(paths[0].stat().st_ino)
            return incoming(raw, config, paths, *args, **kwargs)

        with patch.object(self.store, "import_registry", side_effect=import_owned):
            download(files, self.root / "unused.tar", self.store)
        self.assertEqual(self.store._blob(layer_digest).stat().st_ino, original_inode[0])
        self.assertEqual(len(self.store.open_base(REFERENCE, source="artifactory").layers), 1)
        self.assertFalse(list(self.store.root.parent.glob("artifactory_download_*")))

    def test_owned_flag_requires_boolean(self):
        with self.assertRaisesRegex(ArchiveError, "owned_layers must be a boolean"):
            self.store.import_registry(b"{}", "unused", [], "base:1", "linux/amd64",
                                       owned_layers="false")


if __name__ == "__main__":
    unittest.main()
