"""检查上线前收敛的配置契约：明确报错，不隐式覆盖或忽略设置。"""

import copy
import hashlib
import json
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from errors import BuildError
from image_reader import BaseImage
from image_writer import ImageArchiveWriter
from oci_writer import OCIImageWriter
from settings import load_settings
import fast


class ConfigurationContractTests(unittest.TestCase):
    def read(self, value, raw=False):
        """通过真实配置文件验证所有入口复用的加载器。"""
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            path.write_text(value if raw else json.dumps(value), encoding="utf-8")
            return load_settings(path)[0]

    def test_duplicate_keys_are_rejected_at_every_level(self):
        examples = [
            '{"schemaVersion":2,"schemaVersion":2}',
            '{"schemaVersion":2,"cache":{"imageStore":"a","imageStore":"b"}}',
            '{"schemaVersion":2,"repositories":{"example.test":{"workers":1,"workers":2}}}',
            '{"schemaVersion":2,"fast":{"profiles":{"p":{},"p":{}}}}',
        ]
        for raw in examples:
            with self.subTest(raw=raw), self.assertRaisesRegex(BuildError, "Duplicate"):
                self.read(raw, raw=True)

    def test_nonstandard_numbers_are_rejected(self):
        for number in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(number=number), self.assertRaisesRegex(BuildError, "Non-JSON"):
                self.read('{"schemaVersion":2,"fast":{"profiles":{"p":{"owner":' + number + '}}}}', raw=True)

    def test_schema_version_requires_integer(self):
        for version in (2.0, True, "2", None):
            with self.subTest(version=version), self.assertRaisesRegex(BuildError, "schemaVersion"):
                self.read({"schemaVersion": version})

    def test_unknown_fields_are_rejected_with_location(self):
        examples = [
            ({"schemaVersion": 2, "cahce": {}}, "config"),
            ({"schemaVersion": 2, "cache": {"imageStroe": "x"}}, "cache"),
            ({"schemaVersion": 2, "repositories": {"example.test": {"worker": 2}}}, "repositories.example.test"),
            ({"schemaVersion": 2, "fast": {"defaultProflie": "p"}}, "fast"),
            ({"schemaVersion": 2, "fast": {"profiles": {"p": {"baseURL": "x"}}}}, "fast.profiles.p"),
        ]
        for value, location in examples:
            with self.subTest(location=location), self.assertRaises(BuildError) as raised:
                self.read(value)
            self.assertIn(location, str(raised.exception))

    def test_repository_text_fields_are_typed(self):
        for field in ("username", "passwordEnv", "authHost"):
            for value in (123, False, [], ""):
                with self.subTest(field=field, value=value), self.assertRaises(BuildError):
                    self.read({"schemaVersion": 2, "repositories": {"example.test": {field: value}}})

    def test_unselected_profile_has_same_structural_contract(self):
        for field in ("baseTar", "baseUrl", "serverConfig", "outputDir"):
            with self.subTest(field=field), self.assertRaises(BuildError):
                self.read({"schemaVersion": 2, "fast": {"profiles": {"p": {field: []}}}})

    def test_default_profile_must_exist(self):
        with self.assertRaisesRegex(BuildError, "existing profile"):
            self.read({"schemaVersion": 2, "fast": {"defaultProfile": "missing"}})

    def test_nullable_optional_profile_fields_remain_supported(self):
        value = {"schemaVersion": 2, "fast": {"defaultProfile": "p", "profiles": {
            "p": {"serverConfig": None, "outputDir": None}}}}
        self.assertIsNone(self.read(value)["fast"]["profiles"]["p"]["serverConfig"])

    def test_bom_config_can_be_extended_by_fast_init(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "config.json"
            config.write_text(json.dumps({"schemaVersion": 2}), encoding="utf-8-sig")
            base = root / "base.tar"
            ImageArchiveWriter().write_new(base, {"os": "linux", "architecture": "amd64",
                "rootfs": {"type": "layers", "diff_ids": []}}, [], "example/base:1")
            self.assertEqual(fast.main(["init", "--config", str(config), "--profile", "p",
                                       "--flavor", "tomcat", "--base-tar", str(base),
                                       "--base-image", "example/base:1"]), 0)
            self.assertEqual(load_settings(config)[0]["fast"]["defaultProfile"], "p")


class WriterPublicContractTests(unittest.TestCase):
    def test_ambiguous_write_api_is_removed(self):
        for writer in (ImageArchiveWriter(), OCIImageWriter()):
            self.assertFalse(hasattr(writer, "write"))

    def test_new_image_api_cannot_accept_transfer_bytes(self):
        for writer in (ImageArchiveWriter(), OCIImageWriter()):
            with tempfile.TemporaryDirectory() as temp:
                output = Path(temp) / "image.tar"
                with self.assertRaises(TypeError):
                    writer.write_new(output, {}, [], "example/app:1", config_raw=b"{}")
                self.assertFalse(output.exists())

    def test_transfer_api_preserves_original_config_and_digest(self):
        config = {"os": "linux", "architecture": "amd64",
                  "rootfs": {"type": "layers", "diff_ids": []}}
        raw = (json.dumps(config, indent=3) + "\n").encode()
        digest = hashlib.sha256(raw).hexdigest()
        image = BaseImage(copy.deepcopy(config), [], ["example/app:1"], {},
                          config_digest="sha256:" + digest, config_raw=raw)
        for writer in (ImageArchiveWriter(), OCIImageWriter()):
            with tempfile.TemporaryDirectory() as temp:
                output = Path(temp) / "image.tar"
                writer.write_image(output, image, "example/app:1")
                writer.verify(output, "example/app:1")
                name = digest + ".json" if isinstance(writer, ImageArchiveWriter) else "blobs/sha256/" + digest
                with tarfile.open(output) as archive:
                    self.assertEqual(archive.extractfile(name).read(), raw)


if __name__ == "__main__":
    unittest.main()
