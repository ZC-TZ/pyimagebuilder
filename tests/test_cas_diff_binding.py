"""验证解码缓存命中不能代替压缩 blob 与 DiffID 的绑定校验。"""

import gzip
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cas_store import CASStore
from errors import ArchiveError


def digest(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def layer(payload):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        member = tarfile.TarInfo("payload")
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))
    return stream.getvalue()


def imported(root, store, raw, diff_id=None, blob=None, reference="example/app:1"):
    config_raw = (json.dumps({"os": "linux", "architecture": "amd64", "config": {},
                              "rootfs": {"type": "layers", "diff_ids": [diff_id or digest(raw)]}},
                             indent=2) + "\n").encode()
    config = root / "config.json"
    config.write_bytes(config_raw)
    encoded = gzip.compress(raw) if blob is None else blob
    source = root / "layer.gz"
    source.write_bytes(encoded)
    manifest = {"schemaVersion": 2,
                "config": {"digest": digest(config_raw), "size": len(config_raw)},
                "layers": [{"digest": digest(encoded), "size": len(encoded),
                            "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip"}]}
    return store.import_registry(json.dumps(manifest).encode(), config, [source],
                                 reference, "linux/amd64")


class CASDiffBindingTests(unittest.TestCase):
    def test_other_blobs_decoded_cache_cannot_hide_wrong_diff_id(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = CASStore(root / "store")
            old, wrong = layer(b"old"), layer(b"bad")
            cached = root / "cached.tar"
            cached.write_bytes(old)
            store.store_decoded_layer(cached, digest(old))
            before = imported(root, store, wrong, diff_id=digest(old))
            with self.assertRaises(ArchiveError):
                store.open_base("example/app:1", source="artifactory")
            self.assertEqual(store.resolve("example/app:1", source="artifactory"), before)
            self.assertEqual(store._blob(digest(old)).read_bytes(), old)

    def test_cached_tar_cannot_hide_invalid_gzip(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = CASStore(root / "store")
            raw = layer(b"old")
            cached = root / "cached.tar"
            cached.write_bytes(raw)
            store.store_decoded_layer(cached, digest(raw))
            imported(root, store, raw, blob=b"invalid gzip")
            with self.assertRaises(ArchiveError):
                store.open_base("example/app:1", source="artifactory")

    def test_store_decoded_layer_checks_new_input_even_if_digest_exists(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = CASStore(root / "store")
            old, wrong = layer(b"old"), layer(b"bad")
            self.assertEqual(len(old), len(wrong))
            path = root / "layer.tar"
            path.write_bytes(old)
            store.store_decoded_layer(path, digest(old))
            path.write_bytes(wrong)
            with self.assertRaises(ArchiveError):
                store.store_decoded_layer(path, digest(old))
            self.assertEqual(store._blob(digest(old)).read_bytes(), old)

    def test_legacy_binding_is_verified_once_and_then_reused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = CASStore(root / "store")
            raw = layer(b"old")
            path = root / "cached.tar"
            path.write_bytes(raw)
            store.store_decoded_layer(path, digest(raw))
            before = imported(root, store, raw)
            real_open = gzip.open
            with patch("cas_store.gzip.open", wraps=real_open) as decoder:
                first = store.open_base("example/app:1", source="artifactory")
            self.assertEqual(decoder.call_count, 1)
            after = store.resolve("example/app:1", source="artifactory")
            self.assertEqual(after["manifest"], before["manifest"])
            self.assertEqual(after["config"], before["config"])
            self.assertIs(after["layers"][0]["diff_id_verified"], True)
            with patch("cas_store.gzip.open", side_effect=AssertionError("must reuse verified binding")):
                second = CASStore(root / "store").open_base("example/app:1", source="artifactory")
            self.assertEqual(first.config_raw, second.config_raw)
            self.assertEqual(second.layers[0].read_bytes(), raw)

    def test_non_boolean_binding_record_is_rejected(self):
        for invalid in (1, "true", [], None):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                store = CASStore(root / "store")
                value = imported(root, store, layer(b"old"))
                value["layers"][0]["diff_id_verified"] = invalid
                store._ref("example/app:1", "linux/amd64", "artifactory").write_text(json.dumps(value))
                with self.assertRaises(ArchiveError):
                    store.open_base("example/app:1", source="artifactory")

    def test_verification_does_not_recreate_deleted_ref(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = CASStore(root / "store")
            raw = layer(b"old")
            imported(root, store, raw)
            original = store.store_decoded_layer

            def accept_then_delete(path, diff_id):
                result = original(path, diff_id)
                store.remove_ref("example/app:1", "linux/amd64", "artifactory")
                return result

            with patch.object(store, "store_decoded_layer", side_effect=accept_then_delete):
                image = store.open_base("example/app:1", source="artifactory")
            self.assertEqual(image.layers[0].read_bytes(), raw)
            self.assertFalse(store.has_ref("example/app:1", "linux/amd64", "artifactory"))

    def test_verification_does_not_overwrite_replaced_ref(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = CASStore(root / "store")
            old = imported(root, store, layer(b"old"))
            original = store.store_decoded_layer
            winner = []

            def accept_then_replace(path, diff_id):
                result = original(path, diff_id)
                winner.append(imported(root, store, layer(b"new"), reference="example/winner:1"))
                value = dict(winner[-1], reference="example/app:1")
                with store.ref_lock("example/app:1", "linux/amd64", "artifactory"):
                    store._publish_ref(value, replace=True)
                winner[-1] = value
                return result

            with patch.object(store, "store_decoded_layer", side_effect=accept_then_replace):
                image = store.open_base("example/app:1", source="artifactory")
            self.assertEqual(image.manifest_digest, old["manifest"])
            self.assertEqual(store.resolve("example/app:1", source="artifactory"), winner[0])

    def test_failed_record_publication_keeps_original_ref_and_cleans_pending(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = CASStore(root / "store")
            imported(root, store, layer(b"old"))
            before = store.snapshot_ref("example/app:1", "linux/amd64", "artifactory")
            ref_path = store._ref("example/app:1", "linux/amd64", "artifactory")
            replace = os.replace

            def fail_record(source, destination):
                if Path(destination) == ref_path:
                    raise OSError("binding publication interrupted")
                return replace(source, destination)

            with patch("cas_store.os.replace", side_effect=fail_record):
                with self.assertRaisesRegex(OSError, "binding publication interrupted"):
                    store.open_base("example/app:1", source="artifactory")
            self.assertEqual(store.snapshot_ref("example/app:1", "linux/amd64", "artifactory"), before)
            self.assertFalse(list(store.refs.glob("*.part")))
            self.assertFalse(list(store.blobs.glob("*.part")))
            store.open_base("example/app:1", source="artifactory")

    def test_remote_descriptor_cannot_claim_verified_binding(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = CASStore(root / "store")
            old = layer(b"old")
            path = root / "cached.tar"
            path.write_bytes(old)
            store.store_decoded_layer(path, digest(old))
            before = imported(root, store, layer(b"bad"), diff_id=digest(old))
            manifest = json.loads(store._blob(before["manifest"]).read_bytes())
            manifest["layers"][0]["diff_id_verified"] = True
            store.import_registry(json.dumps(manifest).encode(), root / "config.json", [root / "layer.gz"],
                                  "example/remote:1", "linux/amd64")
            with self.assertRaises(ArchiveError):
                store.open_base("example/remote:1", source="artifactory")

    def test_later_layer_failure_leaves_all_binding_flags_unpublished(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = CASStore(root / "store")
            raw = layer(b"first")
            before = imported(root, store, raw)
            manifest = json.loads(store._blob(before["manifest"]).read_bytes())
            config = json.loads((root / "config.json").read_bytes())
            config["rootfs"]["diff_ids"].append(digest(layer(b"expected second")))
            config_raw = json.dumps(config).encode()
            (root / "config.json").write_bytes(config_raw)
            manifest["config"] = {"digest": digest(config_raw), "size": len(config_raw)}
            second = gzip.compress(layer(b"bad second"))
            second_path = root / "second.gz"
            second_path.write_bytes(second)
            manifest["layers"].append(dict(manifest["layers"][0], digest=digest(second), size=len(second)))
            store.import_registry(json.dumps(manifest).encode(), root / "config.json",
                                  [root / "layer.gz", second_path], "example/multi:1", "linux/amd64")
            snapshot = store.snapshot_ref("example/multi:1", "linux/amd64", "artifactory")
            with self.assertRaises(ArchiveError):
                store.open_base("example/multi:1", source="artifactory")
            self.assertEqual(store.snapshot_ref("example/multi:1", "linux/amd64", "artifactory"), snapshot)


if __name__ == "__main__":
    unittest.main()
