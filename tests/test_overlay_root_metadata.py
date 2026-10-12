"""验证根目录权限贯穿索引、RUN 层、优化及扁平化；保留名不能误删文件。"""

import io
import os
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import conformance
import overlay
from errors import ArchiveError, BuildError, UnsupportedInstruction
from image_reader import ImageArchiveReader
from image_writer import ImageArchiveWriter
from optimizer import optimize_image
from rootfs import RootFSIndex
from rootfs_archive import merge_rootfs
from rootfs_materializer import RootFSMaterializer
from builder import build


def make_layer(path, root_mode=None, root_uid=0, root_gid=0, items=()):
    with tarfile.open(path, "w", format=tarfile.PAX_FORMAT) as archive:
        if root_mode is not None:
            info = tarfile.TarInfo(".")
            info.type, info.mode = tarfile.DIRTYPE, root_mode
            info.uid, info.gid = root_uid, root_gid
            archive.addfile(info)
        for name, kind, data in items:
            info = tarfile.TarInfo(name)
            info.type, info.size = kind, len(data) if kind == tarfile.REGTYPE else 0
            archive.addfile(info, io.BytesIO(data) if kind == tarfile.REGTYPE else None)
    return path


class OverlayRootMetadataTests(unittest.TestCase):
    def test_materializer_restores_root_metadata_after_children(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            layer = make_layer(root / "base.tar", 0o710, 101, 102,
                               (("child", tarfile.REGTYPE, b"x"),))
            materializer = RootFSMaterializer(root / "rootfs")
            with patch.object(materializer, "_metadata") as metadata:
                materializer.apply(layer)
            args = metadata.call_args_list[-1][0]
            self.assertEqual(args[0], materializer.root)
            self.assertEqual((args[1].mode, args[1].uid, args[1].gid), (0o710, 101, 102))
            self.assertEqual((materializer.root / "child").read_bytes(), b"x")

    def test_rootless_rejects_unmapped_root_before_writing_children(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            layer = make_layer(root / "base.tar", 0o755, 101, 102,
                               (("child", tarfile.REGTYPE, b"x"),))
            materializer = RootFSMaterializer(root / "rootfs", rootless=True)
            with self.assertRaises(BuildError):
                materializer.apply(layer)
            self.assertFalse((materializer.root / "child").exists())

    def test_duplicate_or_non_directory_roots_are_preflight_errors(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, kind in ((".", tarfile.DIRTYPE), ("./", tarfile.DIRTYPE), (".", tarfile.REGTYPE)):
                layer = make_layer(root / "bad.tar", 0o755,
                                   items=(("victim", tarfile.REGTYPE, b"x"), (name, kind, b"")))
                index = RootFSIndex()
                materializer = RootFSMaterializer(root / "rootfs")
                with self.subTest(name=name, kind=kind):
                    with self.assertRaises(ArchiveError):
                        index.apply_layer(layer)
                    with self.assertRaises(ArchiveError):
                        conformance._layer_tree([layer])
                    with self.assertRaises(ArchiveError):
                        materializer.apply(layer)
                    self.assertFalse((materializer.root / "victim").exists())

    def test_optimizer_does_not_remove_root_permission_layer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            layers = [make_layer(root / "first.tar", 0o755,
                                  items=(("keep", tarfile.REGTYPE, b"survive"),)),
                      make_layer(root / "root-change.tar", 0o700)]
            config = {"os": "linux", "architecture": "amd64", "config": {},
                      "rootfs": {"type": "layers", "diff_ids": []}}
            writer = ImageArchiveWriter()
            import hashlib
            config["rootfs"]["diff_ids"] = ["sha256:" + hashlib.sha256(p.read_bytes()).hexdigest()
                                               for p in layers]
            writer.write_new(root / "image.tar", config, layers, "root:1")
            result = optimize_image(root / "image.tar", root / "optimized.tar", "root:1")
            image = ImageArchiveReader(root / "optimized.tar", root / "read").read("root:1")
            merged = root / "merged.tar"
            merge_rootfs(image.layers, merged)
            with tarfile.open(merged) as archive:
                self.assertEqual(archive.getmember(".").mode, 0o700)
                self.assertEqual(archive.extractfile("keep").read(), b"survive")
            self.assertNotIn(2, result["removed_layer_numbers"])

    def test_root_opaque_clears_children_without_dropping_root_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            layers = [make_layer(root / "first.tar", 0o700,
                                  items=(("keep", tarfile.REGTYPE, b"survive"),)),
                      make_layer(root / "delete.tar", items=((".wh..wh..opq", tarfile.REGTYPE, b""),))]
            tree = conformance._layer_tree(layers)
            self.assertEqual(tree[""]["mode"], 0o700)
            self.assertNotIn("keep", tree)
            index = RootFSIndex()
            for layer in layers:
                index.apply_layer(layer)
            self.assertEqual(index.root_metadata.mode, 0o700)
            self.assertEqual(index.entries, {})

    def _manager(self, root):
        lower = root / "lower"
        lower.mkdir()
        workspace = root / "snapshot"
        workspace.mkdir()
        return overlay.OverlayManager(lower, workspace)

    def test_literal_whiteout_names_are_rejected(self):
        for name in (".wh.keep", ".wh..wh..opq", "nested/.wh.keep"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                manager = self._manager(root)
                path = manager.upper / name
                path.parent.mkdir(exist_ok=True)
                path.write_bytes(b"")
                with patch("overlay._overlay_value", return_value=None), \
                     patch.object(overlay.os, "listxattr", return_value=[], create=True), \
                     self.assertRaises(UnsupportedInstruction):
                    manager.to_layer(root / "run.tar")

    def test_changed_root_mode_is_serialized(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manager = self._manager(root)
            original_stat = Path.stat
            def root_stat(path, *args, **kwargs):
                data = original_stat(path, *args, **kwargs)
                if path == manager.upper:
                    values = list(data)
                    values[0] = (values[0] & ~0o7777) | 0o700
                    return os.stat_result(values)
                return data
            target = root / "run.tar"
            with patch.object(Path, "stat", root_stat), patch("overlay._overlay_value", return_value=None), \
                 patch.object(overlay.os, "listxattr", return_value=[], create=True):
                manager.to_layer(target)
            with tarfile.open(target) as archive:
                self.assertEqual(archive.getmember(".").mode, 0o700)

    def test_root_opaque_marker_is_serialized(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manager = self._manager(root)
            target = root / "run.tar"
            with patch("overlay._overlay_value", side_effect=lambda path, key, rootless=False:
                       b"y" if path == manager.upper and key == "opaque" else None), \
                 patch.object(overlay.os, "listxattr", return_value=[], create=True):
                manager.to_layer(target)
            with tarfile.open(target) as archive:
                self.assertIn(".wh..wh..opq", archive.getnames())

    def test_root_xattrs_are_copied_into_initial_upper(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch("overlay._file_xattrs", return_value={"user.note": b"base"}), \
                 patch.object(overlay.os, "setxattr", create=True) as setxattr:
                manager = self._manager(root)
            setxattr.assert_called_once_with(manager.upper, "user.note", b"base", follow_symlinks=False)

    def test_changed_root_xattr_and_epoch_are_serialized(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manager = self._manager(root)
            manager.source_date_epoch = 123
            target = root / "run.tar"
            with patch("overlay._file_xattrs", return_value={"user.note": b"new"}), \
                 patch("overlay._overlay_value", return_value=None):
                manager.to_layer(target)
            with tarfile.open(target) as archive:
                info = archive.getmember(".")
                self.assertEqual(info.mtime, 123)
                self.assertEqual(info.pax_headers["SCHILY.xattr.user.note"], "new")

    def test_unchanged_root_does_not_add_a_directory_header(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manager = self._manager(root)
            target = root / "run.tar"
            with patch("overlay._file_xattrs", return_value={}), \
                 patch("overlay._overlay_value", return_value=None):
                manager.to_layer(target)
            with tarfile.open(target) as archive:
                self.assertEqual(archive.getnames(), [])

    def test_root_permission_differences_are_visible_to_conformance(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = make_layer(root / "first.tar", 0o755)
            second = make_layer(root / "second.tar", 0o700)
            left = {"runtime": {}, "tree": conformance._layer_tree([first])}
            right = {"runtime": {}, "tree": conformance._layer_tree([second])}
            self.assertEqual([item["field"] for item in conformance.compare_snapshots(left, right)],
                             ["rootfs/"])

    def test_run_reserved_name_aborts_before_cache_or_image_publication(self):
        class FakeExecutor:
            def __init__(self, *args):
                pass
            def execute(self, manager, *args):
                (manager.upper / ".wh.keep").write_bytes(b"")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = root / "context"
            context.mkdir()
            dockerfile = context / "Dockerfile"
            dockerfile.write_text("FROM scratch\nRUN touch /.wh.keep\n", encoding="utf-8")
            with patch("executor.RunExecutor", FakeExecutor), \
                 patch("overlay._file_xattrs", return_value={}), \
                 patch("overlay._overlay_value", return_value=None), self.assertRaises(UnsupportedInstruction):
                build(dockerfile, context, None, None, "bad:1", root / "image.tar",
                      enable_run=True, cache_dir=root / "cache")
            self.assertFalse((root / "image.tar").exists())
            self.assertEqual(list((root / "cache" / "entries").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
