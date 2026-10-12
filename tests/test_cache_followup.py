"""验证 Windows 时间戳恢复、拉取清理失败和缓存审计预检的遗漏边界。"""

import json
import os
import shutil
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cache_identity
import cache as cache_module
import builder as builder_module
import image_store
from cache import LayerCache
from cas_store import CASStore
from errors import ArchiveError, BuildError
from image_cli import prune_old_cache
from image_reader import ImageArchiveReader, sha256_file
from image_writer import ImageArchiveWriter
from optimizer import audit_cache, prune_cache
from rootfs import RootFSIndex
from tests.test_phase1 import layer_file, make_base


def overwrite_preserving_times(path, content):
    """修改同大小文件并恢复访问/写入时间，重现备份工具可能产生的状态。"""
    stamp = path.stat()
    if len(content) != stamp.st_size:
        raise AssertionError("Fixture must preserve size")
    path.write_bytes(content)
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))


class CacheFollowupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_restored_mtime_cannot_authorize_changed_instruction_layer(self):
        source = self.root / "layer.tar"
        layer_file(source, {"payload": b"old"})
        cache = LayerCache(self.root / "cache")
        digest = "sha256:" + sha256_file(source)
        cache.store("key", digest, source)
        hit = cache.lookup("key")
        overwrite_preserving_times(hit.path, hit.path.read_bytes().replace(b"old", b"bad"))
        self.assertIsNone(cache.lookup("key"))
        # 正常重建既能修复损坏层，又能恢复下一次命中。
        cache.store("key", digest, source)
        self.assertEqual(sha256_file(cache.lookup("key").path), digest[7:])

    def test_restored_mtime_cannot_authorize_changed_base_archive(self):
        archive = make_base(self.root)
        directory = self.root / "base-cache"
        ImageArchiveReader(archive, self.root / "first", directory).read("example/base:1")
        with tarfile.open(archive) as source:
            offset = source.getmember("one/layer.tar").offset_data
        content = bytearray(archive.read_bytes())
        offset = content.index(b"base", offset)
        content[offset:offset + 4] = b"evil"
        overwrite_preserving_times(archive, content)
        with self.assertRaisesRegex(ArchiveError, "DiffID mismatch"):
            ImageArchiveReader(archive, self.root / "second", directory).read("example/base:1")

    def test_restored_mtime_cannot_authorize_changed_decoded_base_layer(self):
        archive = make_base(self.root)
        directory = self.root / "base-cache"
        image = ImageArchiveReader(archive, self.root / "first", directory).read("example/base:1")
        layer = image.layers[0]
        overwrite_preserving_times(layer, layer.read_bytes().replace(b"base", b"evil"))
        repaired = ImageArchiveReader(archive, self.root / "second", directory).read("example/base:1")
        self.assertEqual(["sha256:" + sha256_file(path) for path in repaired.layers],
                         image.config["rootfs"]["diff_ids"])

    def test_windows_identity_falls_back_to_content_when_api_unavailable(self):
        if os.name != "nt":
            self.skipTest("Windows identity fallback")
        path = self.root / "payload"
        path.write_bytes(b"old")
        with patch.object(cache_identity, "_windows_change_time", return_value=None):
            before = cache_identity.file_identity(path)
            self.assertTrue(before[-1].startswith("sha256:"))
            overwrite_preserving_times(path, b"bad")
            after = cache_identity.file_identity(path)
        self.assertNotEqual(before, after)

    def test_windows_old_identity_is_revalidated_and_upgraded(self):
        if os.name != "nt":
            self.skipTest("Windows legacy identity")
        source = self.root / "layer.tar"
        layer_file(source, {"payload": b"old"})
        cache = LayerCache(self.root / "cache")
        cache.store("key", "sha256:" + sha256_file(source), source)
        entry = cache.entries / "key.json"
        metadata = json.loads(entry.read_text())
        metadata["file_identity"] = metadata["file_identity"][:5]
        entry.write_text(json.dumps(metadata))
        with patch("cache.sha256_file", wraps=sha256_file) as checked:
            self.assertIsNotNone(cache.lookup("key"))
        self.assertEqual(checked.call_count, 1)
        self.assertEqual(len(json.loads(entry.read_text())["file_identity"]), 6)

    def test_upgrade_invalidates_layers_derived_from_a_legacy_false_base_hit(self):
        if os.name != "nt":
            self.skipTest("Windows legacy false hit")
        archive = make_base(self.root)
        cache_dir = self.root / "cache"
        context = self.root / "context"
        context.mkdir()
        recipe = context / "Dockerfile"
        recipe.write_text("FROM example/base:1 AS base\nFROM scratch\n"
                          "COPY --from=base /app/existing.txt /payload\n")
        # 模拟版本 21 已生成的错误后续层：最终阶段是 scratch，最终 tar 自身仍能通过校验。
        old_identity = lambda path: cache_identity.stat_identity(path.stat())
        with patch.object(ImageArchiveReader, "_identity", staticmethod(old_identity)), \
                patch.object(builder_module, "CACHE_VERSION", 21), \
                patch.object(cache_module, "CACHE_VERSION", 21):
            image = ImageArchiveReader(archive, self.root / "warm", cache_dir / "base-layers").read(
                "example/base:1")
            layer = image.layers[0]
            overwrite_preserving_times(layer, layer.read_bytes().replace(b"base", b"evil"))
            wrong = self.root / "legacy.tar"
            builder_module.build(recipe, context, archive, None, "app:1", wrong,
                                 cache_dir=cache_dir, trusted_base_cache=True)
            loaded = ImageArchiveReader(wrong, self.root / "legacy-read").read("app:1")
            index = RootFSIndex()
            for layer in loaded.layers:
                index.apply_layer(layer)
            self.assertEqual(index.read_file("payload"), b"evil")
        # 新身份能修好基础层，但还必须淘汰旧版依据错误 rootfs 生成的指令条目。
        stats = {}
        output = self.root / "fixed.tar"
        builder_module.build(recipe, context, archive, None, "app:1", output,
                             cache_dir=cache_dir, cache_stats=stats, trusted_base_cache=True)
        loaded = ImageArchiveReader(output, self.root / "fixed-read").read("app:1")
        index = RootFSIndex()
        for layer in loaded.layers:
            index.apply_layer(layer)
        self.assertEqual(index.read_file("payload"), b"base")
        self.assertEqual(stats["hits"], 0)

    def test_pull_cleanup_failure_preserves_old_ref_and_archive_and_allows_retry(self):
        directory_class = tempfile.TemporaryDirectory

        class FailedCleanup:
            def __init__(self, *args, **kwargs):
                self.directory = directory_class(*args, **kwargs)
                self.fail = kwargs.get("prefix") == "pull-"

            def __enter__(self):
                return self.directory.__enter__()

            def __exit__(self, *args):
                self.directory.__exit__(*args)
                if self.fail:
                    raise OSError("temporary cleanup failed")

        for materialize in (False, True):
            for existing in (False, True):
                with self.subTest(materialize=materialize, existing=existing):
                    case = self.root / (str(materialize) + str(existing))
                    case.mkdir()
                    old = make_base(case)
                    reference = "example/base:1"
                    store = case / "store"
                    cas = CASStore(store)
                    target = image_store._archive_path(store, reference, "linux/amd64", "registry")
                    if existing:
                        cas.import_docker(old, reference, "linux/amd64", "registry")
                        shutil.copyfile(old, target)
                    before = cas.snapshot_ref(reference, "linux/amd64", "registry")
                    image = ImageArchiveReader(old, case / "read").read(reference)
                    image.config["config"]["Labels"] = {"revision": "new"}
                    new = case / "new.tar"
                    ImageArchiveWriter().write_new(new, image.config, image.layers, reference)

                    def downloader(ref, output, *_args, cas_store=None, cas_only=False, **kwargs):
                        cas_store.import_docker(new, ref, "linux/amd64", "registry", replace=True)
                        if not cas_only:
                            shutil.copyfile(new, output)

                    with patch.object(image_store, "registry_pull", downloader), \
                            patch.object(image_store.tempfile, "TemporaryDirectory", FailedCleanup):
                        with self.assertRaisesRegex(OSError, "cleanup failed"):
                            image_store.pull_image(reference, store=store, refresh=True,
                                                   materialize_tar=materialize)
                    self.assertEqual(cas.snapshot_ref(reference, "linux/amd64", "registry"), before)
                    self.assertEqual(target.exists(), existing)
                    if existing:
                        self.assertEqual(target.read_bytes(), old.read_bytes())
                    self.assertEqual(list(store.glob("pull-publish-*.part")), [])
                    with patch.object(image_store, "registry_pull", downloader):
                        _, downloaded = image_store.pull_image(reference, store=store, refresh=True,
                                                               materialize_tar=materialize)
                    self.assertTrue(downloaded)
                    self.assertNotEqual(cas.snapshot_ref(reference, "linux/amd64", "registry"), before)
                    self.assertEqual(target.exists(), materialize)
                    if materialize:
                        self.assertEqual(target.read_bytes(), new.read_bytes())

    def _unsafe_node(self, kind):
        cache = LayerCache(self.root / kind)
        cache.store("old", None, None)
        stamp = (cache.entries / "old.json").stat().st_mtime - 86400
        os.utime(cache.entries / "old.json", (stamp, stamp))
        if kind == "entry":
            cache.store("unsafe", None, None)
            unsafe = cache.entries / "unsafe.json"
        elif kind == "layer":
            unsafe = cache.layers / ("f" * 64 + ".tar")
            unsafe.write_bytes(b"external layer")
        else:
            unsafe = cache.entries / "unsafe.json"
            unsafe.mkdir()
        return cache, unsafe

    def test_audit_and_prune_precheck_empty_entry_and_orphan_layer_links(self):
        original = Path.is_symlink
        for kind in ("entry", "layer"):
            with self.subTest(kind=kind):
                cache, unsafe = self._unsafe_node(kind)
                # 无符号链接权限的 Windows 也验证入口在读取前拒绝链接节点。
                def linked(path):
                    return path == unsafe or original(path)
                with patch.object(Path, "is_symlink", linked):
                    for operation in (audit_cache, prune_cache):
                        with self.assertRaisesRegex(BuildError, "symlink"):
                            operation(cache.root)
                    self.assertTrue((cache.entries / "old.json").exists())
                self.assertTrue(unsafe.exists())

    def test_expiration_prechecks_unsafe_nodes_before_removing_old_entries(self):
        original = Path.is_symlink
        for kind in ("entry", "layer"):
            with self.subTest(kind=kind):
                cache, unsafe = self._unsafe_node(kind)
                def linked(path):
                    return path == unsafe or original(path)
                with patch.object(Path, "is_symlink", linked):
                    for dry_run in (False, True):
                        with self.assertRaisesRegex(BuildError, "symlink"):
                            prune_old_cache(cache.root, 0, dry_run=dry_run)
                        self.assertTrue((cache.entries / "old.json").exists())

    def test_cache_audit_rejects_non_file_nodes_before_any_deletion(self):
        cache, unsafe = self._unsafe_node("directory")
        for operation in (audit_cache, prune_cache):
            with self.assertRaisesRegex(BuildError, "regular file"):
                operation(cache.root)
        with self.assertRaisesRegex(BuildError, "regular file"):
            prune_old_cache(cache.root, 0)
        self.assertTrue((cache.entries / "old.json").exists())
        self.assertTrue(unsafe.is_dir())


if __name__ == "__main__":
    unittest.main()
