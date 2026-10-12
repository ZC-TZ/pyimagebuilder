"""区分 tar 完整性与层应用成功，防止失败指令发布可命中的缓存。"""

import io
import sys
import tarfile
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from builder import build
from errors import BuildError
from image_reader import ImageArchiveReader, sha256_file
from rootfs import RootFSIndex
from rootfs_materializer import RootFSMaterializer


def write_layer(path, kind="file"):
    """无效用例仍生成摘要正确、格式完整的 tar，排除传输损坏的影响。"""
    with tarfile.open(path, "w", format=tarfile.PAX_FORMAT) as archive:
        member = tarfile.TarInfo("payload")
        if kind == "symlink":
            member.type, member.linkname = tarfile.SYMTYPE, "bad\\target"
            archive.addfile(member)
        elif kind == "hardlink":
            member.type, member.linkname = tarfile.LNKTYPE, "missing"
            archive.addfile(member)
        else:
            member.uid = 4294967295 if kind == "owner" else 0
            member.size = 4
            archive.addfile(member, io.BytesIO(b"good"))
    return "sha256:" + sha256_file(path)


class LayerAcceptanceCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.context = self.root / "context"
        self.context.mkdir()
        self.dockerfile = self.context / "Dockerfile"
        self.cache = self.root / "cache"

    @contextmanager
    def run_environment(self):
        """不挂载内核；模拟执行器，保留真实层生成、索引、物化与缓存流程。"""
        executions = []

        class Runner:
            def __init__(self, *args, **kwargs):
                pass

            def execute(self, manager, command, *args):
                executions.append(command.value)

        with ExitStack() as stack:
            stack.enter_context(patch("executor.RunExecutor", Runner))
            # Windows 无法恢复 POSIX 属主；层的语义预检仍由真实 apply 执行。
            stack.enter_context(patch.object(RootFSMaterializer, "_metadata"))
            yield executions

    def build_image(self, name, stats=None, **kwargs):
        return build(self.dockerfile, self.context, None, None, "example/app:1",
                     self.root / name, enable_run=True, cache_dir=self.cache,
                     cache_stats=stats, **kwargs)

    def assert_unpublished(self, output, stats):
        self.assertFalse((self.root / output).exists())
        self.assertEqual(list((self.cache / "entries").iterdir()), [])
        self.assertEqual(list((self.cache / "layers").iterdir()), [])
        self.assertEqual(stats["stored"], 0)

    def invalid_run_then_recover(self, kind):
        self.dockerfile.write_text("FROM scratch\nRUN create-payload\n", encoding="utf-8")
        state = [kind]
        stats = {}
        with self.run_environment() as executions, \
                patch("overlay.OverlayManager.to_layer",
                      side_effect=lambda path: write_layer(path, state[0])):
            with self.assertRaises(BuildError):
                self.build_image("failed.tar", stats)
            self.assert_unpublished("failed.tar", stats)
            # 同一 Dockerfile 在执行器恢复后必须重新执行，再次构建才允许命中。
            state[0] = "file"
            for number in range(2):
                stats = {}
                name = "success-{}.tar".format(number)
                self.build_image(name, stats)
                self.assertEqual((stats["hits"], stats["misses"], stats["stored"]),
                                 (number, 1 - number, 1 - number))
                image = ImageArchiveReader(self.root / name, self.root / ("read-{}".format(number))).read("example/app:1")
                index = RootFSIndex()
                for layer in image.layers:
                    index.apply_layer(layer)
                self.assertEqual(index.read_file("payload"), b"good")
            self.assertEqual(len(executions), 2)

    def test_invalid_symlink_does_not_poison_retry(self):
        self.invalid_run_then_recover("symlink")

    def test_missing_hardlink_does_not_poison_retry(self):
        self.invalid_run_then_recover("hardlink")

    def test_invalid_owner_does_not_poison_retry(self):
        self.invalid_run_then_recover("owner")

    def test_materialization_failure_prevents_cache_publication(self):
        self.dockerfile.write_text("FROM scratch\nRUN create-payload\n", encoding="utf-8")
        stats = {}
        with self.run_environment(), \
                patch("overlay.OverlayManager.to_layer", side_effect=write_layer), \
                patch.object(RootFSMaterializer, "apply",
                             side_effect=BuildError("Cannot preserve layer mode/time")):
            with self.assertRaisesRegex(BuildError, "mode/time"):
                self.build_image("failed.tar", stats)
        self.assert_unpublished("failed.tar", stats)

    def test_copy_uses_the_same_acceptance_gate(self):
        self.dockerfile.write_text("FROM scratch\nCOPY app.war /payload\n", encoding="utf-8")
        (self.context / "app.war").write_bytes(b"WAR input")
        stats = {}
        with patch("layer.LayerBuilder.transfer_layer",
                   side_effect=lambda transfer, workdir, path, **kwargs: write_layer(path, "hardlink")):
            with self.assertRaisesRegex(BuildError, "Hardlink target missing"):
                self.build_image("failed.tar", stats)
        self.assert_unpublished("failed.tar", stats)

    def test_cached_and_new_layers_are_applied_exactly_once(self):
        self.dockerfile.write_text("FROM scratch\nRUN first\nRUN second\n", encoding="utf-8")
        indexed = RootFSIndex.apply_layer
        seen = []

        def apply(index, path):
            seen.append(str(path))
            return indexed(index, path)

        with self.run_environment() as executions, \
                patch("overlay.OverlayManager.to_layer", side_effect=write_layer), \
                patch.object(RootFSIndex, "apply_layer", apply):
            identities = []
            for number in range(2):
                seen.clear()
                stats = {}
                name = "success-{}.tar".format(number)
                self.build_image(name, stats)
                self.assertEqual(len(seen), 2)
                self.assertEqual((stats["hits"], stats["misses"]), (number * 2, (1 - number) * 2))
                image = ImageArchiveReader(self.root / name, self.root / ("read-{}".format(number))).read("example/app:1")
                identities.append(image.config_digest)
            self.assertEqual(identities[0], identities[1])
            self.assertEqual(executions, ["first", "second"])

    def test_later_failure_keeps_valid_earlier_instruction_cache(self):
        self.dockerfile.write_text("FROM scratch\nCOPY app.war /app.war\nRUN create-payload\n",
                                  encoding="utf-8")
        (self.context / "app.war").write_bytes(b"WAR input")
        state = ["hardlink"]
        stats = {}
        with self.run_environment(), patch("overlay.OverlayManager.to_layer",
                side_effect=lambda path: write_layer(path, state[0])):
            with self.assertRaises(BuildError):
                self.build_image("failed.tar", stats)
            self.assertFalse((self.root / "failed.tar").exists())
            self.assertEqual(stats["stored"], 1)
            self.assertEqual(len(list((self.cache / "entries").iterdir())), 1)
            self.assertEqual(len(list((self.cache / "layers").iterdir())), 1)
            state[0] = "file"
            stats = {}
            self.build_image("recovered.tar", stats)
            self.assertEqual((stats["hits"], stats["misses"], stats["stored"]), (1, 1, 1))

    def test_secret_cache_bypass_still_applies_output_once(self):
        self.dockerfile.write_text("FROM scratch\nRUN --mount=type=secret,id=token use-secret\n",
                                  encoding="utf-8")
        secret = self.root / "secret"
        secret.write_bytes(b"never archived")
        stats = {}
        with self.run_environment(), patch("overlay.OverlayManager.to_layer", side_effect=write_layer):
            self.build_image("secret.tar", stats, secret_sources={"token": secret})
        self.assertEqual((stats["hits"], stats["misses"], stats["stored"]), (0, 1, 0))
        image = ImageArchiveReader(self.root / "secret.tar", self.root / "read-secret").read("example/app:1")
        index = RootFSIndex()
        for layer in image.layers:
            index.apply_layer(layer)
        self.assertEqual(index.read_file("payload"), b"good")
        self.assertEqual(list((self.cache / "entries").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
