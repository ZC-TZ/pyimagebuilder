"""核对 whiteout 删除边界，防止异常标记或符号链接误删其他文件。"""

import io
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import conformance
from builder import build
from cas_store import CASStore
from errors import ArchiveError
from image_reader import ImageArchiveReader, sha256_file
from image_writer import ImageArchiveWriter
from rootfs import RootFSIndex
from rootfs_materializer import RootFSMaterializer


def layer(path, entries):
    """生成实际 tar 成员，包括无效删除标记，便于验证预检发生在修改之前。"""
    with tarfile.open(path, "w") as archive:
        for name, kind, value in entries:
            member = tarfile.TarInfo(name)
            member.type, member.mode = kind, 0o755
            if kind == tarfile.REGTYPE:
                member.size = len(value)
                archive.addfile(member, io.BytesIO(value))
            else:
                if kind == tarfile.SYMTYPE:
                    member.linkname = value
                archive.addfile(member)
    return path


class WhiteoutBoundariesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.lower = layer(self.root / "lower.tar", [
            ("target/keep", tarfile.REGTYPE, b"survive"),
            ("alias", tarfile.SYMTYPE, "/target")])

    def _materializer(self, name="fs"):
        with patch.object(RootFSMaterializer, "_metadata"):
            result = RootFSMaterializer(self.root / name)
        return result

    def _alias_patch(self, materializer):
        """用普通占位文件模拟镜像绝对符号链接，Windows 无需创建链接权限。"""
        target = materializer.root / "target"
        target.mkdir()
        (target / "keep").write_bytes(b"survive")
        alias = materializer.root / "alias"
        alias.write_bytes(b"symlink placeholder")
        actual = Path.is_symlink

        def is_symlink(path):
            return (path == alias and path.is_file()) or actual(path)
        return patch.object(Path, "is_symlink", is_symlink), patch(
            "rootfs_materializer.os.readlink", return_value="/target")

    def test_opaque_marker_under_replaced_symlink_does_not_delete_target(self):
        upper = layer(self.root / "upper.tar", [
            ("alias/.wh..wh..opq", tarfile.REGTYPE, b""),
            ("alias", tarfile.DIRTYPE, ""),
            ("alias/new", tarfile.REGTYPE, b"new")])
        index = RootFSIndex()
        index.apply_layer(self.lower)
        index.apply_layer(upper)
        materializer = self._materializer()
        first, second = self._alias_patch(materializer)
        with first, second, patch.object(materializer, "_metadata"):
            materializer.apply(upper)
        self.assertEqual((materializer.root / "target/keep").read_bytes(), index.read_file("target/keep"))
        self.assertEqual((materializer.root / "alias/new").read_bytes(), b"new")

    def test_regular_marker_under_symlink_does_not_delete_target(self):
        upper = layer(self.root / "upper.tar", [("alias/.wh.keep", tarfile.REGTYPE, b"")])
        materializer = self._materializer()
        first, second = self._alias_patch(materializer)
        with first, second, patch.object(materializer, "_metadata"):
            materializer.apply(upper)
        self.assertTrue((materializer.root / "target/keep").is_file())

    def test_whiteout_of_symlink_itself_unlinks_only_alias(self):
        upper = layer(self.root / "upper.tar", [(".wh.alias", tarfile.REGTYPE, b"")])
        materializer = self._materializer()
        first, second = self._alias_patch(materializer)
        with first, second, patch.object(materializer, "_metadata"):
            materializer.apply(upper)
        self.assertFalse((materializer.root / "alias").exists())
        self.assertTrue((materializer.root / "target/keep").is_file())

    def test_reserved_targets_are_rejected_by_all_rootfs_consumers(self):
        for number, name in enumerate((".wh.", ".wh..", ".wh...", "target/.wh..", "target/.wh...")):
            with self.subTest(name=name):
                invalid = layer(self.root / "invalid.tar", [(name, tarfile.REGTYPE, b"")])
                index = RootFSIndex()
                index.apply_layer(self.lower)
                with self.assertRaisesRegex(ArchiveError, "whiteout"):
                    index.apply_layer(invalid)
                with self.assertRaisesRegex(ArchiveError, "whiteout"):
                    conformance._layer_tree([self.lower, invalid])
                materializer = self._materializer("fs-invalid-" + str(number))
                (materializer.root / "target").mkdir()
                (materializer.root / "target/keep").write_bytes(b"survive")
                with patch.object(materializer, "_metadata"), self.assertRaisesRegex(ArchiveError, "whiteout"):
                    materializer.apply(invalid)
                self.assertTrue((materializer.root / "target/keep").exists())

    def test_bad_marker_is_detected_before_earlier_delete(self):
        for kind, value in ((tarfile.REGTYPE, b"not empty"), (tarfile.DIRTYPE, "")):
            with self.subTest(kind=kind):
                invalid = layer(self.root / "invalid.tar", [
                    ("target/.wh.keep", tarfile.REGTYPE, b""), (".wh.bad", kind, value)])
                index = RootFSIndex()
                index.apply_layer(self.lower)
                with self.assertRaises(ArchiveError):
                    index.apply_layer(invalid)
                self.assertEqual(index.read_file("target/keep"), b"survive")
                materializer = self._materializer("bad-" + kind.decode())
                (materializer.root / "target").mkdir()
                (materializer.root / "target/keep").write_bytes(b"survive")
                with patch.object(materializer, "_metadata"), self.assertRaises(ArchiveError):
                    materializer.apply(invalid)
                self.assertTrue((materializer.root / "target/keep").exists())

    def test_whiteout_reserved_parent_cannot_be_created_as_directory(self):
        for name in (".wh.hidden/file", "dir/.wh..wh..opq/file"):
            with self.subTest(name=name):
                invalid = layer(self.root / "invalid.tar", [(name, tarfile.REGTYPE, b"data")])
                with self.assertRaisesRegex(ArchiveError, "whiteout"):
                    RootFSIndex().apply_layer(invalid)
                with self.assertRaisesRegex(ArchiveError, "whiteout"):
                    conformance._layer_tree([invalid])
                materializer = self._materializer("parent-" + name.replace("/", "-"))
                with patch.object(materializer, "_metadata"), self.assertRaisesRegex(ArchiveError, "whiteout"):
                    materializer.apply(invalid)

    def test_opaque_marker_removes_lower_children_but_keeps_new_siblings(self):
        upper = layer(self.root / "upper.tar", [
            ("target/new", tarfile.REGTYPE, b"new"),
            ("target/.wh..wh..opq", tarfile.REGTYPE, b"")])
        materializer = self._materializer()
        (materializer.root / "target").mkdir()
        (materializer.root / "target/keep").write_bytes(b"old")
        with patch.object(materializer, "_metadata"):
            materializer.apply(upper)
        self.assertFalse((materializer.root / "target/keep").exists())
        self.assertEqual((materializer.root / "target/new").read_bytes(), b"new")

    def test_cas_base_run_stage_copy_and_cache_preserve_symlink_target(self):
        """真实层/CAS/构建缓存贯通，替代 Linux 执行器并模拟无权限创建的符号链接。"""
        upper = layer(self.root / "upper.tar", [
            ("alias/.wh..wh..opq", tarfile.REGTYPE, b""), ("alias", tarfile.DIRTYPE, "")])
        config = {"os": "linux", "architecture": "amd64", "config": {},
                  "rootfs": {"type": "layers", "diff_ids": ["sha256:" + sha256_file(path)
                                                              for path in (self.lower, upper)]},
                  "history": [{"created_by": "lower"}, {"created_by": "upper"}]}
        base = self.root / "base.tar"
        ImageArchiveWriter().write_new(base, config, [self.lower, upper], "example/base:1")
        store = self.root / "store"
        CASStore(store).import_docker(base, "example/base:1", "linux/amd64")
        source = {"type": "cas", "store": str(store), "reference": "example/base:1",
                  "platform": "linux/amd64", "source": "local"}
        context = self.root / "context"
        context.mkdir()
        dockerfile = context / "Dockerfile"
        dockerfile.write_text("FROM example/base:1 AS source\nRUN verify\nFROM scratch\n"
                              "COPY --from=source /result /app.war\n", encoding="utf-8")
        links, calls = {}, []
        real_is_symlink = Path.is_symlink

        def make_link(target, path):
            path = Path(path)
            path.write_bytes(b"symlink placeholder")
            links[path] = target

        def is_symlink(path):
            return (path in links and path.is_file()) or real_is_symlink(path)

        class FakeOverlay:
            def __init__(self, rootfs, workspace):
                self.rootfs = rootfs

            def to_layer(self, output):
                layer(output, [("result", tarfile.REGTYPE, self.content)])
                return "sha256:" + sha256_file(output)

        class FakeExecutor:
            def __init__(self, *args):
                pass

            def execute(self, overlay, *args):
                overlay.content = (overlay.rootfs / "target/keep").read_bytes()
                calls.append(overlay.content)

        with patch.object(RootFSMaterializer, "_metadata"), \
                patch("rootfs_materializer.os.symlink", make_link), \
                patch("rootfs_materializer.os.readlink", side_effect=lambda path: links[path]), \
                patch.object(Path, "is_symlink", is_symlink), \
                patch("overlay.OverlayManager", FakeOverlay), \
                patch("executor.RunExecutor", FakeExecutor):
            for number in range(2):
                stats = {}
                output = self.root / ("built-{}.tar".format(number))
                build(dockerfile, context, source, None, "example/app:1", output,
                      enable_run=True, cache_dir=self.root / "cache", cache_stats=stats)
                image = ImageArchiveReader(output, self.root / ("read-{}".format(number))).read("example/app:1")
                index = RootFSIndex()
                for path in image.layers:
                    index.apply_layer(path)
                self.assertEqual(index.read_file("app.war"), b"survive")
                self.assertEqual(stats["hits"], number * 2)
        self.assertEqual(calls, [b"survive"])


if __name__ == "__main__":
    unittest.main()
