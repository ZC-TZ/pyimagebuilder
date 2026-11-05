"""验证跨盘归档减少复制，同时保留输出身份、上下文边界和失败回滚。"""

import errno
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import builder
import file_publish
from attest import context_digest
from errors import BuildError
from image_writer import ImageArchiveWriter
from oci_writer import OCIImageWriter
from progress import make_reporter
from test_publication_scope import failing_cleanup


@contextmanager
def other_device(*directories):
    """只改变规划阶段的设备号，实际仍使用宿主文件系统验证硬链接及文件内容。"""
    original = Path.stat

    def changed(path, *args, **kwargs):
        data = original(path, *args, **kwargs)
        if path in directories:
            fields = list(data)
            fields[2] += 1000
            return os.stat_result(fields)
        return data

    with patch.object(Path, "stat", changed):
        yield


class ExportStagingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.context = self.root / "context"
        self.context.mkdir()
        (self.context / "payload").write_bytes(b"WAR content")
        self.recipe = self.context / "Dockerfile"
        self.recipe.write_text("FROM scratch\nCOPY payload /app.war\n", encoding="utf-8")
        self.outputs = self.root / "outputs"
        self.outputs.mkdir()

    def build(self, name="image.tar", **options):
        return builder.build(self.recipe, self.context, None, None, "example/app:1",
                             self.outputs / name, **options)

    def assert_clean(self):
        self.assertFalse(list(self.root.rglob("pyimagebuilder-export-*")))

    def test_cross_device_export_publishes_without_full_archive_copy(self):
        observed = []
        original = builder.publish_new_file

        def record(source, target):
            observed.append((Path(source), Path(target)))
            return original(source, target)

        with other_device(self.outputs), patch.object(builder, "publish_new_file", side_effect=record), \
                patch.object(file_publish.shutil, "copyfileobj", side_effect=AssertionError("no full copy")):
            self.build()
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0][0].parent.parent, self.outputs)
        ImageArchiveWriter().verify(self.outputs / "image.tar", "example/app:1")
        self.assert_clean()

    def test_both_formats_stage_at_their_own_output_directories(self):
        second_directory = self.root / "oci-outputs"
        second_directory.mkdir()
        second = second_directory / "image.tar"
        observed = []
        original = builder.publish_new_file

        def record(source, target):
            observed.append((Path(source), Path(target)))
            return original(source, target)

        with other_device(self.outputs, second_directory), \
                patch.object(builder, "publish_new_file", side_effect=record):
            self.build(image_format="both", oci_output=second)
        self.assertEqual(len(observed), 2)
        for source, destination in observed:
            self.assertEqual(source.parent.parent, destination.parent)
        OCIImageWriter().verify(second, "example/app:1")
        self.assert_clean()

    def test_explicit_workspace_is_kept(self):
        workspace = self.root / "workspace"
        original = builder.publish_new_file
        observed = []

        def record(source, target):
            observed.append(Path(source))
            return original(source, target)

        with other_device(self.outputs), patch.object(builder, "publish_new_file", side_effect=record):
            self.build(workspace_dir=workspace)
        self.assertIn(workspace, observed[0].parents)
        self.assertNotIn(self.outputs, observed[0].parents)
        self.assert_clean()

    def test_output_inside_context_does_not_pollute_provenance(self):
        self.outputs = self.context / "images"
        self.outputs.mkdir()
        before = context_digest(self.context)
        with other_device(self.outputs):
            self.build(attest=True)
        provenance = json.loads((self.outputs / "image.tar.provenance.json").read_text())
        inputs = provenance["predicate"]["buildDefinition"]["resolvedDependencies"]
        self.assertEqual(next(item["digest"]["sha256"] for item in inputs
                              if item["uri"] == "context:build-context"), before)
        self.assert_clean()

    def test_unavailable_staging_or_hardlinks_fall_back_to_workspace(self):
        real_directory, real_link = tempfile.TemporaryDirectory, os.link
        for denied in ("directory", "hardlinks"):
            with self.subTest(denied=denied):
                observed = []
                original = builder.publish_new_file

                def directory(*args, **kwargs):
                    if denied == "directory" and kwargs.get("prefix") == "pyimagebuilder-export-":
                        raise PermissionError("directory denied")
                    return real_directory(*args, **kwargs)

                def link(source, target, *args, **kwargs):
                    if denied == "hardlinks" and Path(target).name == "probe.link":
                        raise OSError(errno.EPERM, "no hardlinks")
                    return real_link(source, target, *args, **kwargs)

                def record(source, target):
                    observed.append(Path(source))
                    return original(source, target)

                with other_device(self.outputs), patch.object(builder.tempfile, "TemporaryDirectory", directory), \
                        patch.object(builder.os, "link", side_effect=link), \
                        patch.object(builder, "publish_new_file", side_effect=record):
                    self.build(denied + ".tar")
                self.assertNotIn(self.outputs, observed[0].parents)
                self.assert_clean()

    def test_verification_failure_removes_private_staging(self):
        with other_device(self.outputs), \
                patch.object(ImageArchiveWriter, "verify", side_effect=BuildError("bad archive")):
            with self.assertRaisesRegex(BuildError, "bad archive"):
                self.build()
        self.assertFalse((self.outputs / "image.tar").exists())
        self.assert_clean()

    def test_staging_flush_failure_does_not_publish_output(self):
        with other_device(self.outputs), patch.object(builder.os, "fsync", side_effect=OSError("flush failed")):
            with self.assertRaisesRegex(OSError, "flush failed"):
                self.build()
        self.assertFalse((self.outputs / "image.tar").exists())
        self.assert_clean()

    def test_staging_cleanup_failure_rolls_back_all_products(self):
        with other_device(self.outputs), failing_cleanup("pyimagebuilder-export-"):
            with self.assertRaisesRegex(OSError, "cleanup failed"):
                self.build(image_format="both", attest=True)
        self.assertEqual(list(self.outputs.iterdir()), [])
        self.assert_clean()

    def test_archive_and_attestation_bytes_match_explicit_workspace(self):
        seed = self.root / "seed.hex"
        seed.write_text("01" * 32)
        self.build("baseline.tar", image_format="both", attest=True, sign_key=seed,
                   workspace_dir=self.root / "workspace")
        with other_device(self.outputs):
            self.build("optimized.tar", image_format="both", attest=True, sign_key=seed)
        for suffix in (".tar", ".oci.tar", ".tar.spdx.json", ".tar.provenance.json", ".tar.dsse.json"):
            self.assertEqual((self.outputs / ("baseline" + suffix)).read_bytes(),
                             (self.outputs / ("optimized" + suffix)).read_bytes())
        self.assert_clean()

    def test_publication_has_separate_stage_timings(self):
        reporter = make_reporter(progress="plain", stream=io.StringIO())
        self.addCleanup(reporter.close)
        with other_device(self.outputs):
            self.build(image_format="both", reporter=reporter)
        for label in ("Write Docker archive", "Verify Docker archive", "Publish Docker archive",
                      "Write OCI archive", "Verify OCI archive", "Publish OCI archive"):
            self.assertIn(label, reporter.timings)
            self.assertGreaterEqual(reporter.timings[label], 0)


if __name__ == "__main__":
    unittest.main()
