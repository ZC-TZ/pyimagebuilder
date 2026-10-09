"""验证超大 Docker 元数据在读入 Python 对象前被拒绝。"""

import json
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fast
import image_cli
import image_store
from errors import ArchiveError, BuildError
from image_reader import ImageArchiveReader
from test_phase1 import make_base


class ArchiveJsonLimitTests(unittest.TestCase):
    def test_all_docker_archive_entrypoints_bound_manifest_json(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = make_base(root)
            with patch("image_reader.MAX_JSON", 16):
                with self.assertRaisesRegex(ArchiveError, "Oversized archive JSON"):
                    ImageArchiveReader(archive, root / "unpacked").read("example/base:1")
            with tarfile.open(archive) as source:
                manifest = json.load(source.extractfile("manifest.json"))
                manifest_size = source.getmember("manifest.json").size
                self.assertGreater(source.getmember(manifest[0]["Config"]).size, manifest_size)
            with patch("image_reader.MAX_JSON", manifest_size):
                with self.assertRaisesRegex(ArchiveError, "Oversized image config"):
                    ImageArchiveReader(archive, root / "unpacked").read("example/base:1")
            with patch.object(image_cli, "MAX_JSON", 16):
                with self.assertRaisesRegex(ArchiveError, "oversized manifest"):
                    image_cli._manifest(archive)
            with patch.object(image_store, "MAX_JSON", 16):
                with self.assertRaisesRegex(BuildError, "oversized pulled manifest"):
                    image_store._check_archive_platform(archive, "example/base:1", "linux/amd64")
            with patch.object(image_cli, "MAX_JSON", manifest_size):
                listing = image_cli.list_images(root)
                self.assertIn("oversized image config", listing["images"][0]["error"])
            with patch("image_reader.MAX_JSON", 16):
                with self.assertRaisesRegex(BuildError, "bounded manifest"):
                    fast._require_base_tag(archive, "example/base:1")


if __name__ == "__main__":
    unittest.main()
