import hashlib
import io
import os
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from builder import build
from errors import BuildError
from image_reader import ImageArchiveReader
from cache import LayerCache
from test_phase1 import make_base


class PhaseFiveTests(unittest.TestCase):
    def test_trusted_base_layer_store_reuses_verified_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = make_base(root)
            store = root / "base-store"
            first = ImageArchiveReader(base, root / "read-1", store).read("example/base:1")
            self.assertGreaterEqual(len(first.layers), 1)
            self.assertEqual(first.layers[0].parent.name, "layers")
            second = ImageArchiveReader(base, root / "read-2", store).read("example/base:1")
            self.assertEqual(second.layers, first.layers)
            self.assertFalse((root / "read-2" / "base-layer-1.tar").exists())
            first.layers[0].write_bytes(b"bad")
            repaired = ImageArchiveReader(base, root / "read-3", store).read("example/base:1")
            self.assertEqual(repaired.layers[0], first.layers[0])
            self.assertNotEqual(repaired.layers[0].read_bytes(), b"bad")

    def test_layer_cache_fast_identity_and_full_verify(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "layer.tar"
            with tarfile.open(source, "w") as archive:
                info = tarfile.TarInfo("file")
                info.size = 1
                archive.addfile(info, io.BytesIO(b"x"))
            digest = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
            cache = LayerCache(root / "cache")
            cache.store("key", digest, source)
            with patch("cache.sha256_file", side_effect=AssertionError("rehash")):
                self.assertIsNotNone(cache.lookup("key"))
            with patch("cache.sha256_file", return_value="0" * 64):
                self.assertIsNone(LayerCache(root / "cache", verify=True).lookup("key"))

    def test_copy_layer_cache_hit_content_change_and_corruption(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = make_base(root)
            context = root / "context"
            context.mkdir()
            payload = context / "payload.txt"
            payload.write_bytes(b"first")
            (context / "Dockerfile").write_text("FROM example/base:1\nCOPY payload.txt /app/data.txt\n")
            cache = root / "cache"

            def create(index):
                stats = {}
                output = root / ("result-{}.tar".format(index))
                build(context / "Dockerfile", context, base, None, "example/cached:1", output,
                      cache_dir=cache, cache_stats=stats)
                with tempfile.TemporaryDirectory() as unpacked:
                    image = ImageArchiveReader(output, Path(unpacked)).read("example/cached:1")
                    with tarfile.open(image.layers[-1]) as archive:
                        content = archive.extractfile("app/data.txt").read()
                return stats, content

            first, data = create(1)
            self.assertEqual((first["hits"], first["misses"]), (0, 1))
            self.assertEqual(data, b"first")
            second, data = create(2)
            self.assertEqual((second["hits"], second["misses"]), (1, 0))
            self.assertEqual(data, b"first")
            timestamp = payload.stat().st_mtime_ns
            payload.write_bytes(b"other")
            os.utime(payload, ns=(timestamp, timestamp))
            third, data = create(3)
            self.assertEqual((third["hits"], third["misses"]), (0, 1))
            self.assertEqual(data, b"other")
            cached_layers = list((cache / "layers").glob("*.tar"))
            self.assertGreaterEqual(len(cached_layers), 2)
            newest = max(cached_layers, key=lambda path: path.stat().st_mtime_ns)
            newest.write_bytes(b"corrupt")
            fourth, data = create(4)
            self.assertEqual((fourth["hits"], fourth["misses"]), (0, 1))
            self.assertEqual(data, b"other")

    def test_run_cache_skips_executor_and_invalidates_after_copy_change(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = make_base(root)
            context = root / "context"
            context.mkdir()
            payload = context / "payload.txt"
            payload.write_bytes(b"first")
            (context / "Dockerfile").write_text(
                "FROM example/base:1\nCOPY payload.txt /app/input.txt\nRUN printf result > /run.txt\n")
            cache = root / "cache"
            calls = []

            class FakeMaterializer:
                def __init__(self, path):
                    self.root = path
                    path.mkdir()

                def apply(self, path):
                    pass

            class FakeOverlay:
                def __init__(self, rootfs, workspace):
                    pass

                def to_layer(self, target):
                    with tarfile.open(target, "w") as archive:
                        content = b"result"
                        info = tarfile.TarInfo("run.txt")
                        info.size = len(content)
                        archive.addfile(info, io.BytesIO(content))
                    return "sha256:" + hashlib.sha256(target.read_bytes()).hexdigest()

            class FakeExecutor:
                def __init__(self, network, sandbox="legacy"):
                    pass

                def execute(self, *args):
                    calls.append("RUN")

            def create(index):
                stats = {}
                output = root / ("run-result-{}.tar".format(index))
                with patch("builder.RootFSMaterializer", FakeMaterializer), \
                     patch("overlay.OverlayManager", FakeOverlay), \
                     patch("executor.RunExecutor", FakeExecutor):
                    build(context / "Dockerfile", context, base, None, "example/run-cache:1",
                          output, enable_run=True, cache_dir=cache, cache_stats=stats)
                return stats

            first = create(1)
            self.assertEqual((first["hits"], first["misses"], len(calls)), (0, 2, 1))
            second = create(2)
            self.assertEqual((second["hits"], second["misses"], len(calls)), (2, 0, 1))
            payload.write_bytes(b"changed")
            third = create(3)
            self.assertEqual((third["hits"], third["misses"], len(calls)), (0, 2, 2))

    def test_cache_directory_inside_context_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = make_base(root)
            context = root / "context"
            context.mkdir()
            (context / "Dockerfile").write_text("FROM example/base:1\n")
            with self.assertRaises(BuildError):
                build(context / "Dockerfile", context, base, None, "example/cache:1",
                      root / "result.tar", cache_dir=context / ".cache")


if __name__ == "__main__":
    unittest.main()
