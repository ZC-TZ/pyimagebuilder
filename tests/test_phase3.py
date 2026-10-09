import io
import os
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import mock_open, patch


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from builder import build
from dockerfile_parser import parse
from executor import _resolve_user
from errors import ArchiveError, UnsupportedInstruction
from image_reader import ImageArchiveReader
from rootfs import RootFSIndex
from test_phase1 import make_base


def archive_member(archive, name, data=b"", kind=tarfile.REGTYPE, link=""):
    info = tarfile.TarInfo(name)
    info.type = kind
    info.mode = 0o640
    info.uid, info.gid = 12, 34
    info.mtime = 123456
    info.linkname = link
    info.size = len(data) if kind == tarfile.REGTYPE else 0
    archive.addfile(info, io.BytesIO(data) if kind == tarfile.REGTYPE else None)


class PhaseThreeTests(unittest.TestCase):
    def test_run_user_primary_and_supplementary_groups(self):
        files = {
            "/etc/passwd": "app:x:1201:1202::/app:/bin/sh\n",
            "/etc/group": "staff:x:1300:app\nprimary:x:1202:\n",
        }

        def open_image_file(path, **kwargs):
            return mock_open(read_data=files[path])()

        with patch("builtins.open", side_effect=open_image_file):
            self.assertEqual(_resolve_user("app"), (1201, 1202, [1300]))
            self.assertEqual(_resolve_user("app:staff"), (1201, 1300, []))

    def test_named_chown_chmod_local_add_and_links(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = make_base(root)
            context = root / "context"
            context.mkdir()
            (context / "passwd").write_bytes(b"app:x:1201:1202::/app:/bin/sh\n")
            (context / "group").write_bytes(b"staff:x:1300:\n")
            (context / "start.sh").write_bytes(b"#!/bin/sh\n")
            (context / "dist.zip").write_bytes(b"PK\x03\x04payload")
            with tarfile.open(context / "bundle.tar.gz", "w:gz") as tar:
                archive_member(tar, "pkg/", kind=tarfile.DIRTYPE)
                archive_member(tar, "pkg/empty/", kind=tarfile.DIRTYPE)
                archive_member(tar, "pkg/data.txt", b"payload")
                archive_member(tar, "pkg/shortcut", kind=tarfile.SYMTYPE, link="data.txt")
                archive_member(tar, "pkg/duplicate", kind=tarfile.LNKTYPE, link="pkg/data.txt")
            (context / "Dockerfile").write_text(
                "FROM example/base:1\n"
                "COPY passwd /etc/passwd\n"
                "COPY group /etc/group\n"
                "COPY --chmod=0750 --chown=app:staff start.sh /app/start.sh\n"
                "ADD bundle.tar.gz /opt/assets/\n"
                "ADD dist.zip /app/\n"
                "USER app\n"
                "SHELL [\"/bin/sh\", \"-c\"]\n", encoding="utf-8")
            output = root / "result.tar"
            build(context / "Dockerfile", context, base, None, "example/phase3:1", output)
            with tempfile.TemporaryDirectory() as unpacked:
                image = ImageArchiveReader(output, Path(unpacked)).read("example/phase3:1")
                index = RootFSIndex()
                for layer in image.layers:
                    index.apply_layer(layer)
                self.assertEqual(index.kind("opt/assets/pkg/shortcut"), "symlink")
                self.assertEqual(index.kind("opt/assets/pkg/duplicate"), "hardlink")
                self.assertEqual(index.kind("opt/assets/pkg/empty"), "dir")
                self.assertEqual(index.read_file("etc/passwd"), b"app:x:1201:1202::/app:/bin/sh\n")
                self.assertEqual(image.config["config"]["User"], "app")
                self.assertEqual(image.config["config"]["Shell"], ["/bin/sh", "-c"])
                with tarfile.open(image.layers[-3]) as tar:
                    script = tar.getmember("app/start.sh")
                    self.assertEqual((script.uid, script.gid, script.mode), (1201, 1300, 0o750))
                with tarfile.open(image.layers[-2]) as tar:
                    data = tar.getmember("opt/assets/pkg/data.txt")
                    self.assertEqual((data.uid, data.gid, data.mode, data.mtime), (12, 34, 0o640, 0))
                    self.assertEqual(tar.getmember("opt/assets/pkg/shortcut").linkname, "data.txt")
                    self.assertEqual(tar.getmember("opt/assets/pkg/duplicate").linkname,
                                     "opt/assets/pkg/data.txt")
                    self.assertEqual(tar.getmember("opt/assets/pkg/empty").mode, 0o640)
                with tarfile.open(image.layers[-1]) as tar:
                    self.assertEqual(tar.extractfile("app/dist.zip").read(), b"PK\x03\x04payload")

    def test_context_hardlink_and_symlink_are_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = make_base(root)
            context = root / "context"
            context.mkdir()
            files = context / "files"
            files.mkdir()
            (files / "a.txt").write_bytes(b"linked")
            os.link(files / "a.txt", files / "b.txt")
            symlink_available = True
            try:
                os.symlink("a.txt", files / "c.txt")
            except (OSError, NotImplementedError):
                symlink_available = False
            (context / "Dockerfile").write_text("FROM example/base:1\nCOPY files/ /files/\n")
            output = root / "result.tar"
            build(context / "Dockerfile", context, base, None, "example/links:1", output)
            with tempfile.TemporaryDirectory() as unpacked:
                image = ImageArchiveReader(output, Path(unpacked)).read("example/links:1")
                with tarfile.open(image.layers[-1]) as tar:
                    self.assertTrue(tar.getmember("files/a.txt").isfile())
                    self.assertTrue(tar.getmember("files/b.txt").islnk())
                    self.assertEqual(tar.getmember("files/b.txt").linkname, "files/a.txt")
                    if symlink_available:
                        self.assertTrue(tar.getmember("files/c.txt").issym())

    def test_hardlink_keeps_original_bytes_after_target_is_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = root / "context"
            context.mkdir()
            with tarfile.open(context / "links.tar", "w") as archive:
                archive_member(archive, "original", b"old")
                archive_member(archive, "alias", kind=tarfile.LNKTYPE, link="original")
            (context / "replacement").write_bytes(b"new")
            (context / "Dockerfile").write_text(
                "FROM scratch AS source\n"
                "ADD links.tar /data/\n"
                "COPY replacement /data/original\n"
                "FROM scratch\n"
                "COPY --from=source /data/alias /result\n")
            output = root / "result.tar"
            build(context / "Dockerfile", context, None, None, "example/hardlink:1", output)
            with tempfile.TemporaryDirectory() as unpacked:
                image = ImageArchiveReader(output, Path(unpacked)).read("example/hardlink:1")
                index = RootFSIndex()
                for image_layer in image.layers:
                    index.apply_layer(image_layer)
                self.assertEqual(index.read_file("result"), b"old")

    def test_hardlink_chain_can_target_file_from_a_lower_layer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first, second, third = (root / name for name in ("first.tar", "second.tar", "third.tar"))
            with tarfile.open(first, "w") as archive:
                archive_member(archive, "original", b"old")
            with tarfile.open(second, "w") as archive:
                archive_member(archive, "alias-a", kind=tarfile.LNKTYPE, link="alias-b")
                archive_member(archive, "alias-b", kind=tarfile.LNKTYPE, link="original")
            with tarfile.open(third, "w") as archive:
                archive_member(archive, "original", b"new")
            index = RootFSIndex()
            for layer in (first, second, third):
                index.apply_layer(layer)
            self.assertEqual(index.read_file("alias-a"), b"old")
            self.assertEqual(index.read_file("alias-b"), b"old")
            self.assertEqual(index.read_file("original"), b"new")

    def test_cyclic_hardlinks_are_rejected_when_layer_is_indexed(self):
        with tempfile.TemporaryDirectory() as temporary:
            layer = Path(temporary) / "cycle.tar"
            with tarfile.open(layer, "w") as archive:
                archive_member(archive, "a", kind=tarfile.LNKTYPE, link="b")
                archive_member(archive, "b", kind=tarfile.LNKTYPE, link="a")
            with self.assertRaisesRegex(ArchiveError, "Cyclic hardlink"):
                RootFSIndex().apply_layer(layer)

    def test_unsupported_flags_and_unsafe_archive_fail(self):
        with self.assertRaises(UnsupportedInstruction):
            parse("FROM example/base:1\nCOPY --chmod=+x a /a\n")
        with self.assertRaises(UnsupportedInstruction):
            parse("FROM example/base:1\nADD --unknown=true file /a\n")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = make_base(root)
            context = root / "context"
            context.mkdir()
            with tarfile.open(context / "bad.tar", "w") as tar:
                archive_member(tar, "../escape", b"bad")
            (context / "Dockerfile").write_text("FROM example/base:1\nADD bad.tar /app/\n")
            output = root / "result.tar"
            with self.assertRaises(ArchiveError):
                build(context / "Dockerfile", context, base, None, "example/bad:1", output)
            self.assertFalse(output.exists())
            with tarfile.open(context / "badlink.tar", "w") as tar:
                archive_member(tar, "alias", kind=tarfile.SYMTYPE, link="/etc")
                archive_member(tar, "alias/passwd", b"bad")
            (context / "Dockerfile").write_text("FROM example/base:1\nADD badlink.tar /app/\n")
            with self.assertRaises(UnsupportedInstruction):
                build(context / "Dockerfile", context, base, None, "example/bad:1", output)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
