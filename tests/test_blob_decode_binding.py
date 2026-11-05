"""复现摘要检查后换源，防止解压结果被绑定到另一份传输 blob。"""

import gzip
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cas_store
import registry
from cas_store import CASStore, DIFF_ID_VERIFICATION_VERSION
from errors import ArchiveError, BuildError
from image_reader import copy_verified_gzip_blob
from test_cas_diff_binding import digest, imported, layer
from test_ingestion_validation import TAG, client_for, transport


class BlobDecodeBindingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = CASStore(self.root / "store")
        self.first, self.second = layer(b"AAAA"), layer(b"BBBB")
        self.reference = "example/app:1"

    def cache_second(self):
        source = self.root / "cached.tar"
        source.write_bytes(self.second)
        self.store.store_decoded_layer(source, digest(self.second))

    def test_cas_open_rejects_swap_after_compressed_digest_check(self):
        value = imported(self.root, self.store, self.first, diff_id=digest(self.second))
        blob = self.store._blob(value["layers"][0]["digest"])
        previous = self.store.snapshot_ref(self.reference, "linux/amd64", "artifactory")
        actual = cas_store.sha256_file
        changed = []

        def checked_then_changed(path, *args, **kwargs):
            result = actual(path, *args, **kwargs)
            if Path(path) == blob and not changed:
                changed.append(True)
                blob.write_bytes(gzip.compress(self.second))
            return result

        with patch("cas_store.sha256_file", side_effect=checked_then_changed):
            with self.assertRaisesRegex(ArchiveError, "blob SHA-256"):
                self.store.open_base(self.reference)
        self.assertTrue(changed)
        self.assertEqual(self.store.snapshot_ref(self.reference, "linux/amd64", "artifactory"), previous)
        self.assertFalse(self.store._blob(digest(self.second)).exists())
        self.assertFalse(list(self.store.blobs.glob("*.part")))

    def test_cas_export_rejects_swap_after_resolve_and_removes_output(self):
        value = imported(self.root, self.store, self.first, diff_id=digest(self.second))
        blob = self.store._blob(value["layers"][0]["digest"])
        actual = self.store.resolve

        def checked_then_changed(*args, **kwargs):
            result = actual(*args, **kwargs)
            blob.write_bytes(gzip.compress(self.second))
            return result

        output = self.root / "export.tar"
        with patch.object(self.store, "resolve", side_effect=checked_then_changed):
            with self.assertRaisesRegex(ArchiveError, "blob SHA-256"):
                self.store.export_docker(self.reference, output)
        self.assertFalse(output.exists())

    def test_registry_rejects_downloaded_file_swapped_before_materialization(self):
        for compressed in (False, True):
            with self.subTest(compressed=compressed):
                config, _, raw_manifest, blobs = transport(self.first, compressed)
                config["rootfs"]["diff_ids"] = [digest(self.second)]
                config_raw = json.dumps(config).encode()
                manifest = json.loads(raw_manifest)
                manifest["config"].update(digest=digest(config_raw), size=len(config_raw))
                blobs[digest(config_raw)] = config_raw
                client = client_for(json.dumps(manifest).encode(), blobs)
                original = client.blob.side_effect
                descriptor = manifest["layers"][0]

                def checked_then_changed(entry, path):
                    original(entry, path)
                    if entry["digest"] == descriptor["digest"]:
                        Path(path).write_bytes(gzip.compress(self.second) if compressed else self.second)

                client.blob.side_effect = checked_then_changed
                output = self.root / ("registry-{}.tar".format(compressed))
                with patch.object(registry, "RegistryClient", return_value=client):
                    with self.assertRaises(BuildError):
                        registry.pull(TAG, output)
                self.assertFalse(output.exists())

    def test_legacy_true_flag_cannot_hide_mismatched_binding(self):
        self.cache_second()
        value = imported(self.root, self.store, self.first, diff_id=digest(self.second))
        path = self.store._ref(self.reference, "linux/amd64", "artifactory")
        value["layers"][0]["diff_id_verified"] = True
        for version in (None, 1):
            with self.subTest(version=version):
                record = dict(value, layers=[dict(value["layers"][0])])
                if version is not None:
                    record["layers"][0]["diff_id_verification_version"] = version
                raw = json.dumps(record).encode()
                path.write_bytes(raw)
                with self.assertRaises(ArchiveError):
                    self.store.open_base(self.reference)
                self.assertEqual(path.read_bytes(), raw)
                self.assertEqual(self.store._blob(digest(self.second)).read_bytes(), self.second)

    def test_legacy_good_binding_upgrades_once_without_new_blob(self):
        value = imported(self.root, self.store, self.first)
        path = self.store._ref(self.reference, "linux/amd64", "artifactory")
        value["layers"][0]["diff_id_verified"] = True
        path.write_text(json.dumps(value))
        with patch("cas_store.copy_verified_gzip_blob", wraps=copy_verified_gzip_blob) as decoder:
            self.store.open_base(self.reference)
        self.assertEqual(decoder.call_count, 1)
        updated = self.store.resolve(self.reference)
        self.assertEqual(updated["layers"][0]["diff_id_verification_version"], DIFF_ID_VERIFICATION_VERSION)
        with patch("cas_store.copy_verified_gzip_blob", side_effect=AssertionError("must reuse")):
            self.store.open_base(self.reference)

    def test_remote_descriptor_cannot_supply_binding_version(self):
        value = imported(self.root, self.store, self.first, diff_id=digest(self.second))
        manifest = json.loads(self.store._blob(value["manifest"]).read_bytes())
        manifest["layers"][0].update(diff_id_verified=True,
                                    diff_id_verification_version=DIFF_ID_VERIFICATION_VERSION)
        remote = self.store.import_registry(json.dumps(manifest).encode(), self.root / "config.json",
            [self.root / "layer.gz"], "example/remote:1", "linux/amd64")
        self.assertNotIn("diff_id_verified", remote["layers"][0])
        self.assertNotIn("diff_id_verification_version", remote["layers"][0])
        with self.assertRaises(ArchiveError):
            self.store.open_base("example/remote:1")

    def test_invalid_binding_versions_are_rejected(self):
        value = imported(self.root, self.store, self.first)
        path = self.store._ref(self.reference, "linux/amd64", "artifactory")
        for version in (True, False, None, "2", 2.0, 0, -1, []):
            with self.subTest(version=version):
                value["layers"][0]["diff_id_verification_version"] = version
                path.write_text(json.dumps(value))
                with self.assertRaises(ArchiveError):
                    self.store.open_base(self.reference)

    def test_gzip_members_and_trailing_padding_are_part_of_transport_digest(self):
        encoded = gzip.compress(b"first") + gzip.compress(b"second") + b"\0" * 123
        source = self.root / "blob.gz"
        source.write_bytes(encoded)
        target = io.BytesIO()
        count = copy_verified_gzip_blob(source, target, digest(encoded), len(encoded))
        self.assertEqual(count, 11)
        self.assertEqual(target.getvalue(), b"firstsecond")
        self.assertFalse(target.closed)
        with self.assertRaisesRegex(ArchiveError, "size mismatch"):
            copy_verified_gzip_blob(source, io.BytesIO(), digest(encoded), len(encoded) + 1)

    def test_in_place_swap_at_decoder_open_uses_actual_input_digest(self):
        source = self.root / "blob.gz"
        encoded = gzip.compress(self.first)
        source.write_bytes(encoded)
        original = gzip.open

        def replace_before_decode(*args, **kwargs):
            source.write_bytes(gzip.compress(self.second))
            return original(*args, **kwargs)

        with patch("image_reader.gzip.open", side_effect=replace_before_decode):
            with self.assertRaisesRegex(ArchiveError, "blob SHA-256"):
                copy_verified_gzip_blob(source, io.BytesIO(), digest(encoded))


if __name__ == "__main__":
    unittest.main()
