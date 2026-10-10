"""区分硬链接共享的权限与固定内容，覆盖导出和跨阶段传输。"""

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
from dockerfile_parser import parse
from errors import BuildError
from image_reader import ImageArchiveReader, sha256_file
from image_writer import ImageArchiveWriter
from layer import LayerBuilder
from rootfs import RootFSIndex
from rootfs_archive import flatten_image, merge_rootfs, open_image
from rootfs_materializer import RootFSMaterializer


def layer(path, entries):
    """头元数据刻意不同，防止只检查文件字节而漏掉共享 inode 的变化。"""
    with tarfile.open(path, "w", format=tarfile.PAX_FORMAT) as archive:
        for name, target, uid, mode, attrs in entries:
            member = tarfile.TarInfo(name)
            member.uid, member.gid, member.mode, member.mtime = uid, uid + 1, mode, uid + 10
            member.pax_headers = {"SCHILY.xattr." + key: value for key, value in attrs.items()}
            if target is None:
                member.size = 7
                archive.addfile(member, io.BytesIO(b"payload"))
            else:
                member.type, member.linkname = tarfile.LNKTYPE, target
                archive.addfile(member)
    return path


class HardlinkMetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.lower = layer(self.root / "lower.tar", [("data", None, 10, 0o644,
                                                   {"user.keep": "lower"})])
        self.upper = layer(self.root / "upper.tar", [("alias", "data", 20, 0o4750,
                                                   {"user.add": "upper"})])

    def _merged(self, layers):
        output = self.root / "merged.tar"
        merge_rootfs(layers, output)
        with tarfile.open(output) as archive:
            return {member.name: member for member in archive}

    def _assert_owner(self, member, uid, mode):
        self.assertEqual((member.uid, member.gid, member.mode, member.mtime),
                         (uid, uid + 1, mode, uid + 10))

    def test_export_uses_latest_metadata_for_every_inode_alias(self):
        members = self._merged([self.lower, self.upper])
        for name in ("data", "alias"):
            self._assert_owner(members[name], 20, 0o4750)
            self.assertEqual(members[name].pax_headers["SCHILY.xattr.user.keep"], "lower")
            self.assertEqual(members[name].pax_headers["SCHILY.xattr.user.add"], "upper")

    def test_overwriting_original_path_detaches_its_metadata(self):
        replacement = layer(self.root / "replacement.tar", [("data", None, 30, 0o600, {})])
        members = self._merged([self.lower, self.upper, replacement])
        self._assert_owner(members["alias"], 20, 0o4750)
        self._assert_owner(members["data"], 30, 0o600)
        self.assertTrue(members["alias"].isfile())
        self.assertTrue(members["data"].isfile())

    def test_forward_link_chain_updates_shared_metadata_in_execution_order(self):
        chain = layer(self.root / "chain.tar", [("alias", "middle", 30, 0o4700, {}),
                                                ("middle", "data", 20, 0o640, {})])
        members = self._merged([self.lower, chain])
        for name in ("data", "middle", "alias"):
            self._assert_owner(members[name], 30, 0o4700)

    def test_repeated_layer_creates_new_inode_without_rejoining_old_alias(self):
        members = self._merged([self.lower, self.upper, self.lower])
        self._assert_owner(members["alias"], 20, 0o4750)
        self._assert_owner(members["data"], 10, 0o644)
        self.assertTrue(members["alias"].isfile())
        self.assertTrue(members["data"].isfile())

    def test_stage_copy_uses_shared_metadata_and_preserves_xattrs(self):
        source = RootFSIndex()
        for path in (self.lower, self.upper):
            source.apply_layer(path)
        transfer = parse("FROM scratch\nCOPY --from=source /data /alias /out/\n")[1].value
        output = self.root / "copy.tar"
        LayerBuilder(self.root, RootFSIndex()).transfer_from_rootfs(source, transfer, "/", output)
        with tarfile.open(output) as archive:
            for name in ("out/data", "out/alias"):
                member = archive.getmember(name)
                self.assertEqual((member.uid, member.gid, member.mode), (20, 21, 0o4750))
                self.assertEqual(member.pax_headers["SCHILY.xattr.user.keep"], "lower")
                self.assertEqual(member.pax_headers["SCHILY.xattr.user.add"], "upper")
                self.assertEqual(archive.extractfile(member).read(), b"payload")

    def test_independent_reader_reports_shared_metadata(self):
        tree = conformance._layer_tree([self.lower, self.upper])
        for name in ("data", "alias"):
            self.assertEqual((tree[name]["uid"], tree[name]["gid"], tree[name]["mode"]),
                             (20, 21, 0o4750))
            self.assertEqual(tree[name]["xattrs"], {"user.keep": "lower", "user.add": "upper"})

    def test_materializer_updates_the_actual_shared_inode(self):
        with patch.object(RootFSMaterializer, "_metadata") as metadata:
            materializer = RootFSMaterializer(self.root / "fs")
            metadata.reset_mock()
            materializer.apply(self.lower)
            materializer.apply(self.upper)
            self.assertEqual(metadata.call_args[0][1].uid, 20)
        first, second = materializer.root / "data", materializer.root / "alias"
        self.assertEqual(first.stat().st_ino, second.stat().st_ino)
        self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_deleting_original_path_keeps_surviving_alias_permissions(self):
        whiteout = self.root / "delete.tar"
        with tarfile.open(whiteout, "w") as archive:
            archive.addfile(tarfile.TarInfo(".wh.data"))
        members = self._merged([self.lower, self.upper, whiteout])
        self.assertNotIn("data", members)
        self._assert_owner(members["alias"], 20, 0o4750)
        self.assertTrue(members["alias"].isfile())

    def test_sibling_and_forward_links_follow_materializer_dependency_order(self):
        chain = layer(self.root / "chain.tar", [("child", "middle", 30, 0o4700, {}),
                                                ("middle", "data", 20, 0o640, {}),
                                                ("sibling", "data", 40, 0o600, {})])
        members = self._merged([self.lower, chain])
        tree = conformance._layer_tree([self.lower, chain])
        for name in ("data", "middle", "child", "sibling"):
            self._assert_owner(members[name], 30, 0o4700)
            self.assertEqual((tree[name]["uid"], tree[name]["mode"]), (30, 0o4700))

    def test_stage_copy_flags_override_inode_header_without_pax_overrides(self):
        source = RootFSIndex()
        source.apply_layer(self.lower)
        source.apply_layer(self.upper)
        transfer = parse("FROM scratch\nCOPY --from=source --chown=50:60 --chmod=0700 /alias /copy\n")[1].value
        output = self.root / "flags.tar"
        LayerBuilder(self.root, RootFSIndex(), source_date_epoch=99).transfer_from_rootfs(
            source, transfer, "/", output)
        with tarfile.open(output) as archive:
            member = archive.getmember("copy")
            self.assertEqual((member.uid, member.gid, member.mode, member.mtime), (50, 60, 0o700, 99))
            self.assertEqual(member.pax_headers["SCHILY.xattr.user.keep"], "lower")

    def test_add_preserves_xattrs_and_does_not_restore_source_pax_ids_or_time(self):
        source = self.root / "source.tar"
        with tarfile.open(source, "w", format=tarfile.PAX_FORMAT) as archive:
            member = tarfile.TarInfo("file")
            member.size = 3
            member.pax_headers = {"uid": "123", "gid": "456", "mtime": "12.5",
                                  "SCHILY.xattr.user.example": "kept"}
            archive.addfile(member, io.BytesIO(b"WAR"))
        transfer = parse("FROM scratch\nADD --chown=50:60 --chmod=0750 source.tar /app/\n")[1].value
        output = self.root / "add.tar"
        LayerBuilder(self.root, RootFSIndex(), source_date_epoch=99).transfer_layer(
            transfer, "/", output, add=True)
        with tarfile.open(output) as archive:
            member = archive.getmember("app/file")
            self.assertEqual((member.uid, member.gid, member.mode, member.mtime), (50, 60, 0o750, 99))
            self.assertEqual(member.pax_headers["SCHILY.xattr.user.example"], "kept")

    def test_link_chown_clears_old_capability_but_allows_explicit_replacement(self):
        for attrs, expected in (({}, None), ({"security.capability": "new"}, "new")):
            with self.subTest(attrs=attrs):
                lower = layer(self.root / "cap-lower.tar", [("data", None, 10, 0o644,
                                                           {"security.capability": "old"})])
                upper = layer(self.root / "cap-upper.tar", [("alias", "data", 20, 0o750, attrs)])
                for member in self._merged([lower, upper]).values():
                    self.assertEqual(member.pax_headers.get("SCHILY.xattr.security.capability"), expected)

    def _base_image(self):
        config = {"os": "linux", "architecture": "amd64", "config": {"User": "20:21"},
                  "rootfs": {"type": "layers", "diff_ids": ["sha256:" + sha256_file(path)
                                                              for path in (self.lower, self.upper)]},
                  "history": [{"created_by": "file"}, {"created_by": "link"}]}
        base = self.root / "base.tar"
        ImageArchiveWriter().write_new(base, config, [self.lower, self.upper], "example/base:1")
        return base

    def test_flatten_docker_and_oci_preserve_shared_permission_metadata(self):
        base = self._base_image()
        for format in ("docker", "oci"):
            with self.subTest(format=format):
                output = self.root / ("flat-" + format + ".tar")
                flatten_image(base, output, reference="example/flat:1", format=format)
                with open_image(output, self.root / (format + "-read"), tag="example/flat:1") as (image, _):
                    self.assertEqual(image.config["config"]["User"], "20:21")
                    with tarfile.open(image.layers[0]) as archive:
                        for name in ("data", "alias"):
                            self._assert_owner(archive.getmember(name), 20, 0o4750)

    def test_cas_stage_copy_and_cache_reuse_keep_same_image_identity(self):
        base = self._base_image()
        store = self.root / "store"
        CASStore(store).import_docker(base, "example/base:1", "linux/amd64")
        source = {"type": "cas", "store": str(store), "reference": "example/base:1",
                  "platform": "linux/amd64", "source": "local"}
        context = self.root / "context"
        context.mkdir()
        dockerfile = context / "Dockerfile"
        dockerfile.write_text("FROM example/base:1 AS source\nFROM scratch\n"
                              "COPY --from=source /data /alias /app/\n", encoding="utf-8")
        identities = []
        for number in range(2):
            output = self.root / ("build-{}.tar".format(number))
            stats = {}
            build(dockerfile, context, source, None, "example/app:1", output,
                  cache_dir=self.root / "cache",
                  cache_stats=stats)
            image = ImageArchiveReader(output, self.root / ("read-{}".format(number))).read("example/app:1")
            identities.append(image.config_digest)
            self.assertEqual(stats["hits"], number)
            with tarfile.open(image.layers[-1]) as archive:
                for name in ("app/data", "app/alias"):
                    member = archive.getmember(name)
                    self.assertEqual((member.uid, member.gid, member.mode), (20, 21, 0o4750))
                    self.assertEqual(member.pax_headers["SCHILY.xattr.user.keep"], "lower")
        self.assertEqual(identities[0], identities[1])

    def test_non_utf8_xattr_fails_with_a_build_error(self):
        materializer = object.__new__(RootFSMaterializer)
        materializer.rootless = False
        member = tarfile.TarInfo("file")
        member.pax_headers = {"SCHILY.xattr.security.capability": "\udcff"}
        with patch("rootfs_materializer.os.chown", create=True), \
                patch("rootfs_materializer.os.chmod"), patch("rootfs_materializer.os.utime"), \
                patch("rootfs_materializer.os.setxattr", create=True) as setter:
            with self.assertRaisesRegex(BuildError, "Cannot preserve xattr security.capability"):
                materializer._metadata(self.root / "file", member)
            setter.assert_not_called()

    def test_missing_xattr_api_fails_with_a_build_error(self):
        materializer = object.__new__(RootFSMaterializer)
        materializer.rootless = False
        member = tarfile.TarInfo("file")
        member.pax_headers = {"SCHILY.xattr.user.example": "value"}
        with patch("rootfs_materializer.os.chown", create=True), \
                patch("rootfs_materializer.os.chmod"), patch("rootfs_materializer.os.utime"), \
                patch("rootfs_materializer.os.setxattr", side_effect=AttributeError("not available"), create=True):
            with self.assertRaisesRegex(BuildError, "Cannot preserve xattr user.example"):
                materializer._metadata(self.root / "file", member)


if __name__ == "__main__":
    unittest.main()
