"""验证 RUN 挂载的路径归一化、预检顺序与直接执行入口。"""

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dockerfile_parser import Command
from errors import BuildError
from executor import RunExecutor
from run_mounts import prepare
from builder import build


class RunMountBoundaryTests(unittest.TestCase):
    def test_normalized_root_targets_are_rejected_without_cache_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for target in ("/", "//", "/cache/..", "/cache/../.", "//cache/.."):
                for kind in ("cache", "secret"):
                    with self.subTest(target=target, kind=kind):
                        options = ({"type": kind, "id": "data", "target": target},)
                        with self.assertRaises(BuildError):
                            prepare(options, {"data": root / "secret"},
                                    root / "cache", root, "linux/amd64")
                        self.assertFalse((root / "cache").exists())

    def test_aliases_cannot_bypass_overlap_or_virtual_path_checks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for target in ("//cache", "//cache/sub", "/cache/child/.."):
                options = ({"type": "cache", "target": "/cache"},
                           {"type": "cache", "target": target})
                with self.subTest(target=target), self.assertRaises(BuildError):
                    prepare(options, {}, root / "cache", root, "linux/amd64")
                self.assertFalse((root / "cache").exists())
            for target in ("//proc", "//sys/entry", "/cache/../dev/null"):
                with self.subTest(target=target), self.assertRaises(BuildError):
                    prepare(({"type": "cache", "target": target},), {},
                            root / "cache", root, "linux/amd64")

    def test_later_invalid_mount_does_not_allocate_earlier_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            options = ({"type": "cache", "id": "valid", "target": "/cache"},
                       {"type": "secret", "id": "missing", "required": "true"})
            with self.assertRaises(BuildError):
                prepare(options, {}, root / "cache", root, "linux/amd64")
            self.assertFalse((root / "cache").exists())

    def test_absent_optional_secret_still_validates_its_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for target in ("relative", "/cache/..", "//proc/secrets"):
                with self.subTest(target=target), self.assertRaises(BuildError):
                    prepare(({"type": "secret", "id": "missing", "required": "false",
                              "target": target},), {}, root / "cache", root, "linux/amd64")
            self.assertEqual(prepare(({"type": "secret", "id": "missing", "required": "false"},),
                                     {}, root / "cache", root, "linux/amd64"), [])

    def test_valid_alias_is_returned_as_one_canonical_absolute_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = prepare(({"type": "cache", "id": "pip", "target": "//app/cache/./"},),
                             {}, root / "cache", root, "linux/amd64")
            self.assertEqual(result[0][2], "/app/cache")
            self.assertTrue(result[0][1].is_dir())

    def test_linked_cache_root_is_rejected_before_mkdir(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mount_root = root / "cache" / "run-mounts"
            with patch("run_mounts.is_linked_directory", side_effect=lambda path: path == mount_root), \
                 self.assertRaises(BuildError):
                prepare(({"type": "cache", "target": "/cache"},), {},
                        root / "cache", root, "linux/amd64")
            self.assertFalse((root / "cache").exists())

    def test_linked_cache_source_is_rejected_before_directory_creation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mount_root = root / "cache" / "run-mounts"
            with patch("run_mounts.is_linked_directory", side_effect=lambda path: path.parent == mount_root), \
                 self.assertRaises(BuildError):
                prepare(({"type": "cache", "target": "/cache"},), {},
                        root / "cache", root, "linux/amd64")
            self.assertEqual(list(mount_root.iterdir()), [])

    def test_invalid_mount_build_does_not_execute_or_publish(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = root / "context"
            context.mkdir()
            dockerfile = context / "Dockerfile"
            dockerfile.write_text("FROM scratch\nRUN --mount=type=cache,target=/cache/.. true\n",
                                  encoding="utf-8")
            with patch("executor.RunExecutor") as runner, self.assertRaises(BuildError):
                build(dockerfile, context, None, None, "example/invalid:1", root / "image.tar",
                      enable_run=True, cache_dir=root / "cache")
            runner.assert_not_called()
            self.assertFalse((root / "image.tar").exists())
            self.assertFalse((root / "cache" / "run-mounts").exists())
            self.assertEqual(list((root / "cache" / "entries").iterdir()), [])

    def test_direct_executor_rejects_invalid_mounts_before_pipe_or_fork(self):
        runner = object.__new__(RunExecutor)
        runner.network, runner.sandbox = "none", "hardened"
        invalid_mounts = (
            (("cache", Path("cache"), "/cache/.."),),
            (("cache", Path("cache"), "//proc"),),
            (("cache", Path("cache"), "/cache"), ("cache", Path("other"), "//cache/sub")),
        )
        for mounts in invalid_mounts:
            with self.subTest(mounts=mounts), patch("executor.os.pipe") as pipe, \
                 self.assertRaises(BuildError):
                runner.execute(SimpleNamespace(), Command(True, "true"), {}, "/", "", ["/bin/sh", "-c"], mounts)
            pipe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
