"""验证删除完成后异常的引用恢复，以及清理候选节点的删除前预检。"""

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
from image_cli import load_archive, prune_images, remove_image
from image_store import _archive_path
from tests.test_ref_transactions import make_image, PLATFORM, REFERENCE


class RemovalBoundaryTests(unittest.TestCase):
    def prepare(self, root, with_archive=True):
        archive = make_image(root, "base")
        directory = root / "store"
        store = CASStore(directory)
        if with_archive:
            load_archive(archive, directory)
        else:
            store.import_docker(archive, REFERENCE, PLATFORM, "local")
        store.import_docker(archive, REFERENCE, PLATFORM, "registry")
        return directory, store

    def test_direct_ref_delete_restores_after_unlink_failure(self):
        for error_type in (OSError, KeyboardInterrupt):
            with self.subTest(error=error_type.__name__), tempfile.TemporaryDirectory() as temporary:
                directory, store = self.prepare(Path(temporary), with_archive=False)
                previous = store.snapshot_ref(REFERENCE, PLATFORM, "local")
                unrelated = store.snapshot_ref(REFERENCE, PLATFORM, "registry")
                target = store._ref(REFERENCE, PLATFORM, "local")
                original = cas_store.unlink_missing

                def fail_after_unlink(path):
                    original(path)
                    if Path(path) == target:
                        raise error_type("ref unlinked before failure")

                with patch("cas_store.unlink_missing", side_effect=fail_after_unlink):
                    with self.assertRaises(error_type):
                        store.remove_ref(REFERENCE, PLATFORM, "local")
                self.assertEqual(store.snapshot_ref(REFERENCE, PLATFORM, "local"), previous)
                self.assertEqual(store.snapshot_ref(REFERENCE, PLATFORM, "registry"), unrelated)
                store.resolve(REFERENCE, PLATFORM, "local")
                store.remove_ref(REFERENCE, PLATFORM, "local")
                self.assertFalse(store.has_ref(REFERENCE, PLATFORM, "local"))
                store.resolve(REFERENCE, PLATFORM, "registry")

    def test_rmi_restores_if_ref_delete_succeeded_then_raised(self):
        for with_archive in (False, True):
            for error_type in (OSError, KeyboardInterrupt):
                with self.subTest(tar=with_archive, error=error_type.__name__), \
                        tempfile.TemporaryDirectory() as temporary:
                    directory, store = self.prepare(Path(temporary), with_archive)
                    previous = store.snapshot_ref(REFERENCE, PLATFORM, "local")
                    unrelated = store.snapshot_ref(REFERENCE, PLATFORM, "registry")
                    target = _archive_path(directory, REFERENCE, PLATFORM, "local")
                    tar_bytes = target.read_bytes() if with_archive else None
                    original = CASStore.remove_ref

                    def fail_after_remove(instance, *args, **kwargs):
                        original(instance, *args, **kwargs)
                        raise error_type("ref delete returned before failure")

                    with patch.object(CASStore, "remove_ref", fail_after_remove):
                        with self.assertRaises(error_type):
                            remove_image(REFERENCE, directory, source="local")
                    self.assertEqual(store.snapshot_ref(REFERENCE, PLATFORM, "local"), previous)
                    self.assertEqual(store.snapshot_ref(REFERENCE, PLATFORM, "registry"), unrelated)
                    if with_archive:
                        self.assertEqual(target.read_bytes(), tar_bytes)
                    store.resolve(REFERENCE, PLATFORM, "local")
                    remove_image(REFERENCE, directory, source="local")
                    self.assertFalse(store.has_ref(REFERENCE, PLATFORM, "local"))
                    self.assertFalse(target.exists())
                    store.resolve(REFERENCE, PLATFORM, "registry")

    def test_rmi_tar_already_unlinked_recovers_cas_and_can_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory, store = self.prepare(Path(temporary))
            previous = store.snapshot_ref(REFERENCE, PLATFORM, "local")
            target = _archive_path(directory, REFERENCE, PLATFORM, "local")
            original = Path.unlink

            def fail_after_unlink(path, *args, **kwargs):
                original(path, *args, **kwargs)
                if path == target:
                    raise KeyboardInterrupt("tar already unlinked")

            with patch.object(Path, "unlink", fail_after_unlink):
                with self.assertRaises(KeyboardInterrupt):
                    remove_image(REFERENCE, directory, source="local")
            self.assertFalse(target.exists())
            self.assertEqual(store.snapshot_ref(REFERENCE, PLATFORM, "local"), previous)
            # 已删除的兼容 tar 不可撤回，但原 CAS 镜像仍可使用，下一次删除也能成功。
            store.open_base(REFERENCE, PLATFORM, "local")
            remove_image(REFERENCE, directory, source="local")
            self.assertFalse(store.has_ref(REFERENCE, PLATFORM, "local"))
            store.open_base(REFERENCE, PLATFORM, "registry")

    def test_image_prune_preflights_tar_directories_regardless_of_age(self):
        for dry_run in (False, True):
            for old_directory in (False, True):
                with self.subTest(dry_run=dry_run, old_directory=old_directory), \
                        tempfile.TemporaryDirectory() as temporary:
                    directory, store = self.prepare(Path(temporary))
                    target = _archive_path(directory, REFERENCE, PLATFORM, "local")
                    previous = store.snapshot_ref(REFERENCE, PLATFORM, "local")
                    tar_bytes = target.read_bytes()
                    blobs = {path.name: path.read_bytes() for path in store.blobs.iterdir()}
                    os.utime(target, (0, 0))
                    # 排在有效候选之后，暴露边遍历边删除或只检查过期项造成的部分清理。
                    invalid = directory / "zz-invalid.tar"
                    invalid.mkdir()
                    marker = invalid / "keep.txt"
                    marker.write_text("keep", encoding="utf-8")
                    if old_directory:
                        os.utime(invalid, (0, 0))
                    with self.assertRaisesRegex(ArchiveError, "Unexpected image archive entry"):
                        prune_images(directory, older_than_days=1, dry_run=dry_run)
                    self.assertEqual(target.read_bytes(), tar_bytes)
                    self.assertEqual(store.snapshot_ref(REFERENCE, PLATFORM, "local"), previous)
                    self.assertEqual({path.name: path.read_bytes() for path in store.blobs.iterdir()}, blobs)
                    self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
                    marker.unlink()
                    invalid.rmdir()
                    result = prune_images(directory, older_than_days=1, dry_run=dry_run)
                    self.assertTrue(result["candidates"])
                    self.assertEqual(target.exists(), dry_run)

    def test_gc_excluded_directory_ref_is_still_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = CASStore(Path(temporary) / "store")
            digest = store._put_bytes(b"orphan to keep")
            store.refs.mkdir()
            invalid = store.refs / "invalid.json"
            invalid.mkdir()
            with self.assertRaisesRegex(ArchiveError, "invalid ref"):
                store.prune_orphans(dry_run=True, excluding=[invalid])
            self.assertEqual(store._blob(digest).read_bytes(), b"orphan to keep")
            self.assertTrue(invalid.is_dir())

    def test_gc_excluded_link_ref_is_still_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, store = self.prepare(Path(temporary), with_archive=False)
            target = store._ref(REFERENCE, PLATFORM, "local")
            original = Path.is_symlink

            def linked(path):
                return path == target or original(path)

            # Windows 无创建符号链接权限时仍覆盖检查顺序；内容和路径来自真实 CAS 引用。
            with patch.object(Path, "is_symlink", linked):
                with self.assertRaisesRegex(ArchiveError, "invalid ref"):
                    store.prune_orphans(dry_run=True, excluding=[target])


if __name__ == "__main__":
    unittest.main()

