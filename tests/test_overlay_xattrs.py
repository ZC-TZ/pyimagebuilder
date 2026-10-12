"""模拟内核 xattr，验证 RUN 差异层不会误删普通节点或复活已删除目录属性。"""

import errno
import io
import stat
import sys
import tarfile
import tempfile
import unittest
from contextlib import contextmanager, ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import overlay
from builder import build
from errors import BuildError, UnsupportedInstruction
from rootfs import RootFSIndex
from rootfs_materializer import RootFSMaterializer


class OverlayXattrTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    @contextmanager
    def filesystem(self, rootless=False, root_attrs=None):
        """文件树真实创建，xattr 系统调用在 Windows 上用独立状态表替代。"""
        lower = self.root / ("lower-rootless" if rootless else "lower-root")
        lower.mkdir()
        states = {str(lower): dict(root_attrs or {})}

        def getxattr(path, key, **kwargs):
            try:
                return states.get(str(path), {})[key]
            except KeyError:
                raise OSError(errno.ENODATA, "No attribute")

        def setxattr(path, key, value, **kwargs):
            states.setdefault(str(path), {})[key] = value

        with ExitStack() as stack:
            stack.enter_context(patch.object(overlay.os, "listxattr",
                side_effect=lambda p, **kw: list(states.get(str(p), {})), create=True))
            stack.enter_context(patch.object(overlay.os, "getxattr", side_effect=getxattr, create=True))
            stack.enter_context(patch.object(overlay.os, "setxattr", side_effect=setxattr, create=True))
            stack.enter_context(patch.object(overlay.os, "chown", create=True))
            stack.enter_context(patch.object(overlay.os, "geteuid", return_value=lower.stat().st_uid, create=True))
            stack.enter_context(patch.object(overlay.os, "getegid", return_value=lower.stat().st_gid, create=True))
            manager = overlay.OverlayManager(lower, self.root / ("upperless" if rootless else "upper"), rootless)
            yield manager, states

    def base_layer(self, name, attrs):
        path = self.root / "base.tar"
        with tarfile.open(path, "w", format=tarfile.PAX_FORMAT) as archive:
            member = tarfile.TarInfo(name)
            member.type, member.mode = tarfile.DIRTYPE, 0o755
            member.pax_headers = {"SCHILY.xattr." + key: value.decode("utf-8") for key, value in attrs.items()}
            archive.addfile(member)
        return path

    def test_internal_namespace_matches_mount_option(self):
        for rootless, prefix in ((False, "trusted.overlay."), (True, "user.overlay.")):
            with self.subTest(rootless=rootless), patch.object(overlay.os, "getxattr",
                    return_value=b"y", create=True) as get:
                self.assertEqual(overlay._overlay_value("unused", "opaque", rootless), b"y")
                get.assert_called_once_with("unused", prefix + "opaque", follow_symlinks=False)

    def test_inactive_namespace_whiteout_preserves_file_and_xattr(self):
        for rootless, prefix in ((False, "user.overlay."), (True, "trusted.overlay.")):
            with self.subTest(rootless=rootless), self.filesystem(rootless) as (manager, states):
                source = manager.upper / "payload"
                source.write_bytes(b"application content")
                states[str(source)] = {prefix + "whiteout": b"application attribute"}
                output = self.root / ("inactive-{}.tar".format(rootless))
                manager.to_layer(output)
                with tarfile.open(output) as archive:
                    self.assertNotIn(".wh.payload", archive.getnames())
                    member = archive.getmember("payload")
                    self.assertEqual(archive.extractfile(member).read(), b"application content")
                    self.assertEqual(member.pax_headers["SCHILY.xattr." + prefix + "whiteout"],
                                     "application attribute")

    def test_inactive_namespace_opaque_does_not_hide_lower_children(self):
        with self.filesystem() as (manager, states):
            (manager.rootfs / "app").mkdir()
            source = manager.upper / "app"
            source.mkdir()
            states[str(source)] = {"user.overlay.opaque": b"y"}
            output = self.root / "inactive-opaque.tar"
            manager.to_layer(output)
            with tarfile.open(output) as archive:
                self.assertNotIn("app/.wh..wh..opq", archive.getnames())
                self.assertEqual(archive.getmember("app").pax_headers["SCHILY.xattr.user.overlay.opaque"], "y")

    def test_active_namespace_empty_regular_whiteout_is_converted(self):
        for rootless, prefix in ((False, "trusted.overlay."), (True, "user.overlay.")):
            with self.subTest(rootless=rootless), self.filesystem(rootless) as (manager, states):
                source = manager.upper / "deleted"
                source.write_bytes(b"")
                states[str(source)] = {prefix + "whiteout": b""}
                output = self.root / ("active-{}.tar".format(rootless))
                manager.to_layer(output)
                with tarfile.open(output) as archive:
                    self.assertEqual(archive.getnames(), [".wh.deleted"])
                    self.assertEqual(archive.getmember(".wh.deleted").size, 0)

    def test_active_whiteout_with_payload_is_rejected(self):
        with self.filesystem() as (manager, states):
            source = manager.upper / "payload"
            source.write_bytes(b"must not vanish")
            states[str(source)] = {"trusted.overlay.whiteout": b""}
            with self.assertRaisesRegex(UnsupportedInstruction, "Invalid OverlayFS xattr whiteout"):
                manager.to_layer(self.root / "invalid.tar")

    def test_character_device_whiteout_remains_valid_with_xattr_marker(self):
        with self.filesystem() as (manager, states):
            source = manager.upper / "deleted"
            source.write_bytes(b"")
            states[str(source)] = {"trusted.overlay.whiteout": b""}
            real_lstat = Path.lstat

            def lstat(path):
                if path == source:
                    return SimpleNamespace(st_mode=stat.S_IFCHR, st_rdev=0, st_size=0)
                return real_lstat(path)

            output = self.root / "device-whiteout.tar"
            with patch.object(Path, "lstat", lstat), \
                    patch.object(overlay.os, "major", return_value=0, create=True), \
                    patch.object(overlay.os, "minor", return_value=0, create=True):
                manager.to_layer(output)
            with tarfile.open(output) as archive:
                self.assertEqual(archive.getnames(), [".wh.deleted"])

    def test_active_whiteout_on_directory_is_rejected(self):
        with self.filesystem() as (manager, states):
            source = manager.upper / "app"
            source.mkdir()
            states[str(source)] = {"trusted.overlay.whiteout": b""}
            with self.assertRaisesRegex(UnsupportedInstruction, "Invalid OverlayFS xattr whiteout"):
                manager.to_layer(self.root / "invalid.tar")

    def test_root_xattr_removal_is_rejected_before_layer_creation(self):
        with self.filesystem(root_attrs={"user.keep": b"old"}) as (manager, states):
            states[str(manager.upper)] = {}
            output = self.root / "root-delete.tar"
            with self.assertRaisesRegex(UnsupportedInstruction, "directory xattr removal.*user.keep"):
                manager.to_layer(output)
            self.assertFalse(output.exists())

    def test_root_opaque_does_not_make_xattr_removal_representable(self):
        with self.filesystem(root_attrs={"user.keep": b"old"}) as (manager, states):
            states[str(manager.upper)] = {"trusted.overlay.opaque": b"y"}
            with self.assertRaisesRegex(UnsupportedInstruction, "directory xattr removal"):
                manager.to_layer(self.root / "root-opaque.tar")

    def test_directory_xattr_removal_is_rejected_even_when_opaque(self):
        with self.filesystem() as (manager, states):
            for name, opaque in (("changed", False), ("recreated", True)):
                with self.subTest(opaque=opaque):
                    lower, upper = manager.rootfs / name, manager.upper / name
                    lower.mkdir()
                    upper.mkdir()
                    states[str(lower)] = {"user.keep": b"old"}
                    states[str(upper)] = {"trusted.overlay.opaque": b"y"} if opaque else {}
                    with self.assertRaisesRegex(UnsupportedInstruction, "directory xattr removal"):
                        manager.to_layer(self.root / (name + ".tar"))
                    # 下一用例不受上一目录影响；仅清理本用例的空 upper 目录。
                    upper.rmdir()

    def test_directory_xattr_add_and_update_remain_supported(self):
        with self.filesystem() as (manager, states):
            lower, upper = manager.rootfs / "app", manager.upper / "app"
            lower.mkdir()
            upper.mkdir()
            states[str(lower)] = {"user.keep": b"old"}
            states[str(upper)] = {"user.keep": b"new", "user.add": b"added"}
            base = self.base_layer("app", states[str(lower)])
            output = self.root / "update.tar"
            manager.to_layer(output)
            index = RootFSIndex()
            index.apply_layer(base)
            index.apply_layer(output)
            self.assertEqual(index.entries["app"].metadata.pax_headers,
                             {"SCHILY.xattr.user.keep": "new", "SCHILY.xattr.user.add": "added"})

    def test_regular_file_xattr_removal_remains_supported(self):
        with self.filesystem() as (manager, states):
            (manager.rootfs / "payload").write_bytes(b"old")
            (manager.upper / "payload").write_bytes(b"new")
            states[str(manager.rootfs / "payload")] = {"user.keep": b"old"}
            base = self.root / "file-base.tar"
            with tarfile.open(base, "w", format=tarfile.PAX_FORMAT) as archive:
                member = tarfile.TarInfo("payload")
                member.size = 3
                member.pax_headers = {"SCHILY.xattr.user.keep": "old"}
                archive.addfile(member, io.BytesIO(b"old"))
            output = self.root / "file-update.tar"
            manager.to_layer(output)
            index = RootFSIndex()
            index.apply_layer(base)
            index.apply_layer(output)
            self.assertNotIn("SCHILY.xattr.user.keep", index.entries["payload"].metadata.pax_headers)
            self.assertEqual(index.read_file("payload"), b"new")

    def test_failed_build_does_not_publish_or_cache_unrepresentable_directory_change(self):
        context = self.root / "context"
        context.mkdir()
        dockerfile = context / "Dockerfile"
        dockerfile.write_text("FROM scratch\nRUN remove-directory-xattr\n", encoding="utf-8")

        class FakeExecutor:
            def __init__(self, *args, **kwargs):
                pass

            def execute(self, manager, *args):
                # 输入与输出 xattr 由模拟执行器明确设置，测试走真实构建与缓存流程。
                manager._initial_root[3]["user.keep"] = b"old"

        output = self.root / "image.tar"
        with patch("executor.RunExecutor", FakeExecutor), \
                patch("overlay._file_xattrs", side_effect=lambda *args, **kwargs: {}), \
                patch("overlay._overlay_value", return_value=None):
            with self.assertRaisesRegex(UnsupportedInstruction, "directory xattr removal"):
                build(dockerfile, context, None, None, "example/bad:1", output,
                      enable_run=True, cache_dir=self.root / "cache")
        self.assertFalse(output.exists())
        self.assertEqual(list((self.root / "cache/entries").iterdir()), [])

    def test_reserved_image_xattrs_fail_before_whiteout_or_metadata_writes(self):
        for rootless, prefix in ((False, "trusted.overlay."), (True, "user.overlay.")):
            with self.subTest(rootless=rootless):
                layer = self.root / ("reserved-{}.tar".format(rootless))
                with tarfile.open(layer, "w", format=tarfile.PAX_FORMAT) as archive:
                    archive.addfile(tarfile.TarInfo(".wh.keep"))
                    member = tarfile.TarInfo("app")
                    member.type = tarfile.DIRTYPE
                    member.pax_headers = {"SCHILY.xattr." + prefix + "opaque": "y"}
                    archive.addfile(member)
                with patch.object(RootFSMaterializer, "_metadata") as metadata:
                    materializer = RootFSMaterializer(self.root / ("fs-{}".format(rootless)), rootless)
                    keep = materializer.root / "keep"
                    keep.write_bytes(b"must remain")
                    metadata.reset_mock()
                    with self.assertRaisesRegex(BuildError, "reserved OverlayFS xattr"):
                        materializer.apply(layer)
                    metadata.assert_not_called()
                self.assertEqual(keep.read_bytes(), b"must remain")

    def test_other_image_xattr_namespace_is_not_rejected_for_run(self):
        for rootless, prefix in ((False, "user.overlay."), (True, "trusted.overlay.")):
            with self.subTest(rootless=rootless):
                materializer = object.__new__(RootFSMaterializer)
                materializer.rootless = rootless
                member = tarfile.TarInfo("file")
                member.pax_headers = {"SCHILY.xattr." + prefix + "note": "application value"}
                materializer._validate_ownership(member)


if __name__ == "__main__":
    unittest.main()
