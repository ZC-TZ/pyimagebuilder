"""验证缓存过期预览与实际清理后的层回收结果一致。"""

import hashlib
import io
import os
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cache import LayerCache
from cas_store import CASStore
from errors import ArchiveError, BuildError
from image_cli import prune_old_cache


class CacheExpirationTests(unittest.TestCase):
    def test_internal_directory_symlinks_cannot_escape_store(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outside = root / "outside"
            outside.mkdir()
            sentinel = outside / ("a" * 64 + ".json")
            sentinel.write_text("keep", encoding="utf-8")
            cache_root = root / "cache"
            cache_root.mkdir()
            (cache_root / "layers").mkdir()
            try:
                os.symlink(outside, cache_root / "entries", target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                self.skipTest("Directory symlink unavailable: " + str(exc))
            with self.assertRaises(BuildError):
                prune_old_cache(cache_root, 0)
            with self.assertRaises(BuildError):
                LayerCache(cache_root)
            self.assertTrue(sentinel.exists())

            cas_root = root / "store" / "cas"
            (cas_root / "blobs").mkdir(parents=True)
            os.symlink(outside, cas_root / "refs", target_is_directory=True)
            with self.assertRaises(ArchiveError):
                CASStore(root / "store")

    def test_preview_keeps_shared_layer_until_last_reference_expires(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = LayerCache(root / "cache")
            layer = root / "layer.tar"
            with tarfile.open(layer, "w") as archive:
                content = b"payload"
                info = tarfile.TarInfo("payload")
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
            diff_id = "sha256:" + hashlib.sha256(layer.read_bytes()).hexdigest()
            first, second = "a" * 64, "b" * 64
            cache.store(first, diff_id, layer)
            cache.store(second, diff_id, layer)
            old = time.time() - 3 * 86400
            os.utime(cache.entries / (first + ".json"), (old, old))

            preview = prune_old_cache(cache.root, 1, dry_run=True)
            self.assertEqual(len(preview["expired_entries"]), 1)
            self.assertEqual(preview["cleanup"]["orphan_layers"], [])
            self.assertTrue((cache.entries / (first + ".json")).exists())

            os.utime(cache.entries / (second + ".json"), (old, old))
            preview = prune_old_cache(cache.root, 1, dry_run=True)
            self.assertEqual(len(preview["expired_entries"]), 2)
            self.assertEqual(len(preview["cleanup"]["orphan_layers"]), 1)
            self.assertEqual(preview["cleanup"]["orphan_layers"][0]["bytes"], layer.stat().st_size)
            actual = prune_old_cache(cache.root, 1)
            self.assertEqual(actual["reclaimable_bytes"], preview["reclaimable_bytes"])
            self.assertFalse((cache.layers / (diff_id[7:] + ".tar")).exists())


if __name__ == "__main__":
    unittest.main()
