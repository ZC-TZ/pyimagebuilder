"""验证 save 和 pull -o 的交付副本：层校验、原始配置身份及中断清理。"""

import contextlib
import hashlib
import io
import json
import shutil
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))

import image_cli
import image_store
import progress
from errors import BuildError
from image_reader import sha256_file
from test_image_identity_contract import fixture, TAG
from test_phase1 import add_bytes, make_base

REFERENCE = "example/base:1"
PLATFORM = "linux/amd64"


def corrupt_layer(path):
    """只改变层内数据，保留外层 tar 和元数据，以确认不是 tar 头解析错误。"""
    with tarfile.open(path) as archive:
        member = archive.getmember("one/layer.tar")
        offset = member.offset_data + 512
    data = bytearray(path.read_bytes())
    data[offset] ^= 1
    return bytes(data)


class ArchiveCopyIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = self.root / "store"
        self.store.mkdir()

    def _legacy_archive(self):
        source = make_base(self.root)
        target = image_store._archive_path(self.store, REFERENCE, PLATFORM, "registry")
        shutil.copyfile(source, target)
        return target

    def test_save_rejects_corrupt_layer_in_tar_only_store(self):
        source = self._legacy_archive()
        damaged = corrupt_layer(source)
        source.write_bytes(damaged)
        output = self.root / "saved.tar"
        with self.assertRaises(BuildError):
            image_cli.save_archive(REFERENCE, output, self.store, source="registry")
        self.assertFalse(output.exists())
        self.assertEqual(source.read_bytes(), damaged)

    def test_save_rejects_invalid_layer_tar_even_with_matching_diff_id(self):
        source = self._legacy_archive()
        with tarfile.open(source) as archive:
            members = {member.name: archive.extractfile(member).read() for member in archive}
        manifest = json.loads(members["manifest.json"])
        entry = manifest[0]
        config = json.loads(members.pop(entry["Config"]))
        layer_name = entry["Layers"][0]
        invalid = b"!" * len(members[layer_name])
        members[layer_name] = invalid
        config["rootfs"]["diff_ids"][0] = "sha256:" + hashlib.sha256(invalid).hexdigest()
        raw_config = json.dumps(config).encode()
        entry["Config"] = hashlib.sha256(raw_config).hexdigest() + ".json"
        members[entry["Config"]] = raw_config
        members["manifest.json"] = json.dumps(manifest).encode()
        with tarfile.open(source, "w") as archive:
            for name, content in members.items():
                add_bytes(archive, name, content)
        original = source.read_bytes()
        output = self.root / "saved.tar"
        with self.assertRaises(BuildError):
            image_cli.save_archive(REFERENCE, output, self.store, source="registry")
        self.assertFalse(output.exists())
        self.assertEqual(source.read_bytes(), original)

    def test_save_rejects_layer_changed_during_copy(self):
        source = self._legacy_archive()
        original = source.read_bytes()
        damaged = corrupt_layer(source)
        output = self.root / "saved.tar"

        def altered_copy(reader, writer, *_args):
            reader.read()
            writer.write(damaged)

        with patch.object(image_store.shutil, "copyfileobj", altered_copy):
            with self.assertRaises(BuildError):
                image_cli.save_archive(REFERENCE, output, self.store, source="registry")
        self.assertFalse(output.exists())
        self.assertEqual(source.read_bytes(), original)

    def test_save_rejects_config_reencoding_during_copy(self):
        source, config, raw = fixture(self.root)
        cached = image_store._archive_path(self.store, TAG, PLATFORM, "registry")
        shutil.copyfile(source, cached)
        with tarfile.open(source) as archive:
            members = {member.name: archive.extractfile(member).read() for member in archive}
        config_name = json.loads(members["manifest.json"])[0]["Config"]
        reencoded = json.dumps(config, sort_keys=True).encode()
        self.assertNotEqual(reencoded, raw)
        members[config_name] = reencoded
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as archive:
            for name, content in members.items():
                add_bytes(archive, name, content)
        changed = stream.getvalue()

        def altered_copy(reader, writer, *_args):
            reader.read()
            writer.write(changed)

        output = self.root / "saved.tar"
        with patch.object(image_store.shutil, "copyfileobj", altered_copy):
            with self.assertRaisesRegex(BuildError, "config.*changed"):
                image_cli.save_archive(TAG, output, self.store, source="registry")
        self.assertFalse(output.exists())
        self.assertEqual(cached.read_bytes(), source.read_bytes())

    def _pull_arguments(self, output):
        return [REFERENCE, "-o", str(output), "--image-store", str(self.store),
                "--source", "registry", "--progress", "plain"]

    def test_pull_copy_interrupt_removes_partial_output(self):
        source = make_base(self.root)
        output = self.root / "pulled.tar"

        def interrupted_copy(reader, writer, *_args):
            writer.write(reader.read(512))
            raise KeyboardInterrupt("copy cancelled")

        with patch.object(image_store, "pull_image", return_value=(source, True)), \
                patch.object(image_store.shutil, "copyfileobj", interrupted_copy), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                image_store.pull_main(self._pull_arguments(output))
        self.assertFalse(output.exists())
        self.assertTrue(source.is_file())

    def test_pull_rejects_corrupt_copied_layer_and_allows_retry(self):
        source = make_base(self.root)
        damaged = corrupt_layer(source)
        output = self.root / "pulled.tar"

        def altered_copy(reader, writer, *_args):
            reader.read()
            writer.write(damaged)

        with patch.object(image_store, "pull_image", return_value=(source, True)), \
                patch.object(image_store.shutil, "copyfileobj", altered_copy), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(image_store.pull_main(self._pull_arguments(output)), 1)
        self.assertFalse(output.exists())
        with patch.object(image_store, "pull_image", return_value=(source, True)), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(image_store.pull_main(self._pull_arguments(output)), 0)
        self.assertEqual(output.read_bytes(), source.read_bytes())

    def test_pull_reports_digest_and_size_of_the_delivered_copy(self):
        source = make_base(self.root)
        original = source.read_bytes()
        output = self.root / "pulled.tar"
        reporter = Mock()

        def change_cache(message):
            if message.startswith("Created "):
                source.write_bytes(b"changed after delivery")

        reporter.log.side_effect = change_cache
        with patch.object(image_store, "pull_image", return_value=(source, True)), \
                patch.object(progress, "make_reporter", return_value=reporter):
            self.assertEqual(image_store.pull_main(self._pull_arguments(output)), 0)
        self.assertEqual(output.read_bytes(), original)
        reported = reporter.success.call_args[1]
        self.assertEqual(reported["digest"], "sha256:" + hashlib.sha256(original).hexdigest())
        self.assertEqual(reported["size"], len(original))

    def test_valid_save_preserves_exact_archive_and_raw_config_bytes(self):
        source, _, raw = fixture(self.root)
        cached = image_store._archive_path(self.store, TAG, PLATFORM, "registry")
        shutil.copyfile(source, cached)
        output = self.root / "saved.tar"
        image_cli.save_archive(TAG, output, self.store, source="registry")
        self.assertEqual(output.read_bytes(), source.read_bytes())
        self.assertEqual(sha256_file(output), sha256_file(source))
        with tarfile.open(output) as archive:
            manifest = json.load(archive.extractfile("manifest.json"))
            self.assertEqual(archive.extractfile(manifest[0]["Config"]).read(), raw)
        self.assertFalse((self.store / "cas" / "refs").exists())


if __name__ == "__main__":
    unittest.main()
