import os
import io
import contextlib
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from builder import build
import main
from errors import BuildError
from image_reader import ImageArchiveReader, sha256_file
from overlay import OverlayManager
from test_phase1 import make_base


class PhaseEightTests(unittest.TestCase):
    def test_cli_environment_epoch_and_explicit_override(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = root / "context"
            context.mkdir()
            dockerfile = context / "Dockerfile"
            dockerfile.write_text("FROM scratch\nENV MODE=fixed\n")
            common = ["--dockerfile", str(dockerfile), "--context", str(context),
                      "--tag", "example/cli:8", "--no-cache"]
            inherited = root / "inherited.tar"
            explicit = root / "explicit.tar"
            with patch.dict(os.environ, {"SOURCE_DATE_EPOCH": "123"}), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main.main(common + ["--output", str(inherited)]), 0)
                self.assertEqual(main.main(common + ["--output", str(explicit),
                                                    "--source-date-epoch", "42"]), 0)
            with tempfile.TemporaryDirectory() as unpacked:
                first = ImageArchiveReader(inherited, Path(unpacked) / "first").read("example/cli:8")
                second = ImageArchiveReader(explicit, Path(unpacked) / "second").read("example/cli:8")
                self.assertEqual(first.config["created"], "1970-01-01T00:02:03Z")
                self.assertEqual(second.config["created"], "1970-01-01T00:00:42Z")

    def test_same_inputs_same_docker_and_oci_bytes_with_and_without_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = make_base(root)
            context = root / "context"
            context.mkdir()
            source = context / "payload.txt"
            source.write_bytes(b"fixed payload")
            dockerfile = context / "Dockerfile"
            dockerfile.write_text("FROM example/base:1\n"
                                  "WORKDIR /app\nCOPY payload.txt /app/payload.txt\n"
                                  "ENV MODE=fixed\n")
            cache = root / "cache"

            def produce(number, cache_dir):
                output = root / ("image-{}.tar".format(number))
                stats = {}
                build(dockerfile, context, base, None, "example/repro:8", output,
                      cache_dir=cache_dir, cache_stats=stats, image_format="both")
                oci = root / ("image-{}.oci.tar".format(number))
                return (sha256_file(output), sha256_file(oci)), stats

            first, _ = produce(1, None)
            second, stats = produce(2, cache)
            self.assertGreater(stats["misses"], 0)
            third, stats = produce(3, cache)
            self.assertGreater(stats["hits"], 0)
            self.assertEqual(first, second)
            self.assertEqual(second, third)
            old_mtime = source.stat().st_mtime_ns
            os.utime(source, ns=(old_mtime + 5_000_000_000,
                                 old_mtime + 5_000_000_000))
            fourth, _ = produce(4, cache)
            self.assertEqual(first, fourth)

    def test_epoch_changes_output_and_cache_key(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = root / "context"
            context.mkdir()
            (context / "data").write_bytes(b"constant")
            dockerfile = context / "Dockerfile"
            dockerfile.write_text("FROM scratch\nCOPY data /data\n")
            cache = root / "cache"
            first = root / "epoch-zero.tar"
            later = root / "epoch-later.tar"
            stats = {}
            build(dockerfile, context, None, None, "example/epoch:8", first,
                  cache_dir=cache, source_date_epoch=0)
            build(dockerfile, context, None, None, "example/epoch:8", later,
                  cache_dir=cache, cache_stats=stats, source_date_epoch=42)
            self.assertEqual((stats["hits"], stats["misses"]), (0, 1))
            self.assertNotEqual(sha256_file(first), sha256_file(later))
            with tarfile.open(later, "r:") as archive:
                self.assertTrue(all(member.mtime == 42 for member in archive))
                layer_name = next(name for name in archive.getnames() if name.endswith("/layer.tar"))
                layer = archive.extractfile(layer_name).read()
            layer_path = root / "later-layer.tar"
            layer_path.write_bytes(layer)
            with tarfile.open(layer_path, "r:") as archive:
                self.assertEqual(archive.getmember("data").mtime, 42)
            with self.assertRaises(BuildError):
                build(dockerfile, context, None, None, "example/epoch:8",
                      root / "invalid.tar", source_date_epoch="yesterday")

    def test_overlay_serialization_ignores_host_file_mtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lower = root / "lower"
            lower.mkdir()
            manager = OverlayManager(lower, root / "overlay")
            manager.source_date_epoch = 86_400
            payload = manager.upper / "generated.txt"
            payload.write_bytes(b"same")
            first = root / "first.tar"
            second = root / "second.tar"
            with patch("overlay._overlay_value", return_value=None), \
                 patch("overlay.os.listxattr", return_value=[], create=True):
                os.utime(payload, (1000, 1000))
                manager.to_layer(first)
                os.utime(payload, (2000, 2000))
                manager.to_layer(second)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            with tarfile.open(first, "r:") as archive:
                self.assertEqual(archive.getmember("generated.txt").mtime, 86_400)


if __name__ == "__main__":
    unittest.main()
