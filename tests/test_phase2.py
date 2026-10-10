import hashlib
import io
import json
import os
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

import builder
import main
import overlay
from dockerfile_parser import parse
from errors import BuildError, UnsupportedInstruction
from image_reader import ImageArchiveReader
from rootfs import RootFSIndex
from test_phase1 import make_base


class PhaseTwoTests(unittest.TestCase):
    def test_parser_carries_user_shell_and_run(self):
        instructions = parse("""FROM example/base:1
USER 1000:1000
SHELL ["/bin/bash", "-c"]
RUN echo hello
""")
        self.assertEqual(instructions[1].value, "1000:1000")
        self.assertEqual(instructions[2].value, ["/bin/bash", "-c"])
        self.assertTrue(instructions[3].value.shell_form)
        self.assertEqual(instructions[3].value.value, "echo hello")

    def test_overlay_whiteout_and_opaque_conversion(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            lower = root / "lower"
            lower.mkdir()
            work = root / "snapshot"
            work.mkdir()
            manager = overlay.OverlayManager(lower, work)
            (manager.upper / "deleted").write_bytes(b"")
            directory = manager.upper / "opaque"
            directory.mkdir()
            (directory / "new.txt").write_bytes(b"new")

            def fake_xattr(path, suffix):
                if Path(path).name == "deleted" and suffix == "whiteout":
                    return b""
                if Path(path).name == "opaque" and suffix == "opaque":
                    return b"y"
                return None

            target = root / "layer.tar"
            with patch.object(overlay, "_overlay_value", side_effect=fake_xattr), \
                 patch.object(overlay.os, "listxattr", return_value=[], create=True):
                diff_id = manager.to_layer(target)
            self.assertEqual(diff_id, "sha256:" + hashlib.sha256(target.read_bytes()).hexdigest())
            with tarfile.open(target) as archive:
                names = archive.getnames()
                self.assertIn(".wh.deleted", names)
                self.assertIn("opaque/.wh..wh..opq", names)
                self.assertEqual(archive.extractfile("opaque/new.txt").read(), b"new")

    def test_run_updates_archive_when_executor_succeeds(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base = make_base(root)
            context = root / "context"
            context.mkdir()
            (context / "Dockerfile").write_text("""FROM example/base:1
ENV MODE=test
WORKDIR /app
USER 123:456
SHELL ["/bin/sh", "-c"]
RUN rm /etc/new.conf && touch /run.txt
""")
            calls = []

            class FakeMaterializer:
                def __init__(self, path):
                    self.root = path
                    self.root.mkdir()

                def apply(self, path):
                    calls.append(("apply", path.name))

            class FakeOverlay:
                def __init__(self, rootfs, workspace):
                    self.rootfs = rootfs

                def to_layer(self, target):
                    with tarfile.open(target, "w") as archive:
                        for name, data in (("etc/.wh.new.conf", b""), ("run.txt", b"done")):
                            info = tarfile.TarInfo(name)
                            info.size = len(data)
                            archive.addfile(info, io.BytesIO(data))
                    return "sha256:" + hashlib.sha256(target.read_bytes()).hexdigest()

            class FakeExecutor:
                def __init__(self, network, sandbox="legacy"):
                    calls.append(("network", network))

                def execute(self, snapshot, command, env, workdir, user, shell):
                    calls.append(("run", command.value, env["MODE"], workdir, user, shell))

            output = root / "result.tar"
            with patch.object(builder.platform, "system", return_value="Linux"), \
                 patch.object(builder, "RootFSMaterializer", FakeMaterializer), \
                 patch("overlay.OverlayManager", FakeOverlay), \
                 patch("executor.RunExecutor", FakeExecutor):
                builder.build(context / "Dockerfile", context, base, None,
                              "example/result:2", output, enable_run=True)
            self.assertIn(("run", "rm /etc/new.conf && touch /run.txt", "test", "/app",
                           "123:456", ["/bin/sh", "-c"]), calls)
            with tempfile.TemporaryDirectory() as extracted:
                image = ImageArchiveReader(output, Path(extracted)).read("example/result:2")
                self.assertEqual(len(image.layers), 3)
                index = RootFSIndex()
                for layer in image.layers:
                    index.apply_layer(layer)
                self.assertIsNone(index.kind("etc/new.conf"))
                self.assertEqual(index.kind("run.txt"), "file")

    def test_run_nonzero_aborts_without_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base = make_base(root)
            context = root / "context"
            context.mkdir()
            (context / "Dockerfile").write_text("FROM example/base:1\nRUN false\n")
            output = root / "result.tar"

            class FakeMaterializer:
                def __init__(self, path):
                    self.root = path
                    self.root.mkdir()

                def apply(self, path):
                    pass

            class FakeOverlay:
                def __init__(self, rootfs, workspace):
                    pass

            class FailingExecutor:
                def __init__(self, network, sandbox="legacy"):
                    pass

                def execute(self, *args):
                    raise BuildError("RUN failed: process exited with code 1")

            with patch.object(builder.platform, "system", return_value="Linux"), \
                 patch.object(builder, "RootFSMaterializer", FakeMaterializer), \
                 patch("overlay.OverlayManager", FakeOverlay), \
                 patch("executor.RunExecutor", FailingExecutor):
                with self.assertRaises(BuildError):
                    builder.build(context / "Dockerfile", context, base, None,
                                  "example/result:2", output, enable_run=True)
            self.assertFalse(output.exists())

    @unittest.skipUnless(os.name == "nt", "Windows WSL bridge")
    def test_wsl_bridge_uses_our_python_project(self):
        args = SimpleNamespace(run=True, base_map=None, base_tar=Path("C:/base.tar"),
                               dockerfile=Path("C:/Dockerfile"), context=Path("C:/context"),
                               output=Path("C:/result.tar"), tag="example/result:1",
                               workspace=None, run_network="none", wsl_distro=None,
                               wsl_python="python3", wsl_mount_root="/mnt",
                               no_cache=False, cache_dir=None)
        with patch.object(main.platform, "system", return_value="Windows"), \
             patch.object(main.shutil, "which", return_value="wsl.exe"), \
             patch.object(main.subprocess, "call", return_value=0) as call:
            self.assertEqual(main._run_wsl(args), 0)
            command = call.call_args[0][0]
            self.assertEqual(command[:5], ["wsl.exe", "--user", "root", "--exec", "python3"])
            self.assertIn("/mnt/c/base.tar", command)
            self.assertIn("--run", command)
            self.assertEqual(command[command.index("--run-sandbox") + 1], "hardened")


if __name__ == "__main__":
    unittest.main()
