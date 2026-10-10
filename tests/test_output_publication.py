"""验证构建、拉取与多平台合并在最终发布时不会覆盖竞争写入者。"""

import errno
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import multiarch
import registry
import file_publish
import artifactory_download
import hermetic
import optimizer
from builder import build
from image_writer import ImageArchiveWriter
from oci_writer import OCIImageWriter


class OutputPublicationTests(unittest.TestCase):
    def prepare_context(self, root):
        context = root / "context"
        context.mkdir()
        (context / "Dockerfile").write_text("FROM scratch\nENV APP=example\n")
        return context

    def test_build_preserves_output_created_after_preflight(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = self.prepare_context(root)
            output = root / "image.tar"
            verify = ImageArchiveWriter.verify

            def competing_verify(writer, path, tag):
                verify(writer, path, tag)
                output.write_bytes(b"other build")

            with patch.object(ImageArchiveWriter, "verify", competing_verify):
                with self.assertRaises(FileExistsError):
                    build(context / "Dockerfile", context, None, None, "example/app:1", output)
            self.assertEqual(output.read_bytes(), b"other build")

    def test_build_both_rolls_back_only_its_first_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = self.prepare_context(root)
            output, oci = root / "image.tar", root / "image.oci.tar"
            verify = OCIImageWriter.verify

            def competing_verify(writer, path, tag):
                verify(writer, path, tag)
                oci.write_bytes(b"other OCI build")

            with patch.object(OCIImageWriter, "verify", competing_verify):
                with self.assertRaises(FileExistsError):
                    build(context / "Dockerfile", context, None, None, "example/app:1", output,
                          image_format="both", oci_output=oci)
            self.assertFalse(output.exists())
            self.assertEqual(oci.read_bytes(), b"other OCI build")

    def test_registry_preserves_output_created_at_final_publication(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "pulled.tar"
            raw = json.dumps({"os": "linux", "architecture": "amd64", "config": {},
                              "rootfs": {"type": "layers", "diff_ids": []}}).encode()
            manifest = {"schemaVersion": 2, "mediaType": registry.MANIFEST_TYPE,
                        "config": {"mediaType": registry.CONFIG_TYPE, "size": len(raw),
                                   "digest": "sha256:" + hashlib.sha256(raw).hexdigest()}, "layers": []}

            class Client:
                def __init__(self, reference, *_args):
                    self.reference = registry.parse_reference(reference)

                def blob(self, _descriptor, path):
                    Path(path).write_bytes(raw)

            original_link, original_replace = os.link, os.replace

            def competing_operation(operation):
                def invoke(source, destination, *args, **kwargs):
                    if Path(destination) == output:
                        output.write_bytes(b"other pull")
                    return operation(source, destination, *args, **kwargs)
                return invoke

            with patch.object(registry, "RegistryClient", Client), \
                    patch.object(registry, "_selected_manifest", return_value=(manifest, "unused")), \
                    patch.object(registry.os, "replace", side_effect=competing_operation(original_replace)), \
                    patch.object(registry.os, "link", side_effect=competing_operation(original_link)):
                with self.assertRaises(FileExistsError):
                    registry.pull("registry.example/p/app:1", output)
            self.assertEqual(output.read_bytes(), b"other pull")
            self.assertEqual(list(root.rglob("*.part")), [])

    def test_multiarch_preserves_output_created_after_validation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = self.prepare_context(root)
            inputs = []
            for architecture in ("amd64", "arm64"):
                path = root / (architecture + ".tar")
                build(context / "Dockerfile", context, None, None, "example/app:1", path,
                      image_format="oci", target_platform="linux/" + architecture)
                inputs.append(path)
            output = root / "multi.tar"
            verify = multiarch.verify

            def competing_verify(path, tag):
                verify(path, tag)
                output.write_bytes(b"other manifest")

            with patch.object(multiarch, "verify", side_effect=competing_verify):
                with self.assertRaises(FileExistsError):
                    multiarch.combine(inputs[0], inputs[1], output, "example/app:1")
            self.assertEqual(output.read_bytes(), b"other manifest")

    def test_cross_device_publication_and_existing_destination(self):
        for existing in (False, True):
            with self.subTest(existing=existing), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source, output = root / "source", root / "saved.tar"
                source.write_bytes(b"verified archive")
                if existing:
                    output.write_bytes(b"other file")
                original_link = os.link

                def cross_device(origin, target):
                    if Path(origin) == source:
                        raise OSError(errno.EXDEV, "cross-device link")
                    return original_link(origin, target)

                with patch.object(file_publish.os, "link", side_effect=cross_device):
                    if existing:
                        with self.assertRaises(FileExistsError):
                            file_publish.publish_new_file(source, output)
                    else:
                        file_publish.publish_new_file(source, output)
                self.assertEqual(output.read_bytes(), b"other file" if existing else b"verified archive")
                self.assertEqual(source.read_bytes(), b"verified archive")
                self.assertEqual(list(root.glob("*.part")), [])

    def test_filesystem_without_hardlinks_uses_exclusive_copy(self):
        for competing in (False, True):
            with self.subTest(competing=competing), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source, output = root / "source", root / "saved.tar"
                source.write_bytes(b"verified archive")
                original_open = open

                def competing_open(path, mode, *args, **kwargs):
                    if competing and Path(path) == output and mode == "xb":
                        output.write_bytes(b"other file")
                    return original_open(path, mode, *args, **kwargs)

                with patch.object(file_publish.os, "link", side_effect=OSError(errno.EPERM, "no hardlinks")), \
                        patch.object(file_publish, "open", side_effect=competing_open, create=True):
                    if competing:
                        with self.assertRaises(FileExistsError):
                            file_publish.publish_new_file(source, output)
                    else:
                        file_publish.publish_new_file(source, output)
                self.assertEqual(output.read_bytes(), b"other file" if competing else b"verified archive")
                self.assertEqual(list(root.glob("*.part")), [])

    def test_copy_failure_cleans_staging_and_exclusive_output(self):
        for phase in ("staging", "final"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source, output = root / "source", root / "saved.tar"
                source.write_bytes(b"verified archive")
                copy = file_publish.shutil.copyfileobj

                def interrupted(stream, target, buffer):
                    if phase == "staging" or target.name == str(output):
                        target.write(b"partial")
                        raise OSError("publication copy interrupted")
                    copy(stream, target, buffer)

                with patch.object(file_publish.os, "link", side_effect=OSError(errno.EPERM, "no hardlinks")), \
                        patch.object(file_publish.shutil, "copyfileobj", side_effect=interrupted):
                    with self.assertRaisesRegex(OSError, "copy interrupted"):
                        file_publish.publish_new_file(source, output)
                self.assertFalse(output.exists())
                self.assertEqual(list(root.glob("*.part")), [])
                self.assertEqual(source.read_bytes(), b"verified archive")

    def test_optimizer_preserves_competing_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = self.prepare_context(root)
            source, output = root / "source.tar", root / "optimized.tar"
            build(context / "Dockerfile", context, None, None, "example/app:1", source)
            verify = ImageArchiveWriter.verify

            def competing_verify(writer, path, tag):
                verify(writer, path, tag)
                output.write_bytes(b"other optimizer")

            with patch.object(ImageArchiveWriter, "verify", competing_verify):
                with self.assertRaises(FileExistsError):
                    optimizer.optimize_image(source, output)
            self.assertEqual(output.read_bytes(), b"other optimizer")

    def test_hermetic_report_conflict_rolls_back_only_its_image(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = self.prepare_context(root)
            output = root / "image.tar"
            report = Path(str(output) + ".hermetic.json")
            lock_path = root / "input-lock.json"
            verify = ImageArchiveWriter.verify

            def competing_verify(writer, path, tag):
                verify(writer, path, tag)
                report.write_bytes(b"other report")

            # sys.flags 是全进程对象；保留其他字段，避免影响 3.7 的默认编码查询。
            flags = SimpleNamespace(**{name: getattr(sys.flags, name) for name in dir(sys.flags)
                                       if not name.startswith("_")})
            flags.hash_randomization = 0
            with patch.dict(os.environ, {"PYTHONHASHSEED": "0"}), \
                    patch.object(hermetic.sys, "flags", flags):
                lock = hermetic.make_lock(context / "Dockerfile", context, tag="example/app:1")
                lock_path.write_text(json.dumps(lock))
                with patch.object(ImageArchiveWriter, "verify", competing_verify):
                    with self.assertRaises(FileExistsError):
                        hermetic.build_locked(lock_path, context / "Dockerfile", context,
                                              None, None, "example/app:1", output)
            self.assertFalse(output.exists())
            self.assertEqual(report.read_bytes(), b"other report")

    def test_artifactory_pack_preserves_competing_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = root / "inputs"
            inputs.mkdir()
            (inputs / "payload").write_bytes(b"data")
            output = root / "image.tar"
            original_link, original_replace = os.link, os.replace

            def competing_operation(operation):
                def invoke(source, destination, *args, **kwargs):
                    if Path(destination) == output:
                        output.write_bytes(b"other Artifactory export")
                    return operation(source, destination, *args, **kwargs)
                return invoke

            with patch.object(artifactory_download.os, "replace", side_effect=competing_operation(original_replace)), \
                    patch.object(artifactory_download.os, "link", side_effect=competing_operation(original_link)):
                with self.assertRaises(FileExistsError):
                    artifactory_download.pack_files(inputs, output, Mock(is_tty=False))
            self.assertEqual(output.read_bytes(), b"other Artifactory export")
            self.assertEqual(list(root.glob("*.part")), [])

    def test_artifactory_pack_preserves_unowned_partial_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = root / "inputs"
            inputs.mkdir()
            (inputs / "payload").write_bytes(b"data")
            output = root / "image.tar"
            pending = root / "image.tar.part"
            pending.write_bytes(b"other operation")
            artifactory_download.pack_files(inputs, output, Mock(is_tty=False))
            self.assertTrue(pending.exists())
            self.assertEqual(pending.read_bytes(), b"other operation")


if __name__ == "__main__":
    unittest.main()
