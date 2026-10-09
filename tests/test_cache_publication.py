"""验证缓存发布时复制后的字节，以及升级前已产生的错误可信条目。"""

import errno
import io
import json
import os
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cache as cache_module
import image_reader
from builder import build
from cache import LayerCache
from errors import ArchiveError, BuildError
from image_reader import ImageArchiveReader, sha256_file
from rootfs import RootFSIndex
from tests.test_phase1 import make_base


def make_layer(path, payload):
    with tarfile.open(path, "w") as archive:
        member = tarfile.TarInfo("payload")
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))
    return "sha256:" + sha256_file(path)


class CachePublicationTests(unittest.TestCase):
    def test_instruction_copy_rejects_source_changed_after_initial_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, changed = root / "source.tar", root / "changed.tar"
            digest = make_layer(source, b"old")
            make_layer(changed, b"new")
            layer_cache = LayerCache(root / "cache")
            copy = cache_module.shutil.copyfileobj

            def change_then_copy(stream, output, length):
                source.write_bytes(changed.read_bytes())
                return copy(stream, output, length)

            with patch.object(cache_module.shutil, "copyfileobj", change_then_copy):
                with self.assertRaisesRegex(BuildError, "changed|mismatch"):
                    layer_cache.store("key", digest, source)
            self.assertIsNone(layer_cache.lookup("key"))
            self.assertEqual(list(layer_cache.layers.iterdir()), [])
            self.assertEqual(list(layer_cache.entries.iterdir()), [])

    def test_failed_replacement_preserves_previous_instruction_entry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old, attempted, changed = [root / (name + ".tar") for name in ("old", "attempted", "changed")]
            old_digest = make_layer(old, b"one")
            attempted_digest = make_layer(attempted, b"two")
            make_layer(changed, b"bad")
            layer_cache = LayerCache(root / "cache")
            layer_cache.store("key", old_digest, old)
            previous = (layer_cache.entries / "key.json").read_bytes()
            copy = cache_module.shutil.copyfileobj

            def change_then_copy(stream, output, length):
                attempted.write_bytes(changed.read_bytes())
                return copy(stream, output, length)

            with patch.object(cache_module.shutil, "copyfileobj", change_then_copy):
                with self.assertRaises(BuildError):
                    layer_cache.store("key", attempted_digest, attempted)
            self.assertEqual((layer_cache.entries / "key.json").read_bytes(), previous)
            hit = layer_cache.lookup("key")
            self.assertEqual(hit.diff_id, old_digest)
            self.assertEqual("sha256:" + sha256_file(hit.path), old_digest)
            self.assertEqual(len(list(layer_cache.layers.iterdir())), 1)

    def test_cross_device_base_copy_rejects_changed_bytes_without_publishing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = make_base(root)
            changed = root / "changed.tar"
            make_layer(changed, b"different valid tar")
            workspace, store = root / "workspace", root / "base-cache"
            replace, copy = os.replace, image_reader.shutil.copyfileobj

            def cross_device(source, destination):
                if Path(source).parent == workspace and Path(destination).parent == store / "layers":
                    raise OSError(errno.EXDEV, "cross-device link")
                return replace(source, destination)

            def change_then_copy(stream, output, length):
                Path(stream.name).write_bytes(changed.read_bytes())
                return copy(stream, output, length)

            with patch.object(image_reader.os, "replace", cross_device), \
                    patch.object(image_reader.shutil, "copyfileobj", change_then_copy):
                with self.assertRaisesRegex(ArchiveError, "changed|mismatch"):
                    ImageArchiveReader(archive, workspace, store).read("example/base:1")
            self.assertTrue((workspace / "base-layer-1.tar").is_file())
            self.assertEqual(list(store.rglob("*.tar")), [])
            self.assertEqual(list(store.rglob("*.json")), [])
            self.assertEqual(list(store.rglob("*.part")), [])

    def test_legacy_instruction_identity_cannot_authorize_wrong_layer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.tar"
            digest = make_layer(source, b"old")
            layer_cache = LayerCache(root / "cache")
            target = layer_cache.layers / (digest.split(":")[1] + ".tar")
            make_layer(target, b"new")
            # 重现旧发布器可能写出的状态：内容错误，但文件身份匹配。
            metadata = {"version": 6, "key": "key", "empty": False,
                        "diff_id": digest, "size": target.stat().st_size,
                        "file_identity": layer_cache._identity(target)}
            (layer_cache.entries / "key.json").write_text(json.dumps(metadata), encoding="utf-8")
            self.assertIsNone(layer_cache.lookup("key"))

    def test_legacy_base_identity_is_revalidated_and_repaired(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = make_base(root)
            store = root / "base-cache"
            reader = ImageArchiveReader(archive, root / "first-read", store)
            first = reader.read("example/base:1")
            with tarfile.open(archive) as source:
                manifest = json.load(source.extractfile("manifest.json"))[0]
            _, location = reader._cached_layer(manifest["Layers"][0], first.config["rootfs"]["diff_ids"][0])
            layer, metadata_path = location
            make_layer(layer, b"wrong cached base")
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata.pop("version", None)
            metadata["layer"] = reader._identity(layer)
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
            repaired = ImageArchiveReader(archive, root / "next-read", store).read("example/base:1")
            self.assertEqual(["sha256:" + sha256_file(path) for path in repaired.layers],
                             repaired.config["rootfs"]["diff_ids"])

    def test_failed_cache_copy_does_not_poison_next_war_build(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = root / "context"
            context.mkdir()
            (context / "app.war").write_bytes(b"original WAR")
            dockerfile = context / "Dockerfile"
            dockerfile.write_text("FROM scratch\nCOPY app.war /app/app.war\n", encoding="utf-8")
            changed = root / "changed.tar"
            make_layer(changed, b"wrong copied bytes")
            cache_root = root / "cache"
            copy = cache_module.shutil.copyfileobj

            def change_then_copy(stream, output, length):
                Path(stream.name).write_bytes(changed.read_bytes())
                return copy(stream, output, length)

            failed = root / "failed.tar"
            with patch.object(cache_module.shutil, "copyfileobj", change_then_copy):
                with self.assertRaises(BuildError):
                    build(dockerfile, context, None, None, "app:1", failed, cache_dir=cache_root)
            self.assertFalse(failed.exists())
            for number in range(2):
                stats = {}
                output = root / ("success-{}.tar".format(number))
                build(dockerfile, context, None, None, "app:1", output,
                      cache_dir=cache_root, cache_stats=stats)
                self.assertEqual((stats["hits"], stats["misses"]), (number, 1 - number))
                image = ImageArchiveReader(output, root / ("read-{}".format(number))).read("app:1")
                index = RootFSIndex()
                for layer in image.layers:
                    index.apply_layer(layer)
                self.assertEqual(index.read_file("app/app.war"), b"original WAR")


if __name__ == "__main__":
    unittest.main()
