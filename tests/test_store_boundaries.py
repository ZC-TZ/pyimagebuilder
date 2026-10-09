"""验证跨磁盘缓存、导入竞争及归档路径别名的实际调用边界。"""

import errno
import json
import os
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import image_cli
import image_reader
from builder import build
from cas_store import CASStore
from fast import _require_base_tag
from image_reader import ImageArchiveReader, sha256_file
from image_store import _archive_path, pull_image
from test_phase1 import add_bytes, make_base


class StoreBoundaryTests(unittest.TestCase):
    def test_base_cache_publication_across_filesystems_and_reuse(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = make_base(root)
            workspace, cache = root / "workspace", root / "cache"
            original_replace = os.replace

            def cross_device(source, destination):
                if Path(source).parent == workspace and Path(destination).parent == cache / "layers":
                    raise OSError(errno.EXDEV, "cross-device link")
                return original_replace(source, destination)

            with patch.object(image_reader.os, "replace", side_effect=cross_device):
                first = ImageArchiveReader(archive, workspace, cache).read("example/base:1")
            self.assertEqual(["sha256:" + sha256_file(path) for path in first.layers],
                             first.config["rootfs"]["diff_ids"])
            self.assertEqual(list(workspace.glob("*.tar")), [])
            self.assertEqual(list(cache.rglob("*.part")), [])
            with patch.object(image_reader, "sha256_file", side_effect=AssertionError("cache miss")):
                second = ImageArchiveReader(archive, root / "second-workspace", cache).read("example/base:1")
            self.assertEqual(second.layers, first.layers)
            self.assertEqual(list((root / "second-workspace").glob("*.tar")), [])

    def test_cross_device_copy_failure_keeps_workspace_and_cleans_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = make_base(root)
            reader = ImageArchiveReader(archive, root / "workspace", root / "cache")
            original_replace = os.replace

            def cross_device(source, destination):
                if Path(source).parent == reader.workspace:
                    raise OSError(errno.EXDEV, "cross-device link")
                return original_replace(source, destination)

            def interrupted(_source, output, _length):
                output.write(b"partial")
                raise OSError("cache copy interrupted")

            with patch.object(image_reader.os, "replace", side_effect=cross_device), \
                    patch("shutil.copyfileobj", side_effect=interrupted):
                with self.assertRaisesRegex(OSError, "cache copy interrupted"):
                    reader.read("example/base:1")
            self.assertTrue((reader.workspace / "base-layer-1.tar").is_file())
            self.assertEqual(list((root / "cache").rglob("*.part")), [])
            self.assertEqual(list((root / "cache").rglob("*.json")), [])
            self.assertEqual(list((root / "cache").rglob("*.tar")), [])

    def test_base_cache_permission_error_is_not_treated_as_cross_device(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = make_base(root)
            reader = ImageArchiveReader(archive, root / "workspace", root / "cache")
            with patch.object(image_reader.os, "replace", side_effect=OSError(errno.EACCES, "denied")), \
                    patch.object(image_reader.shutil, "copyfileobj") as copy:
                with self.assertRaisesRegex(OSError, "denied"):
                    reader.read("example/base:1")
                copy.assert_not_called()
            self.assertEqual(list((root / "cache").rglob("*.json")), [])

    def test_load_creation_race_preserves_other_import_and_ref(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = make_base(root)
            store = root / "store"
            reference = "example/base:1"
            target = _archive_path(store, reference, "linux/amd64", "local")
            pending = target.with_name(target.name + ".part")
            original_open = open
            published = []

            def competing_open(path, mode, *args, **kwargs):
                if Path(path) == pending and mode == "xb":
                    pending.write_bytes(b"other import")
                    cas = CASStore(store)
                    cas.import_docker(archive, reference, "linux/amd64")
                    published.append(cas.snapshot_ref(reference, "linux/amd64", "local"))
                return original_open(path, mode, *args, **kwargs)

            with patch.object(image_cli, "open", side_effect=competing_open, create=True):
                with self.assertRaises(FileExistsError):
                    image_cli.load_archive(archive, store)
            self.assertTrue(pending.exists(), "competing import file was removed")
            self.assertEqual(pending.read_bytes(), b"other import")
            self.assertEqual(CASStore(store).snapshot_ref(reference, "linux/amd64", "local"), published[0])

    def test_load_publish_failure_restores_previous_ref(self):
        for existing in (False, True):
            with self.subTest(existing=existing), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                archive = make_base(root)
                store = root / "store"
                target = _archive_path(store, "example/base:1", "linux/amd64", "local")
                if existing:
                    context = root / "context"
                    context.mkdir()
                    (context / "payload").write_bytes(b"old image")
                    (context / "Dockerfile").write_text("FROM scratch\nCOPY payload /payload\n")
                    old = root / "old.tar"
                    build(context / "Dockerfile", context, None, None, "example/base:1", old)
                    image_cli.load_archive(old, store)
                previous_ref = CASStore(store).snapshot_ref("example/base:1", "linux/amd64", "local")
                previous_digest = sha256_file(target) if existing else None
                original_replace = os.replace

                def interrupted(source, destination):
                    if Path(destination) == target:
                        raise OSError("archive publication interrupted")
                    return original_replace(source, destination)

                with patch.object(image_cli.os, "replace", side_effect=interrupted):
                    with self.assertRaisesRegex(OSError, "publication interrupted"):
                        image_cli.load_archive(archive, store, replace=True)
                self.assertEqual(CASStore(store).snapshot_ref("example/base:1", "linux/amd64", "local"),
                                 previous_ref)
                if existing:
                    self.assertEqual(sha256_file(target), previous_digest)
                else:
                    self.assertFalse(target.exists())
                self.assertFalse(target.with_name(target.name + ".part").exists())

    def test_prefixed_archive_works_across_inspect_load_save_tag_and_build(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = make_base(root)
            prefixed = root / "prefixed.tar"
            with tarfile.open(archive) as source, tarfile.open(prefixed, "w") as output:
                for member in source:
                    stream = source.extractfile(member)
                    member.name = "./" + member.name
                    output.addfile(member, stream)
                    if stream is not None:
                        stream.close()
            self.assertEqual(image_cli.inspect_archive(prefixed)["selected_tag"], "example/base:1")
            _require_base_tag(prefixed, "example/base:1")
            store = root / "store"
            image_cli.load_archive(prefixed, store)
            listing = image_cli.list_images(store)
            self.assertTrue(all("error" not in item for item in listing["images"]))
            saved = root / "saved.tar"
            image_cli.save_archive("example/base:1", saved, store)
            self.assertEqual(image_cli.verify_archive(saved)["layers_verified"], 2)
            retagged = root / "retagged.tar"
            image_cli.tag_archive(prefixed, "example/renamed:2", retagged)
            self.assertEqual(image_cli.inspect_archive(retagged)["selected_tag"], "example/renamed:2")
            context = root / "context"
            context.mkdir()
            (context / "app.war").write_bytes(b"WAR")
            (context / "Dockerfile").write_text("FROM example/base:1\nCOPY app.war /app.war\n")
            output = root / "built.tar"
            base, downloaded = pull_image("example/base:1", store=store,
                                          materialize_tar=False, offline=True)
            self.assertFalse(downloaded)
            build(context / "Dockerfile", context, base, None, "example/result:1", output,
                  cache_dir=root / "build-cache")
            self.assertEqual(image_cli.verify_archive(output)["layers_verified"], 3)

    def test_images_reports_bad_config_and_continues_to_valid_images(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = root / "store"
            store.mkdir()
            make_base(root).rename(store / "base.tar")
            for value in ([], None, "bad", 5):
                path = store / ("invalid-{}.tar".format(type(value).__name__))
                with tarfile.open(path, "w") as archive:
                    add_bytes(archive, "manifest.json", json.dumps([
                        {"Config": "config.json", "RepoTags": ["bad:1"], "Layers": []}]).encode())
                    add_bytes(archive, "config.json", json.dumps(value).encode())
            listing = image_cli.list_images(store)
            self.assertEqual(len(listing["images"]), 5)
            self.assertEqual(sum("error" in item for item in listing["images"]), 4)
            self.assertTrue(any(item.get("tags") == ["example/base:1"] for item in listing["images"]))


if __name__ == "__main__":
    unittest.main()
