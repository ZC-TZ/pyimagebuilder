"""验证跨阶段 RUN 的文件系统隔离、显式继承，以及旧污染缓存的迁移。"""

import io
import json
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from builder import build
from cas_store import CASStore
from image_reader import ImageArchiveReader, sha256_file
from image_writer import ImageArchiveWriter
from rootfs import RootFSIndex
from rootfs_materializer import RootFSMaterializer


TAG = "example/app:1"


class RunStageIsolationTests(unittest.TestCase):
    def fixture(self, root, parent="scratch", legacy=False):
        context = root / "context"
        context.mkdir(exist_ok=True)
        if not (context / "private.war").exists():
            (context / "private.war").write_bytes(b"compile-private")
        dockerfile = context / "Dockerfile"
        dockerfile.write_text(
            "FROM scratch AS compile\n"
            "COPY private.war /private/compile.war\n"
            "RUN generate\n"
            "FROM " + parent + " AS runtime\n"
            "RUN inspect\n", encoding="utf-8")
        calls = []
        outer = self

        class TestOverlay:
            def __init__(self, lower, workspace, rootless=False):
                self.lower = Path(lower)
                self.entries = {}

            def to_layer(self, target):
                with tarfile.open(target, "w") as archive:
                    for name, value in sorted(self.entries.items()):
                        info = tarfile.TarInfo(name)
                        info.size = len(value)
                        archive.addfile(info, io.BytesIO(value))
                return "sha256:" + sha256_file(target)

        class TestExecutor:
            def __init__(self, *args):
                pass

            def execute(self, overlay, command, *args):
                text = command.value
                calls.append(text)
                if text == "generate":
                    outer.assertEqual((overlay.lower / "private/compile.war").read_bytes(),
                                      b"compile-private")
                    overlay.entries["generated.war"] = b"generated"
                elif text == "inspect":
                    seen = sorted(path.relative_to(overlay.lower).as_posix()
                                  for path in overlay.lower.rglob("*"))
                    overlay.entries["seen.json"] = json.dumps(seen).encode("utf-8")
                else:
                    raise AssertionError("unexpected synthetic RUN: " + text)

        def materializer(path, rootless=False):
            # 旧实现始终物化到同一目录；仅用于生成可命中的旧污染缓存。
            selected = Path(path).parent / "legacy-shared" if legacy else path
            return RootFSMaterializer(selected, rootless=True)

        return context, dockerfile, calls, TestOverlay, TestExecutor, materializer

    def run_build(self, root, parent="scratch", cached=False, legacy=False,
                  output_name="image", base=None):
        context, dockerfile, calls, overlay, executor, materializer = self.fixture(
            root, parent, legacy)
        output = root / (output_name + ".tar")
        stats = {}
        # 使用真实目录、真实层应用和最终归档；只替代 Linux mount/执行及宿主权限设置。
        with patch("builder.RootFSMaterializer", materializer), \
                patch.object(RootFSMaterializer, "_metadata"), \
                patch("overlay.OverlayManager", overlay), \
                patch("executor.RunExecutor", executor), \
                patch("builder.host_architecture", return_value="amd64"):
            build(dockerfile, context, base, None, TAG, output, enable_run=True,
                  run_sandbox="rootless", cache_dir=root / "cache" if cached else None,
                  cache_stats=stats)
        image = ImageArchiveReader(output, root / (output_name + "-layers")).read(TAG)
        index = RootFSIndex()
        for layer in image.layers:
            index.apply_layer(layer)
        return json.loads(index.read_file("seen.json")), calls, stats, index

    def test_scratch_stage_run_cannot_see_previous_stage_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            seen, calls, _, index = self.run_build(Path(temporary))
            self.assertEqual(calls, ["generate", "inspect"])
            self.assertEqual(seen, [])
            self.assertIsNone(index.kind("private/compile.war"))
            self.assertIsNone(index.kind("generated.war"))

    def test_external_cas_stage_run_sees_only_its_base(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            layer = root / "base-layer.tar"
            with tarfile.open(layer, "w") as archive:
                info = tarfile.TarInfo("runtime.marker")
                info.size = 4
                archive.addfile(info, io.BytesIO(b"base"))
            config = {"os": "linux", "architecture": "amd64", "config": {},
                      "rootfs": {"type": "layers", "diff_ids": ["sha256:" + sha256_file(layer)]}}
            archive = root / "base.tar"
            reference = "example/runtime:1"
            ImageArchiveWriter().write_new(archive, config, [layer], reference)
            store = root / "store"
            CASStore(store).import_docker(archive, reference, "linux/amd64")
            source = {"type": "cas", "reference": reference, "platform": "linux/amd64",
                      "source": "local", "store": str(store)}
            seen, _, _, index = self.run_build(root, parent=reference, base=source)
            self.assertEqual(seen, ["runtime.marker"])
            self.assertEqual(index.read_file("runtime.marker"), b"base")

    def test_from_prior_stage_preserves_intentional_inheritance(self):
        with tempfile.TemporaryDirectory() as temporary:
            seen, _, _, index = self.run_build(Path(temporary), parent="compile")
            self.assertEqual(seen, ["generated.war", "private", "private/compile.war"])
            self.assertEqual(index.read_file("generated.war"), b"generated")

    def test_copy_from_prior_stage_exposes_only_explicit_selection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context, dockerfile, calls, overlay, executor, materializer = self.fixture(root)
            dockerfile.write_text(dockerfile.read_text().replace(
                "FROM scratch AS runtime\n",
                "FROM scratch AS runtime\nCOPY --from=compile /generated.war /selected.war\n"))
            output = root / "selected.tar"
            with patch("builder.RootFSMaterializer", materializer), \
                    patch.object(RootFSMaterializer, "_metadata"), \
                    patch("overlay.OverlayManager", overlay), \
                    patch("executor.RunExecutor", executor), \
                    patch("builder.host_architecture", return_value="amd64"):
                build(dockerfile, context, None, None, TAG, output, enable_run=True,
                      run_sandbox="rootless")
            image = ImageArchiveReader(output, root / "selected-layers").read(TAG)
            index = RootFSIndex()
            for layer in image.layers:
                index.apply_layer(layer)
            self.assertEqual(json.loads(index.read_file("seen.json")), ["selected.war"])
            self.assertEqual(index.read_file("selected.war"), b"generated")

    def test_same_stage_later_run_sees_earlier_run_layer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context, dockerfile, _, overlay, executor, materializer = self.fixture(root)
            dockerfile.write_text(dockerfile.read_text().replace("FROM scratch AS runtime\n", ""))
            output = root / "same-stage.tar"
            with patch("builder.RootFSMaterializer", materializer), \
                    patch.object(RootFSMaterializer, "_metadata"), \
                    patch("overlay.OverlayManager", overlay), \
                    patch("executor.RunExecutor", executor), \
                    patch("builder.host_architecture", return_value="amd64"):
                build(dockerfile, context, None, None, TAG, output, enable_run=True,
                      run_sandbox="rootless")
            image = ImageArchiveReader(output, root / "same-stage-layers").read(TAG)
            index = RootFSIndex()
            for layer in image.layers:
                index.apply_layer(layer)
            self.assertEqual(json.loads(index.read_file("seen.json")),
                             ["generated.war", "private", "private/compile.war"])

    def test_repeated_cached_build_preserves_isolation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seen, calls, stats, _ = self.run_build(root, cached=True, output_name="first")
            self.assertEqual(seen, [])
            self.assertEqual(calls, ["generate", "inspect"])
            self.assertEqual(stats["misses"], 3)
            seen, calls, stats, _ = self.run_build(root, cached=True, output_name="second")
            self.assertEqual(seen, [])
            self.assertEqual(calls, [])
            self.assertEqual(stats["hits"], 3)

    def test_legacy_polluted_run_cache_is_not_reused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch("builder.CACHE_VERSION", 22), patch("cache.CACHE_VERSION", 22):
                polluted, _, _, _ = self.run_build(root, cached=True, legacy=True,
                                                   output_name="legacy")
            self.assertIn("private/compile.war", polluted)
            seen, calls, stats, _ = self.run_build(root, cached=True, output_name="fixed")
            self.assertEqual(seen, [])
            self.assertEqual(calls, ["generate", "inspect"])
            self.assertEqual(stats["hits"], 0)


if __name__ == "__main__":
    unittest.main()

