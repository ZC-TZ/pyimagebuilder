"""验证下载截断、缓存归档歧义及 Windows 输出清理的失败路径。"""

import io
import builtins
import contextlib
import http.client
import shutil
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import image_cli
import image_store
import remote_add
from builder import build
from cas_store import CASStore
from errors import BuildError
from image_store import _archive_path, pull_image
from test_phase1 import make_base


class DownloadResponse(io.BytesIO):
    def __init__(self, body, declared):
        super().__init__(body)
        self.headers = {"Content-Length": str(declared)}

    def geturl(self):
        return "https://example.test/app.war"


class FailurePathTests(unittest.TestCase):
    def test_remote_add_lengths_and_transport_errors(self):
        for declared in (None, 4, 3, 5):
            with self.subTest(length=declared), tempfile.TemporaryDirectory() as temporary:
                response = DownloadResponse(b"body", declared)
                if declared is None:
                    response.headers = {}
                with patch.object(remote_add, "urlopen", return_value=response):
                    target = Path(temporary) / "source"
                    if declared in (None, 4):
                        name, digest = remote_add.fetch("https://example.test/app.war", target)
                        self.assertEqual(name, "app.war")
                        self.assertEqual(target.read_bytes(), b"body")
                        self.assertTrue(digest.startswith("sha256:"))
                    else:
                        with self.assertRaisesRegex(BuildError, "length"):
                            remote_add.fetch("https://example.test/app.war", target)
        with tempfile.TemporaryDirectory() as temporary:
            response = DownloadResponse(b"", 5)
            with patch.object(response, "read", side_effect=http.client.IncompleteRead(b"x", 4)), \
                    patch.object(remote_add, "urlopen", return_value=response):
                with self.assertRaisesRegex(BuildError, "Remote ADD failed"):
                    remote_add.fetch("https://example.test/app.war", Path(temporary) / "source")

    def test_truncated_remote_add_does_not_publish_image_or_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = root / "context"
            context.mkdir()
            (context / "Dockerfile").write_text(
                "FROM scratch\nADD https://example.test/app.war /app.war\n")
            output = root / "image.tar"
            cache = root / "cache"
            with patch.object(remote_add, "urlopen", return_value=DownloadResponse(b"half", 10)):
                with self.assertRaisesRegex(BuildError, "length"):
                    build(context / "Dockerfile", context, None, None, "test:1", output,
                          cache_dir=cache)
            self.assertFalse(output.exists())
            self.assertEqual(list(cache.rglob("*.json")), [])

    def test_export_creation_race_preserves_other_writers_file(self):
        for command in ("save", "pull"):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                base = make_base(root)
                output = root / "saved.tar"
                original_open = builtins.open

                def racing_open(path, mode, *args, **kwargs):
                    if Path(path) == base and mode == "rb":
                        output.write_bytes(b"other writer")
                    return original_open(path, mode, *args, **kwargs)

                if command == "save":
                    with patch.object(image_cli, "locate_archive", return_value=("local", base)), \
                            patch.object(image_cli, "open", side_effect=racing_open, create=True):
                        with self.assertRaises(FileExistsError):
                            image_cli.save_archive("example/base:1", output)
                else:
                    with patch.object(image_store, "pull_image", return_value=(base, False)), \
                            patch.object(image_store, "open", side_effect=racing_open, create=True), \
                            patch("progress.make_reporter", return_value=Mock()), \
                            contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(image_store.pull_main([
                            "example/base:1", "-o", str(output), "--image-store", str(root / "store")]), 1)
                self.assertEqual(output.read_bytes(), b"other writer")

    def test_duplicate_cached_tar_members_are_rejected(self):
        for duplicate_name in ("one/layer.tar", "./one/layer.tar"):
            with self.subTest(name=duplicate_name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                base = make_base(root)
                store = root / "store"
                reference = "example/base:1"
                CASStore(store).import_docker(base, reference, "linux/amd64", "registry")
                cached = _archive_path(store, reference, "linux/amd64", "registry")
                shutil.copyfile(base, cached)
                with tarfile.open(cached, "a") as archive:
                    member = tarfile.TarInfo(duplicate_name)
                    raw = (root / "first.tar").read_bytes()
                    member.size = len(raw)
                    archive.addfile(member, io.BytesIO(raw))
                with self.assertRaisesRegex(BuildError, "Duplicate"):
                    pull_image(reference, store=store, offline=True)

    def test_save_copy_error_closes_output_before_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = make_base(root)
            output = root / "saved.tar"

            def interrupted(_source, target, _buffer):
                target.write(b"partial")
                raise OSError("copy interrupted")

            with patch.object(image_cli, "locate_archive", return_value=("local", base)), \
                    patch.object(image_cli.shutil, "copyfileobj", side_effect=interrupted):
                with self.assertRaisesRegex(OSError, "copy interrupted"):
                    image_cli.save_archive("example/base:1", output)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
