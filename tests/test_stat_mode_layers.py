"""复现真实基础层携带完整 st_mode，验证权限兼容与原始镜像身份。"""

import json
import stat
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from builder import build
from cas_store import CASStore
from errors import BuildError
from fast import quick_build
from image_reader import ImageArchiveReader, sha256_file
from image_writer import ImageArchiveWriter
from permissions import validate_metadata
from rootfs import RootFSIndex
from rootfs_archive import merge_rootfs, prepare_rootfs
from rootfs_materializer import RootFSMaterializer


def full_mode_layer(path, links=False):
    """手写 mode 与 checksum；tarfile 写入器会先去掉类型位，无法生成复现输入。"""
    raw = bytearray()
    entries = [(".", tarfile.DIRTYPE, stat.S_IFDIR | 0o755, b""),
               ("soft/tomcat/webapps", tarfile.DIRTYPE, stat.S_IFDIR | 0o2755, b""),
               ("etc/passwd", tarfile.REGTYPE, stat.S_IFREG | 0o644,
                b"tomcat:x:1000:1000::/soft/tomcat:/bin/sh\n"),
               ("etc/group", tarfile.REGTYPE, stat.S_IFREG | 0o644, b"tomcat:x:1000:\n"),
               ("soft/tomcat/start.sh", tarfile.REGTYPE, stat.S_IFREG | 0o4755, b"#!/bin/sh\n")]
    if links:
        entries.extend([("soft/tomcat/current.sh", tarfile.SYMTYPE, stat.S_IFLNK | 0o777, b""),
                        ("soft/tomcat/start-copy.sh", tarfile.LNKTYPE, stat.S_IFREG | 0o4755, b"")])
    for name, kind, mode, payload in entries:
        member = tarfile.TarInfo(name)
        member.type, member.size = kind, len(payload)
        if kind == tarfile.SYMTYPE:
            member.linkname = "start.sh"
        elif kind == tarfile.LNKTYPE:
            member.linkname = "soft/tomcat/start.sh"
        header = bytearray(member.tobuf(format=tarfile.USTAR_FORMAT))
        header[100:108] = ("%07o\0" % mode).encode("ascii")
        header[148:156] = b"        "
        header[148:156] = ("%06o\0 " % sum(header)).encode("ascii")
        raw.extend(header)
        raw.extend(payload)
        raw.extend(b"\0" * ((-len(payload)) % 512))
    raw.extend(b"\0" * 1024)
    path.write_bytes(raw)
    return path


