"""验证 CAS 导入导出、blob 去重和损坏数据拒绝行为。"""

import tempfile
import contextlib
import io
import json
import hashlib
import os
import time
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from builder import build
from cas_store import CASStore
from errors import ArchiveError
from errors import BuildError
from image_cli import verify_archive
from image_cli import list_images
from image_cli import load_archive
from image_cli import save_archive
from image_cli import prune_images
from image_reader import sha256_file
from image_store import _archive_path, locate_archive
from main import main


class CASTests(unittest.TestCase):
    def test_docker_and_oci_import_share_blobs_and_export(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = root / "context"
            context.mkdir()
            (context / "Dockerfile").write_text("FROM scratch\nCOPY payload /payload\n",
                                                encoding="utf-8")
            (context / "payload").write_bytes(b"shared layer")
            docker = root / "docker.tar"
            build(context / "Dockerfile", context, None, None, "example/app:1", docker,
                  image_format="both")
            store = CASStore(root / "store")
            first = store.import_docker(docker, "example/app:1", "linux/amd64", "local")
            blobs_before = set(store.blobs.iterdir())
            second = store.import_oci(root / "docker.oci.tar", "example/app:1",
                                      "linux/amd64", "registry")
            self.assertEqual(first["config"], second["config"])
            self.assertEqual(first["layers"], second["layers"])
            self.assertEqual(blobs_before, set(store.blobs.iterdir()))
            with self.assertRaises(BuildError):
                locate_archive(root / "store", "example/app:1", "linux/amd64")
            self.assertEqual(list((root / "store").glob("*.tar")), [])
            exported = root / "exported.tar"
            store.export_docker("example/app:1", exported, source="local")
            self.assertEqual(verify_archive(exported)["tag"], "example/app:1")

    def test_corrupt_blob_is_not_exported(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = root / "context"
            context.mkdir()
            (context / "Dockerfile").write_text("FROM scratch\nCOPY payload /payload\n")
            (context / "payload").write_bytes(b"payload")
            archive = root / "base.tar"
            build(context / "Dockerfile", context, None, None, "example/base:1", archive)
            store = CASStore(root / "store")
            value = store.import_docker(archive, "example/base:1", "linux/amd64")
            store._blob(value["layers"][0]["digest"]).write_bytes(b"tampered")
            with self.assertRaises(ArchiveError):
                store.export_docker("example/base:1", root / "bad.tar")
            self.assertFalse((root / "bad.tar").exists())

    def test_cas_only_oci_import_is_visible_and_saveable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = root / "context"
            context.mkdir()
            (context / "Dockerfile").write_text("FROM scratch\nCOPY payload /payload\n")
            (context / "payload").write_bytes(b"OCI")
            oci = root / "image.oci.tar"
            build(context / "Dockerfile", context, None, None, "example/oci:1", oci,
                  image_format="oci")
            store = root / "store"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["cas", "import", str(oci), "example/oci:1",
                                       "--format", "oci", "--image-store", str(store)]), 0)
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(["images", "--image-store", str(store)]), 0)
            self.assertEqual(json.loads(output.getvalue())["images"][0]["storage"], "cas")
            exported = root / "saved.tar"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["save", "example/oci:1", "-o", str(exported),
                                       "--image-store", str(store)]), 0)
            self.assertEqual(verify_archive(exported)["tag"], "example/oci:1")

    def test_conflicting_ref_requires_replace_and_orphans_are_collectable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = root / "context"
            context.mkdir()
            (context / "Dockerfile").write_text("FROM scratch\nCOPY payload /payload\n")
            payload = context / "payload"
            payload.write_bytes(b"first")
            first = root / "first.tar"
            build(context / "Dockerfile", context, None, None, "example/app:1", first)
            payload.write_bytes(b"second")
            second = root / "second.tar"
            build(context / "Dockerfile", context, None, None, "example/app:1", second)
            store = CASStore(root / "store")
            store.import_docker(first, "example/app:1", "linux/amd64")
            with self.assertRaises(BuildError):
                store.import_docker(second, "example/app:1", "linux/amd64")
            store.import_docker(second, "example/app:1", "linux/amd64", replace=True)
            self.assertGreater(store.prune_orphans(dry_run=True)["orphan_blobs"], 0)
            self.assertGreater(store.prune_orphans()["orphan_blobs"], 0)

    def test_ref_descriptor_mismatch_and_bad_listing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = root / "context"
            context.mkdir()
            (context / "Dockerfile").write_text("FROM scratch\nCOPY payload /payload\n")
            (context / "payload").write_bytes(b"payload")
            archive = root / "base.tar"
            build(context / "Dockerfile", context, None, None, "example/base:1", archive)
            store = CASStore(root / "store")
            value = store.import_docker(archive, "example/base:1", "linux/amd64")
            ref_path = store._ref("example/base:1", "linux/amd64", "local")
            value["layers"][0]["mediaType"] = "application/vnd.oci.image.layer.v1.tar+gzip"
            ref_path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaises(ArchiveError):
                store.resolve("example/base:1", source="local")
            value["layers"] = ["invalid descriptor"]
            ref_path.write_text(json.dumps(value), encoding="utf-8")
            listing = list_images(root / "store")
            self.assertIn("error", listing["images"][0])

    def test_invalid_gzip_blob_has_controlled_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = CASStore(root / "store")
            config = {"os": "linux", "architecture": "amd64",
                      "rootfs": {"type": "layers", "diff_ids": ["sha256:" + "0" * 64]}}
            config_raw = json.dumps(config).encode("utf-8")
            config_path = root / "config.json"
            config_path.write_bytes(config_raw)
            bad_gzip = b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\xff" + b"\xff" * 20
            layer_path = root / "layer.gz"
            layer_path.write_bytes(bad_gzip)
            digest = lambda raw: "sha256:" + hashlib.sha256(raw).hexdigest()
            manifest = {"schemaVersion": 2,
                        "config": {"digest": digest(config_raw), "size": len(config_raw)},
                        "layers": [{"digest": digest(bad_gzip), "size": len(bad_gzip),
                                    "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip"}]}
            store.import_registry(json.dumps(manifest).encode("utf-8"), config_path,
                                  [layer_path], "example/base:1", "linux/amd64")
            with self.assertRaises(ArchiveError):
                store.open_base("example/base:1", source="artifactory")

    def test_cas_base_matches_explicit_tar_build_digest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base_context = root / "base-context"
            base_context.mkdir()
            (base_context / "Dockerfile").write_text("FROM scratch\nCOPY base /base\n")
            (base_context / "base").write_bytes(b"base bytes")
            base_tar = root / "base.tar"
            build(base_context / "Dockerfile", base_context, None, None,
                  "example/base:1", base_tar)
            store_root = root / "store"
            CASStore(store_root).import_docker(base_tar, "example/base:1", "linux/amd64")
            context = root / "context"
            context.mkdir()
            (context / "Dockerfile").write_text(
                "FROM example/base:1\nCOPY payload /payload\nENV AUDIT=1\n")
            (context / "payload").write_bytes(b"new bytes")
            mapped = root / "base-map.json"
            mapped.write_text(json.dumps({"example/base:1": {"type": "cas",
                "store": str(store_root), "reference": "example/base:1",
                "platform": "linux/amd64", "source": "local"}}))
            from_tar, from_cas = root / "from-tar.tar", root / "from-cas.tar"
            build(context / "Dockerfile", context, base_tar, None,
                  "example/derived:1", from_tar)
            build(context / "Dockerfile", context, None, mapped,
                  "example/derived:1", from_cas)
            self.assertEqual(sha256_file(from_tar), sha256_file(from_cas))

    def test_load_detects_stale_cas_even_when_tar_bytes_match(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = root / "context"
            context.mkdir()
            (context / "Dockerfile").write_text("FROM scratch\nCOPY payload /payload\n")
            payload = context / "payload"
            payload.write_bytes(b"old")
            first = root / "first.tar"
            reference = "example/base:1"
            build(context / "Dockerfile", context, None, None, reference, first)
            payload.write_bytes(b"new")
            second = root / "second.tar"
            build(context / "Dockerfile", context, None, None, reference, second)
            store_root = root / "store"
            store = CASStore(store_root)
            old_ref = store.import_docker(first, reference, "linux/amd64")
            managed_tar = _archive_path(store_root, reference, "linux/amd64", "local")
            managed_tar.parent.mkdir(parents=True, exist_ok=True)
            managed_tar.write_bytes(second.read_bytes())
            with self.assertRaises(BuildError):
                save_archive(reference, root / "stale-save.tar", store_root)
            with self.assertRaises(BuildError):
                load_archive(second, store_root)
            self.assertEqual(store.resolve(reference, source="local")["manifest"],
                             old_ref["manifest"])
            load_archive(second, store_root, replace=True)
            self.assertNotEqual(store.resolve(reference, source="local")["manifest"],
                                old_ref["manifest"])

    def test_cas_only_image_prune_estimates_and_reclaims_blobs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = root / "context"
            context.mkdir()
            (context / "Dockerfile").write_text("FROM scratch\nCOPY payload /payload\n")
            (context / "payload").write_bytes(b"payload")
            archive = root / "base.tar"
            build(context / "Dockerfile", context, None, None, "example/base:1", archive)
            store_root = root / "store"
            store = CASStore(store_root)
            store.import_docker(archive, "example/base:1", "linux/amd64")
            ref_path = store._ref("example/base:1", "linux/amd64", "local")
            old = time.time() - 3 * 86400
            os.utime(ref_path, (old, old))
            preview = prune_images(store_root, 2, dry_run=True)
            self.assertEqual(len(preview["candidates"]), 1)
            self.assertGreater(preview["reclaimable_bytes"], 0)
            actual = prune_images(store_root, 2)
            self.assertEqual(actual["reclaimable_bytes"], preview["reclaimable_bytes"])
            self.assertFalse(ref_path.exists())
            self.assertEqual(list(store.blobs.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
