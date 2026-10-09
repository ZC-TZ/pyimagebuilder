"""验证镜像删除失败恢复，以及 CAS 清理对损坏引用的处理。"""

import json
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cas_store import CASStore
from errors import ArchiveError, BuildError
from image_cli import load_archive, prune_images, remove_image
from image_reader import sha256_file
from image_store import _archive_path
from tests.test_ref_transactions import make_image, PLATFORM, REFERENCE


class StoreRemovalTests(unittest.TestCase):
    def test_busy_reference_does_not_remove_archive(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store_root = root / "store"
            load_archive(make_image(root, "base"), store_root)
            store = CASStore(store_root)
            target = _archive_path(store_root, REFERENCE, PLATFORM, "local")
            previous = store.snapshot_ref(REFERENCE, PLATFORM, "local")
            archive_digest = sha256_file(target)
            ready, resume = threading.Event(), threading.Event()

            def hold_reference():
                with store.ref_lock(REFERENCE, PLATFORM, "local"):
                    ready.set()
                    if not resume.wait(5):
                        raise AssertionError("test synchronization timed out")

            with ThreadPoolExecutor(max_workers=1) as executor:
                operation = executor.submit(hold_reference)
                try:
                    self.assertTrue(ready.wait(5))
                    with self.assertRaisesRegex(BuildError, "busy"):
                        remove_image(REFERENCE, store_root, source="local")
                    self.assertTrue(target.is_file())
                    self.assertEqual(sha256_file(target), archive_digest)
                    self.assertEqual(store._ref(REFERENCE, PLATFORM, "local").read_bytes(), previous)
                finally:
                    resume.set()
                operation.result(timeout=5)

    def test_archive_delete_failure_restores_ref(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store_root = root / "store"
            load_archive(make_image(root, "base"), store_root)
            store = CASStore(store_root)
            target = _archive_path(store_root, REFERENCE, PLATFORM, "local")
            previous = store.snapshot_ref(REFERENCE, PLATFORM, "local")
            unlink = Path.unlink

            def deny_archive(path, *args, **kwargs):
                if path == target:
                    raise PermissionError("archive is in use")
                return unlink(path, *args, **kwargs)

            with patch.object(Path, "unlink", deny_archive):
                with self.assertRaisesRegex(PermissionError, "archive is in use"):
                    remove_image(REFERENCE, store_root, source="local")
            self.assertTrue(target.is_file())
            self.assertEqual(store.snapshot_ref(REFERENCE, PLATFORM, "local"), previous)
            store.resolve(REFERENCE, PLATFORM, "local")

    def test_removal_does_not_collect_unpublished_or_shared_blobs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store_root = root / "store"
            archive = make_image(root, "base")
            load_archive(archive, store_root)
            store = CASStore(store_root)
            store.import_docker(archive, REFERENCE, PLATFORM, "registry")
            staged = store._put_bytes(b"another import has not published its ref yet")
            result = remove_image(REFERENCE, store_root, source="local")
            self.assertEqual(result["source"], "local")
            self.assertFalse(store.has_ref(REFERENCE, PLATFORM, "local"))
            self.assertTrue(store._blob(staged).is_file())
            store.open_base(REFERENCE, PLATFORM, "registry")
            self.assertGreater(store.prune_orphans()["orphan_blobs"], 0)
            self.assertFalse(store._blob(staged).exists())
            store.open_base(REFERENCE, PLATFORM, "registry")

    def test_removal_is_independent_of_other_invalid_refs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store_root = root / "store"
            load_archive(make_image(root, "base"), store_root)
            store = CASStore(store_root)
            invalid = store.refs / "unrelated.json"
            invalid.write_text("[]", encoding="utf-8")
            result = remove_image(REFERENCE, store_root, source="local")
            self.assertEqual(result["removed"], REFERENCE)
            self.assertEqual(invalid.read_text(encoding="utf-8"), "[]")
            self.assertFalse(store.has_ref(REFERENCE, PLATFORM, "local"))

    def test_ref_delete_failure_preserves_archive_and_ref(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store_root = root / "store"
            load_archive(make_image(root, "base"), store_root)
            store = CASStore(store_root)
            target = _archive_path(store_root, REFERENCE, PLATFORM, "local")
            previous = store.snapshot_ref(REFERENCE, PLATFORM, "local")
            with patch.object(CASStore, "remove_ref", side_effect=PermissionError("ref is in use")):
                with self.assertRaisesRegex(PermissionError, "ref is in use"):
                    remove_image(REFERENCE, store_root, source="local")
            self.assertTrue(target.is_file())
            self.assertEqual(store.snapshot_ref(REFERENCE, PLATFORM, "local"), previous)
            # 删除失败也必须释放锁，下一次删除仍可正常执行。
            remove_image(REFERENCE, store_root, source="local")
            self.assertFalse(target.exists())

    def test_prune_rejects_non_object_refs_without_deleting_blobs(self):
        for value in ([], None, 1, "text"):
            for dry_run in (False, True):
                with self.subTest(value=value, dry_run=dry_run), \
                        tempfile.TemporaryDirectory() as temporary:
                    store = CASStore(Path(temporary) / "store")
                    digest = store._put_bytes(b"orphan to preserve on failed cleanup")
                    store.refs.mkdir(parents=True)
                    (store.refs / "invalid.json").write_text(json.dumps(value), encoding="utf-8")
                    with self.assertRaisesRegex(ArchiveError, "invalid ref"):
                        store.prune_orphans(dry_run=dry_run)
                    self.assertTrue(store._blob(digest).is_file())

    def test_prune_rejects_ref_that_omits_manifest_layer(self):
        for dry_run in (False, True):
            with self.subTest(dry_run=dry_run), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                store = CASStore(root / "store")
                value = store.import_docker(make_image(root, "base"), REFERENCE, PLATFORM)
                previous = {path.name: path.read_bytes() for path in store.blobs.iterdir()}
                value["layers"] = []
                store._ref(REFERENCE, PLATFORM, "local").write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaisesRegex(ArchiveError, "invalid ref"):
                    store.prune_orphans(dry_run=dry_run)
                self.assertEqual({path.name: path.read_bytes() for path in store.blobs.iterdir()}, previous)

    def test_image_prune_invalid_retained_ref_preserves_candidates(self):
        for dry_run in (False, True):
            with self.subTest(dry_run=dry_run), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                store_root = root / "store"
                load_archive(make_image(root, "base"), store_root)
                store = CASStore(store_root)
                target = _archive_path(store_root, REFERENCE, PLATFORM, "local")
                previous_ref = store.snapshot_ref(REFERENCE, PLATFORM, "local")
                previous_blobs = {path.name: path.read_bytes() for path in store.blobs.iterdir()}
                (store.refs / "unrelated.json").write_text("null", encoding="utf-8")
                with self.assertRaisesRegex(ArchiveError, "invalid ref"):
                    prune_images(store_root, older_than_days=0, dry_run=dry_run)
                self.assertTrue(target.is_file())
                self.assertEqual(store.snapshot_ref(REFERENCE, PLATFORM, "local"), previous_ref)
                self.assertEqual({path.name: path.read_bytes() for path in store.blobs.iterdir()}, previous_blobs)

    def test_gc_checks_metadata_without_rehashing_layer_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = CASStore(root / "store")
            value = store.import_docker(make_image(root, "base"), REFERENCE, PLATFORM)
            layer_path = store._blob(value["layers"][0]["digest"])
            inspected = set()

            def metadata_hash(path):
                self.assertNotEqual(Path(path), layer_path)
                inspected.add(Path(path))
                return sha256_file(path)

            with patch("cas_store.sha256_file", side_effect=metadata_hash):
                self.assertEqual(store.prune_orphans(dry_run=True)["orphan_blobs"], 0)
            self.assertEqual(inspected, {store._blob(value["manifest"]), store._blob(value["config"])})


if __name__ == "__main__":
    unittest.main()
