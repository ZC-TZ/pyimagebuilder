"""验证异常 ref/tar 节点不会被当作镜像缺失并静默切换来源。"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cas_store import CASStore
from errors import ArchiveError, BuildError
from image_cli import save_archive
from image_store import _archive_path, locate_archive, pull_image
from tests.test_context_inputs import directory_link
from tests.test_ref_transactions import make_image, PLATFORM, REFERENCE


class StoreNodeLookupTests(unittest.TestCase):
    def prepare(self, root):
        directory = root / "store"
        store = CASStore(directory)
        archive = make_image(root, "base")
        store.import_docker(archive, REFERENCE, PLATFORM, "registry")
        return directory, store, archive

    def directory_ref(self, store):
        path = store._ref(REFERENCE, PLATFORM, "local")
        path.mkdir()
        marker = path / "keep.txt"
        marker.write_text("preserve", encoding="utf-8")
        return path, marker

    def test_ref_directory_is_rejected_by_lookup_and_snapshot(self):
        for operation in ("has_ref", "snapshot_ref", "resolve"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as temporary:
                _, store, _ = self.prepare(Path(temporary))
                invalid, marker = self.directory_ref(store)
                with self.assertRaisesRegex(ArchiveError, "CAS ref path must be a regular file"):
                    getattr(store, operation)(REFERENCE, PLATFORM, "local")
                self.assertTrue(invalid.is_dir())
                self.assertEqual(marker.read_text(encoding="utf-8"), "preserve")

    def test_ref_directory_cannot_be_replaced_as_missing_ref(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, store, archive = self.prepare(Path(temporary))
            invalid, marker = self.directory_ref(store)
            before = store.snapshot_ref(REFERENCE, PLATFORM, "registry")
            with self.assertRaisesRegex(ArchiveError, "CAS ref path must be a regular file"):
                store.import_docker(archive, REFERENCE, PLATFORM, "local", replace=True)
            self.assertEqual(store.snapshot_ref(REFERENCE, PLATFORM, "registry"), before)
            self.assertTrue(invalid.is_dir())
            self.assertEqual(marker.read_text(encoding="utf-8"), "preserve")

    def test_automatic_cas_resolution_does_not_hide_directory_ref(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, store, _ = self.prepare(Path(temporary))
            self.directory_ref(store)
            with self.assertRaisesRegex(ArchiveError, "CAS ref path must be a regular file"):
                store.resolve(REFERENCE, PLATFORM)
            # 显式选择其他正常来源仍可读取；本地异常不影响其数据。
            store.resolve(REFERENCE, PLATFORM, "registry")

    def test_default_pull_does_not_skip_directory_local_ref(self):
        for materialize in (False, True):
            with self.subTest(materialize=materialize), tempfile.TemporaryDirectory() as temporary:
                directory, store, _ = self.prepare(Path(temporary))
                invalid, marker = self.directory_ref(store)
                before = store.snapshot_ref(REFERENCE, PLATFORM, "registry")
                with patch("image_store.registry_pull") as remote:
                    with self.assertRaisesRegex(ArchiveError, "CAS ref path must be a regular file"):
                        pull_image(REFERENCE, store=directory, offline=True,
                                   materialize_tar=materialize)
                    remote.assert_not_called()
                self.assertEqual(store.snapshot_ref(REFERENCE, PLATFORM, "registry"), before)
                self.assertTrue(invalid.is_dir())
                self.assertEqual(marker.read_text(encoding="utf-8"), "preserve")

    def test_save_and_locate_cannot_hide_directory_ref(self):
        for operation in ("save", "locate"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                directory, store, _ = self.prepare(root)
                self.directory_ref(store)
                destination = root / "saved.tar"
                with self.assertRaisesRegex(ArchiveError, "CAS ref path must be a regular file"):
                    if operation == "save":
                        save_archive(REFERENCE, destination, directory)
                    else:
                        locate_archive(directory, REFERENCE, PLATFORM)
                self.assertFalse(destination.exists())
                self.assertFalse(_archive_path(directory, REFERENCE, PLATFORM, "registry").exists())

    def test_directory_tar_cannot_be_ignored_by_cas_build_lookup(self):
        for kind in ("local", "registry"):
            with self.subTest(source=kind), tempfile.TemporaryDirectory() as temporary:
                directory, store, archive = self.prepare(Path(temporary))
                if kind == "local":
                    store.import_docker(archive, REFERENCE, PLATFORM, "local")
                invalid = _archive_path(directory, REFERENCE, PLATFORM, kind)
                invalid.mkdir()
                marker = invalid / "keep.txt"
                marker.write_text("preserve", encoding="utf-8")
                before = store.snapshot_ref(REFERENCE, PLATFORM, kind)
                with patch("image_store.registry_pull") as remote:
                    with self.assertRaisesRegex(BuildError, "Stored archive path must be a regular file"):
                        pull_image(REFERENCE, store=directory, offline=True, materialize_tar=False)
                    remote.assert_not_called()
                self.assertEqual(store.snapshot_ref(REFERENCE, PLATFORM, kind), before)
                self.assertTrue(invalid.is_dir())
                self.assertEqual(marker.read_text(encoding="utf-8"), "preserve")

    def test_missing_ref_still_allows_other_source_and_tar_materialization(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory, store, _ = self.prepare(Path(temporary))
            self.assertFalse(store.has_ref(REFERENCE, PLATFORM, "local"))
            self.assertIsNone(store.snapshot_ref(REFERENCE, PLATFORM, "local"))
            value, downloaded = pull_image(REFERENCE, store=directory, offline=True, materialize_tar=False)
            self.assertEqual(value["source"], "registry")
            self.assertFalse(downloaded)
            kind, archive = locate_archive(directory, REFERENCE, PLATFORM)
            self.assertEqual(kind, "registry")
            self.assertTrue(archive.is_file())
            store.resolve(REFERENCE, PLATFORM, "registry")

    def test_node_inspection_permission_failure_is_not_missing(self):
        for kind in ("ref", "tar"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                directory, store, _ = self.prepare(Path(temporary))
                target = (store._ref(REFERENCE, PLATFORM, "local") if kind == "ref" else
                          _archive_path(directory, REFERENCE, PLATFORM, "registry"))
                original = Path.lstat

                def denied(path):
                    if path == target:
                        raise PermissionError("cannot inspect stored node")
                    return original(path)

                with patch.object(Path, "lstat", denied), patch("image_store.registry_pull") as remote:
                    with self.assertRaisesRegex(PermissionError, "cannot inspect stored node"):
                        pull_image(REFERENCE, store=directory, offline=True, materialize_tar=False)
                    remote.assert_not_called()

    def test_directory_links_are_checked_before_resolve(self):
        for kind in ("ref", "tar"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                directory, store, _ = self.prepare(root)
                outside = root / "outside"
                outside.mkdir()
                marker = outside / "keep.txt"
                marker.write_text("preserve", encoding="utf-8")
                target = (store._ref(REFERENCE, PLATFORM, "local") if kind == "ref" else
                          _archive_path(directory, REFERENCE, PLATFORM, "local"))
                directory_link(outside, target)
                try:
                    error = ArchiveError if kind == "ref" else BuildError
                    label = "CAS ref path" if kind == "ref" else "Stored archive path"
                    with self.assertRaisesRegex(error, label):
                        pull_image(REFERENCE, store=directory, offline=True, materialize_tar=False)
                    self.assertEqual(marker.read_text(encoding="utf-8"), "preserve")
                    store.resolve(REFERENCE, PLATFORM, "registry")
                finally:
                    # 只移除链接本身，避免旧 Python 的临时目录清理遍历 junction 目标。
                    if os.name == "nt":
                        target.rmdir()
                    else:
                        target.unlink()


if __name__ == "__main__":
    unittest.main()

