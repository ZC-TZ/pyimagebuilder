import io
import errno
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
import executor
import main
import overlay
from errors import BuildError
from rootfs_materializer import RootFSMaterializer
from sandbox import AUDIT_ARCH, BPF_JMP_JEQ_K, BPF_JMP_JSET_K, \
    CLONE_NAMESPACE_MASK, CLONE_SYSCALL, DENIED, build_filter


def make_layer(path, uid=0):
    with tarfile.open(path, "w") as archive:
        entry = tarfile.TarInfo("file")
        entry.uid = uid
        entry.gid = 0
        entry.size = 4
        archive.addfile(entry, io.BytesIO(b"data"))


class PhaseTenTests(unittest.TestCase):
    def test_rootless_materializer_accepts_only_mapped_ownership(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            okay = root / "okay.tar"
            unsupported = root / "unsupported.tar"
            make_layer(okay)
            make_layer(unsupported, uid=1000)
            materializer = RootFSMaterializer(root / "rootfs", rootless=True)
            with patch.object(os, "chmod"), patch.object(os, "utime"):
                materializer.apply(okay)
            self.assertEqual((materializer.root / "file").read_bytes(), b"data")
            with patch.object(os, "chmod"), patch.object(os, "utime"):
                with self.assertRaises(BuildError):
                    materializer.apply(unsupported)

    def test_rootless_executor_requires_nonroot_linux(self):
        with patch.object(executor.platform, "system", return_value="Linux"), \
             patch.object(executor.os, "geteuid", return_value=1000, create=True):
            self.assertEqual(executor.RunExecutor("none", "rootless").sandbox, "rootless")
            with self.assertRaises(BuildError):
                executor.RunExecutor("none", "unknown")
        with patch.object(executor.platform, "system", return_value="Linux"), \
             patch.object(executor.os, "geteuid", return_value=0, create=True):
            with self.assertRaises(BuildError):
                executor.RunExecutor("none", "rootless")

    def test_seccomp_filter_checks_architecture_and_denies_mount(self):
        for arch, syscall in (("x86_64", 165), ("aarch64", 40)):
            result = build_filter(arch)
            self.assertEqual(result[1], (BPF_JMP_JEQ_K, 1, 0, AUDIT_ARCH[arch]))
            self.assertIn(syscall, DENIED[arch])
            self.assertIn((BPF_JMP_JEQ_K, 0, 1, syscall), result)
            self.assertIn((BPF_JMP_JEQ_K, 0, 1, 425), result)
            self.assertIn((BPF_JMP_JEQ_K, 0, 3, CLONE_SYSCALL[arch]), result)
            self.assertIn((BPF_JMP_JSET_K, 0, 1, CLONE_NAMESPACE_MASK), result)
        with self.assertRaises(BuildError):
            build_filter("unknown")

    def test_rootless_overlay_reads_user_whiteout_when_trusted_xattr_denied(self):
        def getxattr(path, name, follow_symlinks=False):
            if name.startswith("trusted."):
                raise PermissionError(errno.EPERM, "denied")
            return b"y"
        with patch.object(overlay.os, "getxattr", side_effect=getxattr, create=True):
            self.assertEqual(overlay._overlay_value("unused", "opaque"), b"y")

    def test_rootless_wsl_uses_default_user(self):
        args = SimpleNamespace(run=True, base_map=None, base_tar=Path("C:/base.tar"),
                               dockerfile=Path("C:/Dockerfile"), context=Path("C:/context"),
                               output=Path("C:/result.tar"), tag="example/result:1",
                               workspace=None, run_network="none", run_sandbox="rootless",
                               wsl_distro=None, wsl_python="python3", wsl_mount_root="/mnt",
                               no_cache=True, cache_dir=None)
        with patch.object(main.platform, "system", return_value="Windows"), \
             patch.object(main.shutil, "which", return_value="wsl.exe"), \
             patch.object(main.subprocess, "call", return_value=0) as call:
            self.assertEqual(main._run_wsl(args), 0)
        command = call.call_args.args[0]
        self.assertEqual(command[:3], ["wsl.exe", "--exec", "python3"])
        self.assertIn("rootless", command)

    def test_mode_is_in_run_cache_key(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = root / "context"
            context.mkdir()
            (context / "Dockerfile").write_text("FROM scratch\nRUN echo hello\n")
            cache = root / "cache"
            class FakeMaterializer:
                def __init__(self, path, rootless=False):
                    self.root = path
                    path.mkdir()
                def apply(self, path):
                    pass
            class FakeOverlay:
                def __init__(self, rootfs, workspace, rootless=False):
                    self.rootless = rootless
                def to_layer(self, path):
                    with tarfile.open(path, "w") as archive:
                        entry = tarfile.TarInfo("hello")
                        entry.size = 1
                        archive.addfile(entry, io.BytesIO(b"x"))
                    import hashlib
                    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
            class FakeExecutor:
                def __init__(self, network, sandbox="legacy"):
                    pass
                def execute(self, *args):
                    pass
            with patch.object(builder, "RootFSMaterializer", FakeMaterializer), \
                 patch("overlay.OverlayManager", FakeOverlay), \
                 patch("executor.RunExecutor", FakeExecutor):
                first = {}
                second = {}
                builder.build(context / "Dockerfile", context, None, None,
                              "example/sandbox:1", root / "first.tar", enable_run=True,
                              cache_dir=cache, cache_stats=first, run_sandbox="hardened")
                builder.build(context / "Dockerfile", context, None, None,
                              "example/sandbox:1", root / "second.tar", enable_run=True,
                              cache_dir=cache, cache_stats=second, run_sandbox="rootless")
            self.assertEqual(first["misses"], 1)
            self.assertEqual(second["misses"], 1)


if __name__ == "__main__":
    unittest.main()
