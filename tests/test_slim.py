"""验证瘦身的文件树语义、保护、压缩、输入身份和多产物回滚。"""

import contextlib
import copy
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

from cas_store import CASStore
from errors import BuildError
from image_reader import BaseImage, ImageArchiveReader, sha256_file
from image_writer import ImageArchiveWriter
from oci_writer import OCIImageWriter
from rootfs import RootFSIndex
from slim import slim_image
from slim_rules import load_patterns, normalize_pattern, runtime_rules
import slim
from test_rootfs_commands import tar_at

TAG = "example/slim:1"
PROJECT = Path(__file__).resolve().parents[1]


def fixture(root, kind="docker"):
    first, second = root / "first.tar", root / "second.tar"
    tar_at(first, [(".", tarfile.DIRTYPE, ""), ("app/start.sh", tarfile.REGTYPE, b"start"),
                   ("app/cache/item", tarfile.REGTYPE, b"temporary"),
                   ("docs/guide", tarfile.REGTYPE, b"documentation" * 1000),
                   ("etc/ssl/cert", tarfile.REGTYPE, b"certificate"),
                   ("etc/group", tarfile.REGTYPE, b"app:x:10:"),
                   ("bin/java", tarfile.SYMTYPE, "../lib/java/bin/java"),
                   ("soft/java", tarfile.SYMTYPE, "/lib/java"),
                   ("lib/java/bin/java", tarfile.REGTYPE, b"java"),
                   ("lib/java/lib/classes", tarfile.REGTYPE, b"classes"),
                   ("lib/java/lib/native", tarfile.REGTYPE, b"native"),
                   ("old-name", tarfile.REGTYPE, b"original inode"),
                   ("alias", tarfile.LNKTYPE, "old-name"),
                   ("dev/pipe", tarfile.FIFOTYPE, "")])
    tar_at(second, [("old-name", tarfile.REGTYPE, b"new inode"),
                    ("docs/.wh.guide", tarfile.REGTYPE, b""),
                    ("docs/current", tarfile.REGTYPE, b"latest documentation")])
    runtime = {"Env": ["PATH=/bin", "JAVA_HOME=/soft/java"], "WorkingDir": "/app",
               "Entrypoint": ["java"], "Cmd": ["-jar", "/app/app.war"], "User": "1000:1000",
               "Healthcheck": {"Test": ["CMD", "/app/start.sh"]}, "StopSignal": "SIGQUIT",
               "Volumes": {"/logs": {}}, "Labels": {"purpose": "fixture"}}
    config = {"os": "linux", "architecture": "amd64", "config": runtime,
              "rootfs": {"type": "layers", "diff_ids": ["sha256:" + sha256_file(p) for p in (first, second)]},
              "history": [{}, {}], "x-company": {"original": True}}
    path = root / ("source.tar" if kind == "docker" else "source.oci.tar")
    raw = (json.dumps(config, indent=3) + "\n").encode()
    writer = ImageArchiveWriter() if kind == "docker" else OCIImageWriter()
    writer.write_image(path, BaseImage(config, [first, second], [TAG], {}, config_raw=raw), TAG)
    return path, config


def read(path, root):
    image = ImageArchiveReader(path, root).read(TAG)
    index = RootFSIndex()
    for layer in image.layers:
        index.apply_layer(layer)
    return image, index


class SlimTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="slim-tests-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source, self.config = fixture(self.root)

    def test_default_flatten_preserves_runtime_root_metadata_special_files_and_hardlinks(self):
        target = self.root / "slim.tar"
        before = self.source.read_bytes()
        result = slim_image(self.source, target)
        image, index = read(target, self.root / "read")
        _, original = read(self.source, self.root / "original")
        self.assertEqual(image.config["config"], self.config["config"])
        self.assertEqual(image.config["x-company"], self.config["x-company"])
        self.assertEqual(set(index.entries), set(original.entries))
        self.assertEqual(index.root_metadata.mode, original.root_metadata.mode)
        self.assertEqual(index.root_metadata.uid, original.root_metadata.uid)
        self.assertEqual(index.root_metadata.pax_headers, original.root_metadata.pax_headers)
        self.assertEqual(index.read_file("/alias"), b"original inode")
        self.assertEqual(index.read_file("/old-name"), b"new inode")
        self.assertEqual(index.entries["dev/pipe"].kind, original.entries["dev/pipe"].kind)
        self.assertEqual(result["layers"], 1)
        self.assertNotEqual(result["source_image_id"], result["image_id"])
        self.assertEqual(result["decision"]["removed_paths"], [])
        self.assertEqual(self.source.read_bytes(), before)

    def test_delete_with_manual_keep_preserves_parents_and_alias_content(self):
        target = self.root / "pruned.tar"
        result = slim_image(self.source, target, deletes=["app", "old-name", "docs"], keeps=["app/start.sh"],
                            auto_protect=False)
        _, index = read(target, self.root / "read")
        self.assertIn("app", index.entries)
        self.assertIn("app/start.sh", index.entries)
        self.assertNotIn("app/cache", index.entries)
        self.assertNotIn("docs", index.entries)
        self.assertNotIn("old-name", index.entries)
        self.assertEqual(index.read_file("/alias"), b"original inode")
        self.assertGreater(result["decision"]["removed_unique_bytes"], 0)
        self.assertTrue(any(p["path"] == "/app/start.sh" for p in result["decision"]["protected_paths"]))

    def test_automatic_protection_covers_workdir_certificates_home_and_intermediate_links(self):
        result = slim_image(self.source, analyze_only=True, deletes=["app", "etc", "soft", "lib", "bin"])
        removed = {p["path"] for p in result["decision"]["removed_paths"]}
        protected = {p["path"] for p in result["decision"]["protected_paths"]}
        for path in ("/app/start.sh", "/etc/ssl/cert", "/soft/java", "/lib/java/bin/java",
                     "/lib/java/lib/classes", "/bin/java"):
            self.assertNotIn(path, removed)
            self.assertIn(path, protected)

    def test_keep_a_symlinked_directory_retains_its_tree_without_retaining_unrelated_files(self):
        result = slim_image(self.source, analyze_only=True, deletes=["*"],
                            keeps=["soft/java"], auto_protect=False)
        removed = {p["path"] for p in result["decision"]["removed_paths"]}
        self.assertNotIn("/lib/java/lib/native", removed)
        self.assertIn("/docs/current", removed)
        self.assertIn("/app/start.sh", removed)

    def test_keep_through_intermediate_symlink_retains_only_required_target(self):
        result = slim_image(self.source, analyze_only=True, deletes=["*"],
                            keeps=["soft/java/lib/classes"], auto_protect=False)
        removed = {p["path"] for p in result["decision"]["removed_paths"]}
        self.assertNotIn("/lib/java/lib/classes", removed)
        self.assertNotIn("/soft/java", removed)
        # 保留下来的 symlink 指向目录，保守保留整个目录，而不是留下断链。
        self.assertNotIn("/lib/java/lib/native", removed)
        self.assertIn("/docs/current", removed)

    def test_analyze_only_writes_complete_report_without_image_or_source_mutation(self):
        report = self.root / "plan.json"
        before = self.source.read_bytes()
        result = slim_image(self.source, analyze_only=True, report=report, deletes=["docs"])
        self.assertEqual(json.loads(report.read_bytes()), result)
        self.assertIsNone(result["output"])
        self.assertIsNone(result["under_upload_limit"])
        self.assertFalse((self.root / "source-slim.tar").exists())
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual([p["path"] for p in result["decision"]["removed_paths"]], ["/docs", "/docs/current"])

    def test_gzip_is_reproducible_and_limit_uses_uncompressed_archive_bytes(self):
        targets = [self.root / ("out%d.tar.gz" % n) for n in (1, 2)]
        results = [slim_image(self.source, path, gzip_output=True, upload_limit=1024) for path in targets]
        self.assertEqual(targets[0].read_bytes(), targets[1].read_bytes())
        raw = gzip.decompress(targets[0].read_bytes())
        self.assertEqual(len(raw), results[0]["uncompressed_archive_bytes"])
        self.assertFalse(results[0]["under_upload_limit"])
        self.assertGreater(len(raw), results[0]["after_bytes"])
        unpacked = self.root / "decompressed.tar"
        unpacked.write_bytes(raw)
        ImageArchiveWriter().verify(unpacked, TAG)
        from image_cli import verify_archive
        self.assertEqual(verify_archive(targets[0], TAG)["layers_verified"], 1)

    def test_rule_files_keep_bom_comments_and_reject_traversal_or_root(self):
        deletes, keeps = self.root / "delete.txt", self.root / "keep.txt"
        deletes.write_text("\ufeff# delete\n/docs\n/app\n", encoding="utf-8")
        keeps.write_text("/app/start.sh\n", encoding="utf-8")
        result = slim_image(self.source, analyze_only=True, delete_files=[deletes], keep_files=[keeps],
                            auto_protect=False)
        self.assertEqual(result["decision"]["delete_patterns"], ["docs", "app"])
        for pattern in ("/", "//", "/cache/..", "a/../b", "a\\b", "\x00", ""):
            with self.subTest(pattern=pattern), self.assertRaises(BuildError):
                normalize_pattern(pattern)
        self.assertEqual(normalize_pattern("//cache//./*"), "cache/*")

    def test_hardlink_unique_bytes_are_not_counted_twice(self):
        result = slim_image(self.source, analyze_only=True, deletes=["alias"], auto_protect=False)
        # 原名字已被覆盖，alias 是旧 inode 唯一可见名字。
        self.assertEqual(result["decision"]["removed_unique_bytes"], len(b"original inode"))

    def test_report_publish_failure_rolls_back_image_and_preserves_existing_report(self):
        from file_publish import publish_new_file
        target, report = self.root / "out.tar", self.root / "out.json"
        def publish(source, destination):
            if destination == report:
                raise OSError("report publish failed")
            return publish_new_file(source, destination)
        with patch.object(slim, "publish_new_file", side_effect=publish):
            with self.assertRaisesRegex(OSError, "report publish failed"):
                slim_image(self.source, target, report=report)
        self.assertFalse(target.exists())
        self.assertFalse(report.exists())
        self.assertTrue(self.source.exists())

    def test_workspace_cleanup_failure_rolls_back_both_artifacts(self):
        factory = tempfile.TemporaryDirectory
        target, report = self.root / "out.tar", self.root / "out.json"
        class FailedCleanup:
            def __init__(self, *args, **kwargs):
                self.inner = factory(*args, **kwargs)
            def __enter__(self):
                return self.inner.__enter__()
            def __exit__(self, *args):
                self.inner.__exit__(*args)
                raise OSError("workspace cleanup failed")
        with patch.object(slim.tempfile, "TemporaryDirectory", FailedCleanup):
            with self.assertRaisesRegex(OSError, "workspace cleanup failed"):
                slim_image(self.source, target, report=report)
        self.assertFalse(target.exists())
        self.assertFalse(report.exists())

    def test_invalid_options_or_existing_outputs_do_not_touch_inputs(self):
        existing = self.root / "existing.json"
        existing.write_text("keep", encoding="utf-8")
        cases = ({"report": existing}, {"upload_limit": 0}, {"compress_level": 0}, {"largest": 0},
                 {"gzip_output": True}, {"deletes": ["/cache/.."]})
        for options in cases:
            with self.subTest(options=options), self.assertRaises((BuildError, OSError)):
                slim_image(self.source, self.root / "out.tar", **options)
        self.assertEqual(existing.read_text(), "keep")
        self.assertFalse((self.root / "out.tar").exists())

    def test_oci_and_cas_inputs_produce_docker_archives(self):
        other = self.root / "oci";other.mkdir()
        archive, _ = fixture(other, "oci")
        result = slim_image(archive, self.root / "from-oci.tar", deletes=["docs"])
        self.assertEqual(result["tag"], TAG)
        store = self.root / "store"
        CASStore(store).import_docker(self.source, TAG, "linux/amd64", "local")
        result = slim_image(TAG, self.root / "from-cas.tar", store=store, kind="local")
        self.assertEqual(result["layers"], 1)

    def test_compressed_input_and_all_standalone_entrypoints(self):
        compressed = self.root / "input.tar.gz"
        compressed.write_bytes(gzip.compress(self.source.read_bytes()))
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        for number, entry in enumerate((["main.py", "slim"], ["slim.py"], ["shrink_docker_archive.py"])):
            target = self.root / ("cli-%d.tar" % number)
            result = subprocess.run([sys.executable]+[str(PROJECT / entry[0])]+entry[1:]+[
                str(compressed), "-o", str(target), "--delete", "/docs", "--progress", "json"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=str(PROJECT), timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
            self.assertEqual(json.loads(result.stdout)["layers"], 1)
            _, index = read(target, self.root / ("cli-read-%d" % number))
            self.assertNotIn("docs", index.entries)

    def test_gzip_verification_failure_does_not_publish(self):
        target, report = self.root / "invalid.tar.gz", self.root / "report.json"
        with patch.object(slim.gzip, "open", return_value=io.BytesIO(b"wrong decompressed content")):
            with self.assertRaisesRegex(BuildError, "Compressed image verification failed"):
                slim_image(self.source, target, report=report)
        self.assertFalse(target.exists())
        self.assertFalse(report.exists())

    def test_rule_files_are_bounded_and_must_be_regular_files(self):
        huge = self.root / "rules.txt"
        huge.write_bytes(b"x" * (1024 * 1024 + 1))
        with self.assertRaises(BuildError):
            load_patterns(files=[huge])
        with self.assertRaises(OSError):
            load_patterns(files=[self.root])

    def test_invalid_runtime_fields_fail_with_a_build_error(self):
        for runtime in (True, False, {"Env": 0}, {"Env": False}, {"Env": 7}, {"WorkingDir": 0},
                        {"WorkingDir": 5}, {"Entrypoint": [1]}, {"Cmd": 2}, {"Healthcheck": False},
                        {"Volumes": False}):
            with self.subTest(runtime=runtime), self.assertRaises(BuildError):
                runtime_rules({"config": runtime}, {})

    def test_cli_output_failure_rolls_back_published_files(self):
        class BrokenOutput:
            def write(self, text):
                raise BrokenPipeError("stdout unavailable")
        target, report = self.root / "out.tar", self.root / "out.json"
        with contextlib.redirect_stdout(BrokenOutput()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(slim.main([str(self.source), "-o", str(target), "--report", str(report)]), 1)
        self.assertFalse(target.exists())
        self.assertFalse(report.exists())

    def test_cancellation_during_report_publish_rolls_back(self):
        from file_publish import publish_new_file
        target, report = self.root / "out.tar", self.root / "out.json"
        def publish(source, destination):
            if destination == report:
                raise KeyboardInterrupt("cancelled")
            return publish_new_file(source, destination)
        with patch.object(slim, "publish_new_file", side_effect=publish):
            with self.assertRaises(KeyboardInterrupt):
                slim_image(self.source, target, report=report)
        self.assertFalse(target.exists())
        self.assertFalse(report.exists())

    def test_input_change_during_snapshot_is_rejected(self):
        from file_resources import open_regular_file
        target = self.root / "out.tar"
        source = self.source
        class MutatingInput:
            def __enter__(self):
                self.stream = open_regular_file(source)
                self.modified = False
                return self
            def __exit__(self, *args):
                self.stream.close()
            def fileno(self):
                return self.stream.fileno()
            def read(self, count):
                result = self.stream.read(count)
                if result and not self.modified:
                    self.modified = True
                    with source.open("ab") as extra:
                        extra.write(b"changed")
                return result
        with patch.object(slim, "open_regular_file", return_value=MutatingInput()):
            with self.assertRaisesRegex(BuildError, "input changed"):
                slim_image(source, target)
        self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