class StatModeLayerTests(unittest.TestCase):
    def test_index_accepts_full_stat_mode_without_changing_layer_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            layer = full_mode_layer(Path(temporary) / "layer.tar")
            digest = sha256_file(layer)
            index = RootFSIndex()
            index.apply_layer(layer)
            self.assertEqual(index.root_metadata.mode, 0o755)
            self.assertEqual(index.entries["soft/tomcat/webapps"].metadata.mode, 0o2755)
            self.assertEqual(index.entries["soft/tomcat/start.sh"].metadata.mode, 0o4755)
            self.assertEqual(sha256_file(layer), digest)
            with tarfile.open(layer) as archive:
                self.assertEqual(archive.getmember(".").mode, 16877)

    def test_full_modes_keep_type_consistency_and_metadata_validation_is_read_only(self):
        kinds = [(tarfile.DIRTYPE, stat.S_IFDIR), (tarfile.REGTYPE, stat.S_IFREG),
                 (tarfile.SYMTYPE, stat.S_IFLNK), (tarfile.LNKTYPE, stat.S_IFREG),
                 (tarfile.FIFOTYPE, stat.S_IFIFO), (tarfile.CHRTYPE, stat.S_IFCHR),
                 (tarfile.BLKTYPE, stat.S_IFBLK)]
        for kind, bits in kinds:
            with self.subTest(kind=kind):
                member = tarfile.TarInfo("entry")
                member.type, member.mode = kind, bits | 0o7755
                validate_metadata(member)
                self.assertEqual(member.mode, bits | 0o7755)
        for invalid in (-1, True, "755", 0o10000, 0o200755, stat.S_IFREG | 0o755):
            with self.subTest(invalid=invalid):
                member = tarfile.TarInfo("directory")
                member.type, member.mode = tarfile.DIRTYPE, invalid
                with self.assertRaises(BuildError):
                    validate_metadata(member)

    def test_rootfs_export_import_and_add_keep_effective_permissions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            layer = full_mode_layer(root / "source.tar")
            for name, operation in (("merged.tar", lambda out: merge_rootfs([layer], out)),
                                    ("prepared.tar", lambda out: prepare_rootfs(layer, out))):
                output = root / name
                operation(output)
                with tarfile.open(output) as archive:
                    self.assertEqual(archive.getmember("soft/tomcat/start.sh").mode, 0o4755)
            dockerfile = root / "Dockerfile"
            dockerfile.write_text("FROM scratch\nADD source.tar /\n", encoding="utf-8")
            output = root / "add.tar"
            build(dockerfile, root, None, None, "example/add:1", output)
            image = ImageArchiveReader(output, root / "add-read").read("example/add:1")
            index = RootFSIndex()
            for path in image.layers:
                index.apply_layer(path)
            self.assertEqual(index.entries["soft/tomcat/webapps"].metadata.mode, 0o2755)

    def test_cas_fast_war_and_from_only_preserve_base_layer_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            layer = full_mode_layer(root / "layer.tar")
            base = root / "base.tar"
            reference = "nmkf.example.test/abc-image/tomcat:1.0.0"
            config = {"os": "linux", "architecture": "amd64", "config": {},
                      "rootfs": {"type": "layers", "diff_ids": ["sha256:" + sha256_file(layer)]}}
            ImageArchiveWriter().write_new(base, config, [layer], reference)
            cas = CASStore(root / "store")
            cas.import_docker(base, reference, "linux/amd64", "artifactory")
            source = {"type": "cas", "store": str(root / "store"), "reference": reference,
                      "platform": "linux/amd64", "source": "artifactory"}
            context = root / "context"
            context.mkdir()
            dockerfile = context / "Dockerfile"
            dockerfile.write_text("FROM " + reference + "\n", encoding="utf-8")
            unchanged = root / "from.tar"
            build(dockerfile, context, source, None, "example/copy:1", unchanged)
            original = ImageArchiveReader(base, root / "base-read").read(reference)
            copied = ImageArchiveReader(unchanged, root / "copy-read").read("example/copy:1")
            self.assertEqual(copied.config_raw, original.config_raw)
            self.assertEqual(copied.config_digest, original.config_digest)
            settings = root / "config.json"
            settings.write_text(json.dumps({"schemaVersion": 2, "cache": {"imageStore": str(root / "store")},
                "fast": {"profiles": {"tomcat": {"flavor": "tomcat", "baseImage": reference,
                                                 "baseUrl": "http://" + reference}}}}), encoding="utf-8")
            war = root / "hisq.war"
            war.write_bytes(b"test-war")
            identities = []
            for number in range(2):
                stats = {}
                with patch("image_store.make_opener", side_effect=AssertionError("cached base must not download")):
                    output, tag = quick_build(war, "hisq", "5.0.8", settings, profile_name="tomcat",
                                             cache_dir=root / "layers", cache_stats=stats,
                                             output=root / ("hisq-{}.tar".format(number)))
                self.assertEqual(stats["hits"], number)
                image = ImageArchiveReader(output, root / ("war-read-{}".format(number))).read(tag)
                identities.append(image.config_digest)
                self.assertEqual(image.layers[0].read_bytes(), layer.read_bytes())
                index = RootFSIndex()
                for path in image.layers:
                    index.apply_layer(path)
                self.assertEqual(index.read_file("/soft/tomcat/webapps/hisq.war"), b"test-war")
                self.assertEqual(index.entries["soft/tomcat/webapps/hisq.war"].metadata.uid, 1000)
            self.assertEqual(identities[0], identities[1])

    def test_add_chmod_cannot_hide_conflicting_source_type_bits(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            layer = full_mode_layer(root / "source.tar")
            raw = bytearray(layer.read_bytes())
            header = raw[512:1024]
            header[100:108] = b"0100755\0"  # 目录 typeflag 与普通文件 st_mode 冲突。
            header[148:156] = b"        "
            header[148:156] = ("%06o\0 " % sum(header)).encode("ascii")
            raw[512:1024] = header
            layer.write_bytes(raw)
            dockerfile = root / "Dockerfile"
            dockerfile.write_text("FROM scratch\nADD --chmod=0755 source.tar /\n", encoding="utf-8")
            with self.assertRaisesRegex(BuildError, "permission mode"):
                build(dockerfile, root, None, None, "example/add:1", root / "bad.tar")

    def test_materializer_applies_full_mode_layer_without_passing_type_bits_to_syscalls(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            layer = full_mode_layer(root / "source.tar")
            with patch("rootfs_materializer.os.chown", create=True), \
                    patch("rootfs_materializer.os.chmod") as chmod, patch("rootfs_materializer.os.utime"):
                materializer = RootFSMaterializer(root / "fs")
                materializer.apply(layer)
            self.assertEqual((root / "fs/soft/tomcat/start.sh").read_bytes(), b"#!/bin/sh\n")
            modes = [call[0][1] for call in chmod.call_args_list]
            self.assertTrue(all(0 <= mode <= 0o7777 for mode in modes))
            self.assertIn(0o4755, modes)

    def test_materializer_uses_only_permission_bits_for_chmod(self):
        member = tarfile.TarInfo("app")
        member.type, member.mode = tarfile.DIRTYPE, stat.S_IFDIR | 0o2755
        materializer = object.__new__(RootFSMaterializer)
        materializer.rootless = False
        with patch("rootfs_materializer.os.chown", create=True), \
                patch("rootfs_materializer.os.chmod") as chmod, patch("rootfs_materializer.os.utime"):
            materializer._metadata(Path("app"), member)
        self.assertEqual(chmod.call_args[0][1], 0o2755)

    def test_stage_copy_normalizes_reopened_symlink_headers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            layer = full_mode_layer(root / "layer.tar", links=True)
            base = root / "base.tar"
            config = {"os": "linux", "architecture": "amd64", "config": {},
                      "rootfs": {"type": "layers", "diff_ids": ["sha256:" + sha256_file(layer)]}}
            ImageArchiveWriter().write_new(base, config, [layer], "example/tomcat:1")
            dockerfile = root / "Dockerfile"
            dockerfile.write_text("FROM example/tomcat:1 AS source\nFROM scratch\n"
                                  "COPY --from=source /soft/tomcat/ /app/\n", encoding="utf-8")
            output = root / "stage.tar"
            build(dockerfile, root, base, None, "example/app:1", output)
            image = ImageArchiveReader(output, root / "read").read("example/app:1")
            index = RootFSIndex()
            for path in image.layers:
                index.apply_layer(path)
            self.assertEqual(index.read_file("/app/current.sh"), b"#!/bin/sh\n")
            self.assertEqual(index.read_file("/app/start-copy.sh"), b"#!/bin/sh\n")
            self.assertEqual(index.entries["app/start.sh"].metadata.mode, 0o4755)


if __name__ == "__main__":
    unittest.main()
