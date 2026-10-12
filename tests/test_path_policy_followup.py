"""复查 URL 文件名、镜像库内部 tar 链接及物化路径的逐组件语义。"""

import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import image_cli
import image_store
import remote_add
from errors import ArchiveError, BuildError
from rootfs_materializer import RootFSMaterializer
from tests.test_phase1 import make_base


class Response(io.BytesIO):
    headers = {"Content-Length": "3"}

    def geturl(self):
        return "https://example.test/app.war"


class PathPolicyFollowupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_remote_filename_rejects_decoded_separators_before_download(self):
        for name in ("a%2fb.war", "a%5cb.war", "a\\b.war", "%00app.war",
                     "%2e", "%2e%2e", "%ff", ""):
            with self.subTest(name=name), patch.object(remote_add, "urlopen",
                                                       return_value=Response(b"WAR")) as request:
                output = self.root / "download"
                try:
                    with self.assertRaises(BuildError):
                        remote_add.fetch("https://example.test/files/" + name, output)
                    request.assert_not_called()
                    self.assertFalse(output.exists())
                finally:
                    if output.exists():
                        output.unlink()

    def test_remote_filename_uses_url_semantics_on_every_host(self):
        for encoded, expected in (("app%20one.war", "app one.war"),
                                  ("a:b.war", "a:b.war"),
                                  ("%E4%B8%AD.war", "中.war"),
                                  ("literal%252f.war", "literal%2f.war")):
            with self.subTest(encoded=encoded), patch.object(remote_add, "urlopen",
                                                             return_value=Response(b"WAR")):
                output = self.root / "download"
                name, _ = remote_add.fetch("https://example.test/files/" + encoded, output)
                self.assertEqual(name, expected)
                self.assertEqual(output.read_bytes(), b"WAR")
                output.unlink()

    def test_internal_tar_link_cannot_be_reused_or_imported(self):
        base = make_base(self.root)
        store = self.root / "store"
        store.mkdir()
        reference = "example/base:1"
        original = Path.is_symlink
        for source in ("registry", "local"):
            cached = image_store._archive_path(store, reference, "linux/amd64", source)
            # Windows 无文件 symlink 权限时模拟最终节点判定；数据读取与导入仍运行真实实现。
            operations = [("pull", lambda: image_store.pull_image(reference, store=store, offline=True)),
                          ("locate", lambda: image_store.locate_archive(store, reference, "linux/amd64", source)),
                          ("remove", lambda: image_cli.remove_image(reference, store, source=source))]
            if source == "local":
                operations.append(("load", lambda: image_cli.load_archive(base, store)))
            for name, operation in operations:
                shutil.copyfile(base, cached)
                with self.subTest(source=source, operation=name), \
                        patch.object(Path, "is_symlink", lambda path: path == cached or original(path)):
                    with self.assertRaises(BuildError):
                        operation()
                    self.assertEqual(cached.read_bytes(), base.read_bytes())
            if cached.exists():
                cached.unlink()

    def test_materialized_links_cannot_cancel_missing_or_file_components(self):
        materializer = RootFSMaterializer(self.root / "rootfs")
        (materializer.root / "blocked").write_bytes(b"not a directory")
        victim = materializer.root / "victim"
        victim.mkdir()
        sentinel = victim / "payload"
        sentinel.write_bytes(b"keep")
        link = materializer.root / "link"
        link.write_bytes(b"link placeholder")
        original = Path.is_symlink
        readlink = os.readlink
        for target in ("missing/../victim", "blocked/../victim", "blocked/./../victim",
                       "victim/payload/", "victim/payload/."):
            with self.subTest(target=target), \
                    patch.object(Path, "is_symlink", lambda path: path == link or original(path)), \
                    patch.object(os, "readlink", lambda path: target if path == link else readlink(path)):
                with self.assertRaises(ArchiveError):
                    materializer._real("link", follow_final=True)
                self.assertEqual(sentinel.read_bytes(), b"keep")

    def test_image_listing_reports_internal_tar_link_without_reading_it(self):
        base = make_base(self.root)
        store = self.root / "store"
        image_cli.load_archive(base, store)
        cached = image_store._archive_path(store, "example/base:1", "linux/amd64", "local")
        original = Path.is_symlink
        with patch.object(Path, "is_symlink", lambda path: path == cached or original(path)), \
                patch.object(image_cli, "_manifest", side_effect=AssertionError("linked tar was read")):
            result = image_cli.list_images(store)
        self.assertTrue(result["images"])
        self.assertTrue(all("error" in item for item in result["images"]))

    def test_materialized_valid_relative_parent_and_missing_leaf_survive(self):
        materializer = RootFSMaterializer(self.root / "rootfs")
        (materializer.root / "directory").mkdir()
        (materializer.root / "victim").mkdir()
        link = materializer.root / "link"
        link.write_bytes(b"placeholder")
        original = Path.is_symlink
        for target, expected in (("directory/../victim", materializer.root / "victim"),
                                 ("victim/new", materializer.root / "victim" / "new")):
            with self.subTest(target=target), \
                    patch.object(Path, "is_symlink", lambda path: path == link or original(path)), \
                    patch.object(os, "readlink", return_value=target):
                self.assertEqual(materializer._real("link", follow_final=True), expected)


if __name__ == "__main__":
    unittest.main()
