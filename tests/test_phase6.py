import json
import hashlib
import io
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from builder import build
from dockerfile_parser import parse
from errors import BuildError
from image_reader import ImageArchiveReader
from rootfs import RootFSIndex
from test_phase1 import make_base


class PhaseSixTests(unittest.TestCase):
    def inspect(self, archive, tag):
        with tempfile.TemporaryDirectory() as temporary:
            image = ImageArchiveReader(archive, Path(temporary)).read(tag)
            index = RootFSIndex()
            for layer in image.layers:
                index.apply_layer(layer)
            return image.config, {path: index.read_file(path, 1000000)
                                  for path, entry in index.entries.items() if entry.kind == "file"}

    def test_named_stage_copy_to_scratch_and_cache_invalidation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = root / "context"
            context.mkdir()
            source = context / "artifact.war"
            source.write_bytes(b"v1")
            dockerfile = context / "Dockerfile"
            dockerfile.write_text("FROM scratch AS build\n"
                                  "ENV BUILD_ONLY=yes\n"
                                  "COPY artifact.war /out/app.war\n"
                                  "FROM scratch AS runtime\n"
                                  "COPY --from=build /out/app.war /app/app.war\n"
                                  "CMD [\"/app/app.war\"]\n")
            cache = root / "cache"
            stats = {}
            first = root / "first.tar"
            build(dockerfile, context, None, None, "example/multi:1", first,
                  cache_dir=cache, cache_stats=stats)
            config, files = self.inspect(first, "example/multi:1")
            self.assertEqual(files["app/app.war"], b"v1")
            self.assertNotIn("out/app.war", files)
            self.assertNotIn("BUILD_ONLY=yes", config["config"].get("Env", []))
            self.assertEqual(len(config["rootfs"]["diff_ids"]), 1)
            self.assertEqual((stats["hits"], stats["misses"]), (0, 2))

            second = root / "second.tar"
            build(dockerfile, context, None, None, "example/multi:1", second,
                  cache_dir=cache, cache_stats=stats)
            self.assertEqual((stats["hits"], stats["misses"]), (2, 0))
            source.write_bytes(b"v2")
            third = root / "third.tar"
            build(dockerfile, context, None, None, "example/multi:1", third,
                  cache_dir=cache, cache_stats=stats)
            self.assertEqual((stats["hits"], stats["misses"]), (0, 2))
            self.assertEqual(self.inspect(third, "example/multi:1")[1]["app/app.war"], b"v2")

    def test_numeric_stage_directory_copy_and_from_prior_stage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = root / "context"
            (context / "dist" / "nested").mkdir(parents=True)
            (context / "dist" / "index.html").write_bytes(b"hello")
            (context / "dist" / "nested" / "app.js").write_bytes(b"js")
            dockerfile = context / "Dockerfile"
            dockerfile.write_text("FROM scratch AS assets\nCOPY dist/ /built/\n"
                                  "FROM assets AS inherited\n"
                                  "COPY --from=0 /built/ /served/\n"
                                  "COPY --from=0 /built/*.html /html/\n")
            output = root / "image.tar"
            build(dockerfile, context, None, None, "example/inherited:1", output)
            config, files = self.inspect(output, "example/inherited:1")
            self.assertEqual(files["built/index.html"], b"hello")
            self.assertEqual(files["served/index.html"], b"hello")
            self.assertEqual(files["served/nested/app.js"], b"js")
            self.assertEqual(files["html/index.html"], b"hello")
            self.assertEqual(len(config["rootfs"]["diff_ids"]), 3)

    def test_multiple_external_bases_and_target_skip(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = make_base(root)
            mapping = root / "bases.json"
            mapping.write_text(json.dumps({"example/build:1": str(base),
                                           "example/runtime:1": str(base)}))
            context = root / "context"
            context.mkdir()
            dockerfile = context / "Dockerfile"
            dockerfile.write_text("FROM example/build:1 AS compile\n"
                                  "WORKDIR /app\n"
                                  "FROM example/runtime:1 AS runtime\n"
                                  "COPY --from=compile /etc/new.conf /app/final.conf\n"
                                  "FROM scratch AS unused\nRUN false\n")
            output = root / "target.tar"
            build(dockerfile, context, None, mapping, "example/target:1", output,
                  target_stage="runtime")
            config, files = self.inspect(output, "example/target:1")
            self.assertEqual(files["app/final.conf"], b"new")
            self.assertEqual(len(config["rootfs"]["diff_ids"]), 3)

    def test_invalid_stage_references_fail(self):
        with self.assertRaises(BuildError):
            parse("FROM scratch AS build\nFROM scratch AS build\n")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = root / "context"
            context.mkdir()
            dockerfile = context / "Dockerfile"
            dockerfile.write_text("FROM scratch\nCOPY --from=later /a /a\n"
                                  "FROM scratch AS later\n")
            with self.assertRaises(BuildError):
                build(dockerfile, context, None, None, "example/invalid:1", root / "invalid.tar")
            self.assertFalse((root / "invalid.tar").exists())

    def test_run_output_can_be_copied_from_prior_stage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = root / "context"
            context.mkdir()
            dockerfile = context / "Dockerfile"
            dockerfile.write_text("FROM scratch AS compile\nRUN generate-artifact\n"
                                  "FROM scratch\nCOPY --from=compile /work/generated.txt /app/output.txt\n")
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
                        data = b"generated"
                        info = tarfile.TarInfo("work/generated.txt")
                        info.size = len(data)
                        archive.addfile(info, io.BytesIO(data))
                    return "sha256:" + hashlib.sha256(target.read_bytes()).hexdigest()

            class FakeExecutor:
                def __init__(self, network, sandbox="legacy"):
                    pass

                def execute(self, *args):
                    calls.append("RUN")

            with patch("builder.RootFSMaterializer", FakeMaterializer), \
                 patch("overlay.OverlayManager", FakeOverlay), \
                 patch("executor.RunExecutor", FakeExecutor):
                output = root / "run-stage.tar"
                build(dockerfile, context, None, None, "example/run-stage:1", output,
                      enable_run=True)
            self.assertEqual(calls, ["RUN"])
            self.assertEqual(self.inspect(output, "example/run-stage:1")[1]["app/output.txt"],
                             b"generated")


if __name__ == "__main__":
    unittest.main()
