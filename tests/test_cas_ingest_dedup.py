"""验证 CAS 去重不能用旧 blob 代替本次输入字节的校验。"""

import gzip
import errno
import hashlib
import io
import json
import os
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import cas_store

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))

from cas_store import CASStore
from errors import ArchiveError
from image_reader import ImageArchiveReader
from oci_writer import OCIImageWriter, LAYER_TYPE
from test_cas_diff_binding import layer
from test_image_identity_contract import fixture, TAG
from test_phase1 import add_bytes


def digest(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def registry_input(root, marker, compressed):
    """明确指定原始传输字节、摘要和 DiffID，避免测试依赖下载器预校验。"""
    raw = layer(marker)
    if compressed:
        buffer = io.BytesIO()
        with gzip.GzipFile(fileobj=buffer, mode="wb", filename="", mtime=0) as stream:
            stream.write(raw)
        encoded = buffer.getvalue()
    else:
        encoded = raw
    config_raw = json.dumps({
        "os": "linux", "architecture": "amd64", "config": {},
        "rootfs": {"type": "layers", "diff_ids": [digest(raw)]}},
        indent=2).encode() + b"\n"
    config = root / (marker.decode() + "-config.json")
    blob = root / (marker.decode() + "-layer.blob")
    config.write_bytes(config_raw)
    blob.write_bytes(encoded)
    manifest = json.dumps({
        "schemaVersion": 2,
        "config": {"digest": digest(config_raw), "size": len(config_raw)},
        "layers": [{"digest": digest(encoded), "size": len(encoded),
                    "mediaType": LAYER_TYPE + "+gzip" if compressed else LAYER_TYPE}]
    }, indent=2).encode()
    return manifest, config, blob, encoded


class CASIngestDedupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_existing_blob_does_not_hide_mismatched_input(self):
        store = CASStore(self.root / "store")
        raw = b"valid input"
        target = store._put_stream(io.BytesIO(raw), digest(raw), len(raw))
        for incoming in (b"wrong input", raw[:-1], raw + b"!"):
            with self.subTest(incoming=incoming):
                with self.assertRaises(ArchiveError):
                    store._put_stream(io.BytesIO(incoming), digest(raw), len(raw))
                self.assertEqual(target.read_bytes(), raw)
        self.assertEqual(list(store.blobs.glob("*.part")), [])

    def test_owned_layer_reuses_file_and_survives_temporary_source_cleanup(self):
        store = CASStore(self.root / "store")
        source = self.root / "decoded.tar"
        raw = layer(b"owned decoded content")
        source.write_bytes(raw)
        original = source.stat()
        target = store.store_decoded_layer(source, digest(raw), owned=True)
        self.assertFalse(source.exists())
        self.assertEqual((target.stat().st_dev, target.stat().st_ino),
                         (original.st_dev, original.st_ino))
        self.assertEqual(target.read_bytes(), raw)
        copied = self.root / "ordinary.tar"
        copied.write_bytes(raw)
        other = CASStore(self.root / "other-store").store_decoded_layer(copied, digest(raw))
        copied.write_bytes(b"changed by caller")
        self.assertEqual(other.read_bytes(), raw)

    def test_owned_layer_falls_back_across_filesystems_and_rejects_corruption(self):
        store = CASStore(self.root / "store")
        source = self.root / "decoded.tar"
        raw = layer(b"valid decoded content")
        source.write_bytes(raw)
        replace = os.replace

        def cross_device(src, dst):
            if Path(src) == source:
                raise OSError(errno.EXDEV, "cross-device")
            return replace(src, dst)

        with patch.object(cas_store.os, "replace", side_effect=cross_device):
            target = store.store_decoded_layer(source, digest(raw), owned=True)
        self.assertFalse(os.path.samefile(source, target))
        self.assertEqual(target.read_bytes(), raw)
        source.write_bytes(layer(b"wrong decoded content"))
        with self.assertRaises(ArchiveError):
            store.store_decoded_layer(source, digest(raw), owned=True)
        self.assertEqual(target.read_bytes(), raw)
        source.write_bytes(raw)
        target.write_bytes(b"corrupt CAS")
        with self.assertRaises(ArchiveError):
            store.store_decoded_layer(source, digest(raw), owned=True)

    def test_owned_layer_detaches_original_path_before_verification_and_publication(self):
        store = CASStore(self.root / "store")
        source = self.root / "decoded.tar"
        raw, changed = layer(b"original"), layer(b"replaced")
        source.write_bytes(raw)
        original = source.stat()
        link = os.link

        def rewrite_original_then_publish(src, dst):
            source.write_bytes(changed)
            os.utime(source, ns=(original.st_atime_ns, original.st_mtime_ns))
            return link(src, dst)

        with patch.object(cas_store.os, "link", side_effect=rewrite_original_then_publish):
            target = store.store_decoded_layer(source, digest(raw), owned=True)
        self.assertEqual(target.read_bytes(), raw)
        self.assertEqual(source.read_bytes(), changed)
        self.assertEqual(list(store.blobs.glob("*.part")), [])

    def test_blob_publication_preserves_a_competing_file(self):
        store = CASStore(self.root / "store")
        raw = b"verified input"
        target = store._blob(digest(raw))
        publish = cas_store.publish_new_file

        def compete(src, dst):
            target.write_bytes(b"competing file")
            return publish(src, dst)

        with patch.object(cas_store, "publish_new_file", side_effect=compete):
            with self.assertRaises(ArchiveError):
                store._put_stream(io.BytesIO(raw), digest(raw), len(raw))
        self.assertEqual(target.read_bytes(), b"competing file")
        self.assertEqual(list(store.blobs.glob("*.part")), [])

    def test_existing_blob_does_not_hide_incoming_read_failure(self):
        store = CASStore(self.root / "store")
        raw = b"valid input"
        target = store._put_stream(io.BytesIO(raw), digest(raw), len(raw))

        class BrokenInput(io.BytesIO):
            def read(self, *args):
                raise OSError("incoming stream unavailable")

        with self.assertRaisesRegex(OSError, "incoming stream unavailable"):
            store._put_stream(BrokenInput(raw), digest(raw), len(raw))
        self.assertEqual(target.read_bytes(), raw)

    def test_registry_import_checks_each_input_even_with_cached_blob(self):
        for compressed in (False, True):
            for warm in (False, True):
                for existing in (False, True):
                    with self.subTest(compressed=compressed, warm=warm, existing=existing):
                        case = self.root / str((compressed, warm, existing))
                        case.mkdir()
                        store = CASStore(case / "store")
                        manifest, config, source, raw = registry_input(case, b"new", compressed)
                        if warm:
                            store.import_registry(manifest, config, [source],
                                                  "seed:1", "linux/amd64")
                        if existing:
                            old_manifest, old_config, old_source, _ = registry_input(
                                case, b"old", compressed)
                            store.import_registry(old_manifest, old_config, [old_source],
                                                  "target:1", "linux/amd64")
                        before = store.snapshot_ref("target:1", "linux/amd64", "artifactory")
                        source.write_bytes(b"!" + raw[1:])
                        with self.assertRaises(ArchiveError):
                            store.import_registry(manifest, config, [source],
                                                  "target:1", "linux/amd64", replace=True)
                        self.assertEqual(
                            store.snapshot_ref("target:1", "linux/amd64", "artifactory"), before)
                        if warm:
                            self.assertEqual(store._blob(digest(raw)).read_bytes(), raw)
                        source.write_bytes(raw)
                        value = store.import_registry(manifest, config, [source],
                                                      "target:1", "linux/amd64", replace=True)
                        self.assertEqual(value["layers"][0]["digest"], digest(raw))
                        self.assertEqual(store.open_base(
                            "target:1", source="artifactory").config_raw, config.read_bytes())

    def test_oci_import_checks_reopened_layers_even_when_cached(self):
        for warm, existing in ((False, False), (True, False), (True, True)):
            with self.subTest(warm=warm, existing=existing):
                case = self.root / str((warm, existing))
                case.mkdir()
                source, _, config_raw = fixture(case, "oci")
                original = source.read_bytes()
                store = CASStore(case / "store")
                if warm:
                    store.import_oci(source, TAG, "linux/amd64", source="registry")
                if existing:
                    store.import_oci(source, TAG, "linux/amd64")
                before = store.snapshot_ref(TAG, "linux/amd64", "local")
                verify = OCIImageWriter.verify

                def change_layer(instance, path, tag):
                    verified = verify(instance, path, tag)
                    with tarfile.open(path) as archive:
                        members = {member.name: archive.extractfile(member).read()
                                   for member in archive}
                    manifest = next(json.loads(raw) for name, raw in verified.items()
                                    if name.startswith("blobs/") and
                                    "layers" in json.loads(raw))
                    name = "blobs/sha256/" + manifest["layers"][0]["digest"].split(":")[1]
                    changed = members[name].replace(b"base", b"evil", 1)
                    self.assertNotEqual(changed, members[name])
                    self.assertEqual(len(changed), len(members[name]))
                    members[name] = changed
                    with tarfile.open(path, "w") as archive:
                        for name, raw in members.items():
                            add_bytes(archive, name, raw)
                    return verified

                with patch.object(OCIImageWriter, "verify", change_layer):
                    with self.assertRaises(ArchiveError):
                        store.import_oci(source, TAG, "linux/amd64", replace=True)
                self.assertEqual(store.snapshot_ref(TAG, "linux/amd64", "local"), before)
                source.write_bytes(original)
                value = store.import_oci(source, TAG, "linux/amd64", replace=True)
                self.assertEqual(value["config"], digest(config_raw))

    def test_docker_import_checks_verified_workspace_layers_even_when_cached(self):
        for warm, existing in ((False, False), (True, False), (True, True)):
            with self.subTest(warm=warm, existing=existing):
                case = self.root / str((warm, existing))
                case.mkdir()
                source, _, config_raw = fixture(case)
                store = CASStore(case / "store")
                if warm:
                    store.import_docker(source, TAG, "linux/amd64", source="registry")
                if existing:
                    store.import_docker(source, TAG, "linux/amd64")
                before = store.snapshot_ref(TAG, "linux/amd64", "local")
                read = ImageArchiveReader.read

                def change_layer(instance, *args, **kwargs):
                    image = read(instance, *args, **kwargs)
                    path = image.layers[0]
                    raw = path.read_bytes()
                    changed = raw.replace(b"base", b"evil", 1)
                    self.assertNotEqual(changed, raw)
                    self.assertEqual(len(changed), len(raw))
                    path.write_bytes(changed)
                    return image

                with patch.object(ImageArchiveReader, "read", change_layer):
                    with self.assertRaises(ArchiveError):
                        store.import_docker(source, TAG, "linux/amd64", replace=True)
                self.assertEqual(store.snapshot_ref(TAG, "linux/amd64", "local"), before)
                value = store.import_docker(source, TAG, "linux/amd64", replace=True)
                self.assertEqual(value["config"], digest(config_raw))

    def test_valid_dedup_consumes_input_without_rewriting_blob(self):
        store = CASStore(self.root / "store")
        for raw in (b"", b"valid input"):
            with self.subTest(raw=raw):
                target = store._put_stream(io.BytesIO(raw), digest(raw), len(raw))
                before = target.stat()
                stream = io.BytesIO(raw)
                self.assertEqual(store._put_stream(stream, digest(raw), len(raw)), target)
                after = target.stat()
                self.assertEqual(stream.tell(), len(raw))
                self.assertEqual(before.st_ino, after.st_ino)
                self.assertEqual(before.st_mtime_ns, after.st_mtime_ns)
                self.assertEqual(target.read_bytes(), raw)
        self.assertEqual(list(store.blobs.glob("*.part")), [])


if __name__ == "__main__":
    unittest.main()
