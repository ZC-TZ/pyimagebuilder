"""验证纯压缩逐字节保留、镜像身份、多镜像归档及异常发布回滚。"""

import contextlib
import gzip
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import archive_compression
import compress
from archive_compression import gzip_stream
from compress import compress_archive
from errors import BuildError
from image_reader import BaseImage, ImageArchiveReader, sha256_file
from oci_writer import OCIImageWriter
from multiarch import combine
from test_rootfs_commands import tar_at
from test_slim import fixture, TAG

PROJECT = Path(__file__).resolve().parents[1]


class CompressionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="compress-tests-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source, self.config = fixture(self.root)

    def test_preserves_every_tar_byte_config_identity_and_layers(self):
        original = self.source.read_bytes()
        before = ImageArchiveReader(self.source, self.root / "before").read(TAG)
        target = self.root / "result.tar.gz"
        report = self.root / "report.json"
        result = compress_archive(self.source, target, report=report)
        self.assertEqual(gzip.decompress(target.read_bytes()), original)
        after = ImageArchiveReader(target, self.root / "after").read(TAG)
        self.assertEqual(after.config_raw, before.config_raw)
        self.assertEqual(after.config_digest, before.config_digest)
        self.assertEqual(after.config["rootfs"], before.config["rootfs"])
        self.assertEqual(len(after.layers), len(before.layers))
        self.assertEqual(result, json.loads(report.read_text()))
        self.assertTrue(result["archive_bytes_preserved"])
        self.assertEqual(result["source_sha256"], "sha256:" + sha256_file(self.source))
        self.assertEqual(result["sha256"], "sha256:" + sha256_file(target))
        self.assertEqual(self.source.read_bytes(), original)

    def test_multiple_docker_images_and_tags_survive_without_selection(self):
        # 两条 manifest 记录共用内容，标签与原始 manifest 的空白都必须保留。
        with tarfile.open(self.source, "r:") as archive:
            entries = [(member, archive.extractfile(member).read() if member.isfile() else None)
                       for member in archive]
        rows = json.loads(next(data for member, data in entries if member.name == "manifest.json"))
        rows.append(dict(rows[0], RepoTags=["example/second:2", "example/alias:3"]))
        changed = self.root / "multiple.tar"
        with tarfile.open(changed, "w") as archive:
            for member, data in entries:
                if member.name == "manifest.json":
                    data = (json.dumps(rows, indent=4) + "\n").encode()
                    member.size = len(data)
                archive.addfile(member, io.BytesIO(data) if data is not None else None)
        target = self.root / "multiple.tgz"
        compress_archive(changed, target)
        self.assertEqual(gzip.decompress(target.read_bytes()), changed.read_bytes())
        reader = ImageArchiveReader(target, self.root / "multiple-read")
        self.assertEqual(reader.read("example/second:2").config, self.config)

    def test_oci_single_and_real_multi_platform_archive_are_unchanged(self):
        work = self.root / "oci"; work.mkdir()
        amd64, config = fixture(work, "oci")
        image = ImageArchiveReader(self.source, self.root / "layers").read(TAG)
        config["architecture"] = "arm64"
        arm64 = self.root / "arm64.tar"
        OCIImageWriter().write_new(arm64, config, image.layers, TAG)
        multi = self.root / "multi.tar"
        combine(amd64, arm64, multi, TAG, 0)
        for i, source in enumerate((amd64, multi)):
            target = self.root / ("oci-%d.tar.gz" % i)
            result = compress_archive(source, target)
            self.assertEqual(gzip.decompress(target.read_bytes()), source.read_bytes())
            self.assertEqual(result["uncompressed_archive_bytes"], source.stat().st_size)

    def test_rootfs_special_metadata_and_empty_tar_are_preserved(self):
        rootfs = self.root / "rootfs.tar"
        tar_at(rootfs, [(".", tarfile.DIRTYPE, ""), ("dev/fifo", tarfile.FIFOTYPE, ""),
                        ("data", tarfile.REGTYPE, b"payload"), ("hard", tarfile.LNKTYPE, "data")])
        empty = self.root / "empty.tar"
        with tarfile.open(empty, "w"):
            pass
        for i, source in enumerate((rootfs, empty)):
            target = self.root / ("generic-%d.tar.gz" % i)
            compress_archive(source, target)
            self.assertEqual(gzip.decompress(target.read_bytes()), source.read_bytes())

    def test_default_output_and_fixed_gzip_header_are_reproducible(self):
        first = compress_archive(self.source)
        self.assertEqual(first["output"], str(self.source) + ".gz")
        second = self.root / "other.tgz"
        compress_archive(self.source, second)
        data = second.read_bytes()
        self.assertEqual(data, Path(first["output"]).read_bytes())
        self.assertEqual(data[4:8], b"\0"*4)
        self.assertFalse(data[3] & 8)  # gzip 头不嵌入输入文件名。

    def test_limit_is_strict_uncompressed_size_and_only_a_warning(self):
        reporter = unittest.mock.Mock()
        result = compress_archive(self.source, upload_limit=self.source.stat().st_size, reporter=reporter)
        self.assertFalse(result["under_upload_limit"])
        self.assertLess(result["after_bytes"], result["upload_limit"])
        reporter.log.assert_called_once()
        second = compress_archive(self.source, self.root / "other.tgz", upload_limit=self.source.stat().st_size + 1)
        self.assertTrue(second["under_upload_limit"])

    def test_invalid_options_and_existing_files_are_never_overwritten(self):
        target, report = self.root / "exists.tgz", self.root / "exists.json"
        target.write_bytes(b"existing archive"); report.write_bytes(b"existing report")
        for options in ({"output": target}, {"report": report}, {"compress_level": True},
                        {"compress_level": 0}, {"compress_level": 10}, {"upload_limit": 0},
                        {"upload_limit": False}, {"output": self.source}, {"output": self.root / "wrong.tar"},
                        {"report": self.root / "wrong.txt"}):
            with self.subTest(options=options), self.assertRaises((BuildError, OSError)):
                compress_archive(self.source, **options)
        self.assertEqual(target.read_bytes(), b"existing archive")
        self.assertEqual(report.read_bytes(), b"existing report")
        self.assertFalse(Path(str(self.source)+".gz").exists())

    def test_rejects_garbage_truncated_and_already_compressed_input(self):
        raw = self.source.read_bytes()
        garbage = self.root / "garbage.tar";garbage.write_bytes(b"not a tar")
        truncated = self.root / "truncated.tar";truncated.write_bytes(raw[:550])
        packed = self.root / "packed.tar.gz";packed.write_bytes(gzip.compress(raw))
        for i, path in enumerate((garbage, truncated, packed, self.root, self.root / "missing.tar")):
            target = self.root / ("invalid-%d.tgz" % i)
            with self.subTest(path=path), self.assertRaises((BuildError, OSError, tarfile.TarError)):
                compress_archive(path, target)
            self.assertFalse(target.exists())

    def test_gzip_decoding_mismatch_never_publishes(self):
        target, report = self.root / "bad.tgz", self.root / "bad.json"
        with patch.object(archive_compression.gzip, "open", return_value=io.BytesIO(b"wrong decoded bytes")):
            with self.assertRaisesRegex(BuildError, "verification failed"):
                compress_archive(self.source, target, report=report)
        self.assertFalse(target.exists());self.assertFalse(report.exists())

    def test_corrupt_gzip_crc_is_detected_before_publication(self):
        target = self.root / "crc.tgz"
        original = gzip.open
        def damaged(path, *args, **kwargs):
            data = bytearray(path.read_bytes())
            data[-8] ^= 1
            path.write_bytes(data)
            return original(path, *args, **kwargs)
        with patch.object(archive_compression.gzip, "open", side_effect=damaged):
            with self.assertRaises((OSError, EOFError)):
                compress_archive(self.source, target)
        self.assertFalse(target.exists())

    def test_report_failure_or_cancellation_rolls_back_archive(self):
        original = compress.publish_new_file
        for i, failure in enumerate((OSError("report failure"), KeyboardInterrupt())):
            target, report = self.root / ("failed%d.tgz" % i), self.root / ("failed%d.json" % i)
            def fail(source, destination):
                if destination == report:
                    raise failure
                return original(source, destination)
            with patch.object(compress, "publish_new_file", side_effect=fail):
                with self.assertRaises(type(failure)):
                    compress_archive(self.source, target, report=report)
            self.assertFalse(target.exists());self.assertFalse(report.exists())

    def test_workspace_cleanup_failure_rolls_back_both_files(self):
        factory = tempfile.TemporaryDirectory
        class FailedCleanup:
            def __init__(self, *args, **kwargs):
                self.inner = factory(*args, **kwargs)
            def __enter__(self):
                return self.inner.__enter__()
            def __exit__(self, *args):
                self.inner.__exit__(*args)
                raise OSError("cleanup failure")
        target, report = self.root / "cleanup.tgz", self.root / "cleanup.json"
        with patch.object(compress.tempfile, "TemporaryDirectory", FailedCleanup):
            with self.assertRaisesRegex(OSError, "cleanup failure"):
                compress_archive(self.source, target, report=report)
        self.assertFalse(target.exists());self.assertFalse(report.exists())

    def test_changed_source_is_rejected_after_streaming(self):
        original = compress.gzip_stream
        target = self.root / "changed.tgz"
        def changed(*args, **kwargs):
            result = original(*args, **kwargs)
            self.source.write_bytes(self.source.read_bytes()+b"x")
            return result
        with patch.object(compress, "gzip_stream", side_effect=changed):
            with self.assertRaisesRegex(BuildError, "changed during read"):
                compress_archive(self.source, target)
        self.assertFalse(target.exists())

    def test_input_shrink_and_growth_are_bounded(self):
        for i, size in enumerate((2, 4)):
            with self.subTest(size=size), self.assertRaises(BuildError):
                gzip_stream(io.BytesIO(b"abc"), self.root / ("size%d.gz" % i), size, 6)

    def test_two_cli_entries_and_json_report(self):
        for i, entry in enumerate((["main.py", "compress"], ["compress.py"])):
            target, report = self.root / ("cli%d.tgz" % i), self.root / ("cli%d.json" % i)
            args=[sys.executable,str(PROJECT/entry[0])]+entry[1:]+[
                str(self.source),"-o",str(target),"--report",str(report),"--progress","json","--compress-level","1"]
            result=subprocess.run(args,cwd=str(PROJECT),stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                                  env=dict(os.environ,PYTHONIOENCODING="utf-8"),timeout=30)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(json.loads(result.stdout),json.loads(report.read_text()))
            self.assertEqual(gzip.decompress(target.read_bytes()),self.source.read_bytes())

    def test_cli_broken_stdout_rolls_back_new_artifacts(self):
        target, report = self.root / "stdout.tgz", self.root / "stdout.json"
        class BrokenOutput(io.StringIO):
            def write(self, value):
                raise BrokenPipeError("closed pipe")
        with patch.object(compress.sys,"stdout",BrokenOutput()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(compress.main([str(self.source),"-o",str(target),"--report",str(report),"--quiet"]),1)
        self.assertFalse(target.exists());self.assertFalse(report.exists())


if __name__ == "__main__":
    unittest.main()
