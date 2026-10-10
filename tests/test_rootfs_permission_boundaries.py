"""验证虚拟路径读取与 RUN 元数据不会误用宿主文件系统语义。"""

import io
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from builder import build
from errors import ArchiveError, BuildError
from rootfs import RootFSIndex
from rootfs_materializer import RootFSMaterializer


def identity_layer(path, target):
    with tarfile.open(path, "w") as archive:
        for name, value in (("data/passwd", b"app:x:1201:1202::/:/bin/sh\n"),
                            ("blocked", b"regular file")):
            member = tarfile.TarInfo(name)
            member.size = len(value)
            archive.addfile(member, io.BytesIO(value))
        directory = tarfile.TarInfo("data/nested")
        directory.type = tarfile.DIRTYPE
        archive.addfile(directory)
        member = tarfile.TarInfo("etc/passwd")
        member.type, member.linkname = tarfile.SYMTYPE, target
        archive.addfile(member)


class RootfsPermissionBoundaries(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def _index(self, target):
        path = self.root / "layer.tar"
        identity_layer(path, target)
        result = RootFSIndex()
        result.apply_layer(path)
        return result

    def test_reading_symlink_cannot_traverse_regular_file_before_dotdot(self):
        index = self._index("/blocked/../data/passwd")
        with self.assertRaisesRegex(ArchiveError, "non-directory"):
            index.read_file("/etc/passwd")

    def test_missing_component_before_dotdot_does_not_resolve_existing_file(self):
        index = self._index("/missing/../data/passwd")
        self.assertIsNone(index.read_file("/etc/passwd"))

    def test_directory_parent_traversal_and_relative_symlink_still_work(self):
        for target in ("/data/nested/../passwd", "../data/nested/../passwd"):
            with self.subTest(target=target):
                index = self._index(target)
                self.assertEqual(index.read_file("/etc/passwd"), b"app:x:1201:1202::/:/bin/sh\n")

    def test_symlink_target_trailing_slash_requires_a_directory(self):
        index = self._index("/data/passwd/")
        with self.assertRaisesRegex(ArchiveError, "non-directory"):
            index.read_file("/etc/passwd")

    def test_chown_build_rejects_false_passwd_path(self):
        context = self.root / "context"
        context.mkdir()
        (context / "passwd").write_text("app:x:1201:1202::/:/bin/sh\n", encoding="utf-8")
        (context / "blocked").write_bytes(b"regular file")
        (context / "payload").write_bytes(b"WAR")
        dockerfile = context / "Dockerfile"
        dockerfile.write_text("FROM scratch\nCOPY passwd /data/passwd\nCOPY blocked /blocked\n"
                              "ADD link.tar /etc/\nCOPY --chown=app payload /app.war\n", encoding="utf-8")
        with tarfile.open(context / "link.tar", "w") as archive:
            member = tarfile.TarInfo("passwd")
            member.type, member.linkname = tarfile.SYMTYPE, "/blocked/../data/passwd"
            archive.addfile(member)
        output = self.root / "bad.tar"
        with self.assertRaises(BuildError):
            build(dockerfile, context, None, None, "app:1", output)
        self.assertFalse(output.exists())

    def test_materializer_rejects_invalid_owners_before_any_layer_write(self):
        for field in ("uid", "gid"):
            for value in (-1, -2, (1 << 32) - 1, 1 << 32, 1 << 80):
                with self.subTest(field=field, value=value):
                    root = self.root / (field + str(value))
                    layer = self.root / "owner.tar"
                    with tarfile.open(layer, "w", format=tarfile.PAX_FORMAT) as archive:
                        first = tarfile.TarInfo("first")
                        archive.addfile(first)
                        member = tarfile.TarInfo("invalid")
                        setattr(member, field, value)
                        archive.addfile(member)
                    with patch.object(RootFSMaterializer, "_metadata"):
                        materializer = RootFSMaterializer(root)
                        with self.assertRaisesRegex(BuildError, "layer " + field.upper()):
                            materializer.apply(layer)
                    self.assertFalse((root / "first").exists())

    def test_direct_metadata_rejects_negative_owner_before_chown(self):
        materializer = object.__new__(RootFSMaterializer)
        materializer.rootless = False
        member = tarfile.TarInfo("file")
        member.uid = -1
        with patch("rootfs_materializer.os.chown", create=True) as chown, \
                patch("rootfs_materializer.os.chmod"), patch("rootfs_materializer.os.utime"):
            with self.assertRaisesRegex(BuildError, "layer UID"):
                materializer._metadata(self.root / "file", member)
            chown.assert_not_called()

    def test_file_owner_range_is_not_limited_to_execution_user_range(self):
        materializer = object.__new__(RootFSMaterializer)
        materializer.rootless = False
        member = tarfile.TarInfo("file")
        member.uid, member.gid = 1 << 31, (1 << 32) - 2
        path = self.root / "file"
        with patch("rootfs_materializer.os.chown", create=True) as chown, \
                patch("rootfs_materializer.os.chmod"), patch("rootfs_materializer.os.utime"):
            materializer._metadata(path, member)
            chown.assert_called_once_with(path, member.uid, member.gid, follow_symlinks=False)

    def test_out_of_range_timestamp_reports_build_error(self):
        materializer = object.__new__(RootFSMaterializer)
        materializer.rootless = False
        member = tarfile.TarInfo("file")
        for error in (OverflowError("timestamp out of range"), ValueError("invalid timestamp")):
            with self.subTest(error=type(error).__name__), \
                    patch("rootfs_materializer.os.chown", create=True), \
                    patch("rootfs_materializer.os.chmod"), \
                    patch("rootfs_materializer.os.utime", side_effect=error):
                with self.assertRaises(BuildError):
                    materializer._metadata(self.root / "file", member)


if __name__ == "__main__":
    unittest.main()
