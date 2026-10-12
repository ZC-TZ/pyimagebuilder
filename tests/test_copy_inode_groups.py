"""检查 COPY 的硬链接分组，覆盖源目录、独立源参数、缓存及双格式交付。"""

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
from builder import build
from cas_store import CASStore
from dockerfile_parser import parse
from image_reader import sha256_file
from image_writer import ImageArchiveWriter
from layer import LayerBuilder
from rootfs import RootFSIndex
from rootfs_archive import open_image
from rootfs_materializer import RootFSMaterializer


def make_layer(path, entries):
    """直接声明 inode 关系，避免依赖宿主文件属主或符号链接权限。"""
    with tarfile.open(path, "w", format=tarfile.PAX_FORMAT) as archive:
        for name, target in entries:
            member = tarfile.TarInfo(name)
            member.uid, member.gid, member.mode = 10, 20, 0o750
            member.pax_headers = {"SCHILY.xattr.user.note": "shared metadata"}
            if target is None:
                member.size = 7
                archive.addfile(member, io.BytesIO(b"payload"))
            else:
                member.type, member.linkname = tarfile.LNKTYPE, target
                archive.addfile(member)
    return path


class CopyInodeGroupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.first = make_layer(self.root / "first.tar", [("pkg/data", None), ("pkg/plain", None)])
        self.second = make_layer(self.root / "second.tar", [("pkg/a-alias", "pkg/data"),
                                                            ("pkg/nested/z-alias", "pkg/data")])
        self.source = RootFSIndex()
        for layer in (self.first, self.second):
            self.source.apply_layer(layer)

    def copy_stage(self, instruction="COPY --from=source /pkg /out/", source=None):
        transfer = parse("FROM scratch\n" + instruction + "\n")[1].value
        output = self.root / "copy.tar"
        LayerBuilder(self.root, RootFSIndex(), source_date_epoch=99).transfer_from_rootfs(
            source or self.source, transfer, "/", output)
        result = RootFSIndex()
        result.apply_layer(output)
        return output, result

    def test_stage_directory_keeps_hardlinks_and_actual_write_aliasing(self):
        output, result = self.copy_stage()
        names = ("out/a-alias", "out/data", "out/nested/z-alias")
        for name in names:
            self.assertIs(result.entries[name].inode, result.entries[names[0]].inode)
            self.assertEqual(result.read_file(name), b"payload")
        # Windows 上跳过 POSIX 元数据写入，实际创建硬链接并验证写入关联。
        with patch.object(RootFSMaterializer, "_metadata"):
            materializer = RootFSMaterializer(self.root / "fs")
            materializer.apply(output)
        (materializer.root / names[0]).write_bytes(b"updated")
        for name in names[1:]:
            self.assertEqual((materializer.root / name).read_bytes(), b"updated")
        self.assertEqual((materializer.root / "out/plain").read_bytes(), b"payload")

    def test_flags_and_xattrs_are_consistent_on_shared_output_inode(self):
        output, result = self.copy_stage("COPY --from=source --chown=50:60 --chmod=0700 /pkg /out/")
        with tarfile.open(output) as archive:
            for name in ("out/a-alias", "out/data", "out/nested/z-alias"):
                member = archive.getmember(name)
                self.assertEqual((member.uid, member.gid, member.mode, member.mtime), (50, 60, 0o700, 99))
                self.assertEqual(member.pax_headers["SCHILY.xattr.user.note"], "shared metadata")
        self.assertIs(result.entries["out/data"].inode, result.entries["out/nested/z-alias"].inode)

    def test_excluded_first_alias_is_not_used_as_output_link_target(self):
        output, result = self.copy_stage("COPY --from=source --exclude=a-alias /pkg /out/")
        self.assertNotIn("out/a-alias", result.entries)
        self.assertIs(result.entries["out/data"].inode, result.entries["out/nested/z-alias"].inode)
        with tarfile.open(output) as archive:
            self.assertTrue(archive.getmember("out/data").isfile())
            self.assertEqual(archive.getmember("out/nested/z-alias").linkname, "out/data")

    def test_deleted_original_does_not_break_surviving_alias_group(self):
        deleted = self.root / "deleted.tar"
        with tarfile.open(deleted, "w") as archive:
            archive.addfile(tarfile.TarInfo("pkg/.wh.data"))
        self.source.apply_layer(deleted)
        _, result = self.copy_stage()
        self.assertNotIn("out/data", result.entries)
        self.assertIs(result.entries["out/a-alias"].inode, result.entries["out/nested/z-alias"].inode)
        self.assertEqual(result.read_file("out/nested/z-alias"), b"payload")

    def test_identical_bytes_do_not_merge_unrelated_files(self):
        _, result = self.copy_stage()
        self.assertIsNot(result.entries["out/data"].inode, result.entries["out/plain"].inode)
        self.assertEqual(result.read_file("out/data"), result.read_file("out/plain"))

    def test_repeated_content_member_does_not_merge_replaced_inode(self):
        # 再应用原始层后 data 是新 inode；旧 alias 仍引用相同 tar 成员的旧 inode。
        self.source.apply_layer(self.first)
        _, result = self.copy_stage()
        self.assertIsNot(result.entries["out/data"].inode, result.entries["out/a-alias"].inode)
        self.assertIs(result.entries["out/a-alias"].inode, result.entries["out/nested/z-alias"].inode)

    def test_separate_stage_source_parameters_remain_independent(self):
        _, result = self.copy_stage("COPY --from=source /pkg/a-alias /pkg/data /out/")
        self.assertIsNot(result.entries["out/a-alias"].inode, result.entries["out/data"].inode)

    def context_copy(self, sources, add=False):
        context = self.root / "context"
        files = context / "pkg"
        files.mkdir(parents=True)
        (files / "a").write_bytes(b"payload")
        os.link(files / "a", files / "b")
        transfer = parse("FROM scratch\n{} {} /out/\n".format("ADD" if add else "COPY", sources))[1].value
        output = self.root / "local-copy.tar"
        LayerBuilder(context, RootFSIndex()).transfer_layer(transfer, "/", output, add=add)
        result = RootFSIndex()
        result.apply_layer(output)
        return result

    def test_local_directory_keeps_its_internal_group(self):
        result = self.context_copy("pkg")
        self.assertIs(result.entries["out/a"].inode, result.entries["out/b"].inode)

    def test_separate_local_source_parameters_remain_independent(self):
        result = self.context_copy("pkg/a pkg/b")
        self.assertIsNot(result.entries["out/a"].inode, result.entries["out/b"].inode)

    def test_separately_expanded_local_sources_remain_independent(self):
        result = self.context_copy("pkg/*")
        self.assertIsNot(result.entries["out/a"].inode, result.entries["out/b"].inode)

    def test_local_add_parameters_do_not_join_output_inodes(self):
        result = self.context_copy("pkg/a pkg/b", add=True)
        self.assertIsNot(result.entries["out/a"].inode, result.entries["out/b"].inode)

    def test_cas_multistage_cache_and_dual_archive_keep_group(self):
        layers = [self.first, self.second]
        config = {"os": "linux", "architecture": "amd64", "config": {},
                  "rootfs": {"type": "layers", "diff_ids": ["sha256:" + sha256_file(p) for p in layers]}}
        base = self.root / "base.tar"
        ImageArchiveWriter().write_new(base, config, layers, "example/base:1")
        store = self.root / "store"
        CASStore(store).import_docker(base, "example/base:1", "linux/amd64")
        descriptor = {"type": "cas", "store": str(store), "reference": "example/base:1",
                      "platform": "linux/amd64", "source": "local"}
        context = self.root / "build-context"
        context.mkdir()
        dockerfile = context / "Dockerfile"
        dockerfile.write_text("FROM example/base:1 AS source\nFROM scratch\nCOPY --from=source /pkg /out/\n",
                              encoding="utf-8")
        identities = []
        for number in range(2):
            output, oci = self.root / ("build-{}.tar".format(number)), self.root / ("build-{}.oci.tar".format(number))
            stats = {}
            build(dockerfile, context, descriptor, None, "example/app:1", output,
                  cache_dir=self.root / "cache", cache_stats=stats, image_format="both", oci_output=oci)
            self.assertEqual((stats["hits"], stats["misses"]), (number, 1 - number))
            for format, path in (("docker", output), ("oci", oci)):
                with open_image(path, self.root / ("{}-read-{}".format(format, number)),
                                tag="example/app:1") as (image, _):
                    identities.append(image.config_digest)
                    index = RootFSIndex()
                    for layer in image.layers:
                        index.apply_layer(layer)
                    self.assertIs(index.entries["out/data"].inode, index.entries["out/nested/z-alias"].inode)
                # 独立解析器直接核对归档中的类型与内容，避免只依赖生产索引。
                tree = conformance.snapshot(path, "example/app:1")["tree"]
                self.assertEqual(tree["out/data"]["type"], "hardlink")
                self.assertEqual(tree["out/data"]["target"], "out/a-alias")
                self.assertEqual(tree["out/data"]["sha256"], tree["out/a-alias"]["sha256"])
        self.assertEqual(len(set(identities)), 1)


if __name__ == "__main__":
    unittest.main()
