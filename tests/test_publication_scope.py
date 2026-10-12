"""验证发布后的清理异常、中断及独立入口不会留下错误引用或半套产物。"""

import contextlib
import errno
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))

import artifactory_download
import builder
import fast
import file_publish
import image_cli
import hermetic
import main as builder_main
import multiarch
import registry
from cas_store import CASStore
from image_reader import ImageArchiveReader
from errors import ArchiveError
from optimizer import optimize_image
from rootfs_archive import export_rootfs, flatten_image, import_rootfs
from test_artifactory_publication import (artifact_files, download, layer_bytes,
                                        REFERENCE as ARTIFACT_REF)
from test_phase1 import make_base
from test_ref_transactions import make_image, REFERENCE, PLATFORM
from test_registry_publication import local_registry


@contextlib.contextmanager
def failing_cleanup(prefix):
    """仅模拟指定工作目录在成功路径退出时抛异常，目录的实际资源仍被释放。"""
    original = tempfile.TemporaryDirectory

    class Directory:
        def __init__(self, *args, **kwargs):
            self.directory = original(*args, **kwargs)
            self.fail = kwargs.get("prefix") == prefix

        def __enter__(self):
            return self.directory.__enter__()

        def cleanup(self):
            self.directory.cleanup()
            if self.fail:
                raise OSError("temporary cleanup failed")

        def __exit__(self, exc_type, *args):
            self.directory.__exit__(exc_type, *args)
            if exc_type is None and self.fail:
                raise OSError("temporary cleanup failed")

    with patch.object(tempfile, "TemporaryDirectory", Directory):
        yield


class PublicationScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_direct_docker_import_cleanup_failure_does_not_publish_ref(self):
        for existing in (False, True):
            with self.subTest(existing=existing):
                case = self.root / str(existing)
                case.mkdir()
                old, new = make_image(case, "old"), make_image(case, "new")
                store = CASStore(case / "store")
                if existing:
                    store.import_docker(old, REFERENCE, PLATFORM)
                before = store.snapshot_ref(REFERENCE, PLATFORM, "local")
                with failing_cleanup("cas-import-"):
                    with self.assertRaisesRegex(OSError, "cleanup failed"):
                        store.import_docker(new, REFERENCE, PLATFORM, replace=True)
                self.assertEqual(store.snapshot_ref(REFERENCE, PLATFORM, "local"), before)
                store.import_docker(new, REFERENCE, PLATFORM, replace=True)
                self.assertNotEqual(store.snapshot_ref(REFERENCE, PLATFORM, "local"), before)

    def test_load_restores_ref_when_import_raises_after_publishing(self):
        for failure in (OSError, KeyboardInterrupt):
            for existing in (False, True):
                with self.subTest(failure=failure, existing=existing):
                    case = self.root / (failure.__name__ + str(existing))
                    case.mkdir()
                    old, new = make_image(case, "old"), make_image(case, "new")
                    directory = case / "store"
                    if existing:
                        image_cli.load_archive(old, directory)
                    store = CASStore(directory)
                    before = store.snapshot_ref(REFERENCE, PLATFORM, "local")
                    target = image_cli._archive_path(directory, REFERENCE, PLATFORM, "local")
                    original = CASStore.import_docker

                    def publish_then_fail(cas, *args, **kwargs):
                        original(cas, *args, **kwargs)
                        raise failure("interrupted after CAS publication")

                    with patch.object(CASStore, "import_docker", publish_then_fail):
                        with self.assertRaises(failure):
                            image_cli.load_archive(new, directory, replace=True)
                    self.assertEqual(store.snapshot_ref(REFERENCE, PLATFORM, "local"), before)
                    self.assertEqual(target.exists(), existing)
                    if existing:
                        self.assertEqual(target.read_bytes(), old.read_bytes())
                    self.assertFalse(target.with_name(target.name + ".part").exists())

    def test_cas_ref_publication_interruption_restores_snapshot(self):
        case = self.root
        old, new = make_image(case, "old"), make_image(case, "new")
        store = CASStore(case / "store")
        store.import_docker(old, REFERENCE, PLATFORM)
        before = store.snapshot_ref(REFERENCE, PLATFORM, "local")
        original = CASStore._publish_ref

        def publish_then_interrupt(cas, *args):
            original(cas, *args)
            raise KeyboardInterrupt("interrupt after atomic ref replacement")

        with patch.object(CASStore, "_publish_ref", publish_then_interrupt):
            with self.assertRaises(KeyboardInterrupt):
                store.import_docker(new, REFERENCE, PLATFORM, replace=True)
        self.assertEqual(store.snapshot_ref(REFERENCE, PLATFORM, "local"), before)

    def test_replace_repairs_corrupt_ref_and_failed_attempt_restores_original_bytes(self):
        source = make_image(self.root, "source")
        store = CASStore(self.root / "store")
        store.import_docker(source, REFERENCE, PLATFORM)
        ref = store._ref(REFERENCE, PLATFORM, "local")
        damaged = b"broken ref bytes\n"
        ref.write_bytes(damaged)
        self.assertEqual(store.snapshot_ref(REFERENCE, PLATFORM, "local"), damaged)
        with patch.object(CASStore, "_publish_ref", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                store.import_docker(source, REFERENCE, PLATFORM, replace=True)
        self.assertEqual(ref.read_bytes(), damaged)
        with self.assertRaises(ArchiveError):
            store.resolve(REFERENCE, PLATFORM, "local")
        store.import_docker(source, REFERENCE, PLATFORM, replace=True)
        self.assertEqual(store.resolve(REFERENCE, PLATFORM, "local")["reference"], REFERENCE)
        # 快照仍有大小上限，不能因不解析旧内容而无限读取元数据。
        with patch("cas_store.MAX_JSON", 4):
            with self.assertRaisesRegex(ArchiveError, "Oversized"):
                store.snapshot_ref(REFERENCE, PLATFORM, "local")

    def test_direct_registry_cleanup_failure_restores_ref_and_all_outputs(self):
        for mode in ("docker", "oci", "both", "cas-only"):
            for existing in (False, True):
                with self.subTest(mode=mode, existing=existing):
                    case = self.root / (mode + str(existing))
                    case.mkdir()
                    store = CASStore(case / "store")
                    if existing:
                        store.import_docker(make_image(case, "old"), REFERENCE, PLATFORM, "registry")
                    before = store.snapshot_ref(REFERENCE, PLATFORM, "registry")
                    output = case / "image.tar"
                    with local_registry(), failing_cleanup("pyimagebuilder-pull-"):
                        with self.assertRaisesRegex(OSError, "cleanup failed"):
                            registry.pull(REFERENCE, output, cas_store=store, cas_only=mode == "cas-only",
                                          image_format="docker" if mode == "cas-only" else mode)
                    self.assertEqual(store.snapshot_ref(REFERENCE, PLATFORM, "registry"), before)
                    self.assertFalse(output.exists())
                    self.assertFalse((case / "image.oci.tar").exists())
                    with local_registry():
                        registry.pull(REFERENCE, output, cas_store=store, cas_only=mode == "cas-only",
                                      image_format="docker" if mode == "cas-only" else mode)
                    self.assertEqual(output.exists(), mode != "cas-only")

    def test_direct_artifactory_cleanup_failure_restores_ref_and_output(self):
        for cas_only in (False, True):
            with self.subTest(cas_only=cas_only):
                case = self.root / str(cas_only)
                case.mkdir()
                store = CASStore(case / "store")
                download(artifact_files(layer_bytes(b"old")), case / "unused.tar", store)
                before = store.snapshot_ref(ARTIFACT_REF, PLATFORM, "artifactory")
                output = case / "new.tar"
                files = artifact_files(layer_bytes(b"new"))
                with failing_cleanup("artifactory_download_"):
                    with self.assertRaisesRegex(OSError, "cleanup failed"):
                        download(files, output, store, cas_only=cas_only)
                self.assertEqual(store.snapshot_ref(ARTIFACT_REF, PLATFORM, "artifactory"), before)
                self.assertFalse(output.exists())
                download(files, output, store, cas_only=cas_only)
                self.assertEqual(output.exists(), not cas_only)

    def test_cas_export_cleanup_failure_removes_output_and_allows_retry(self):
        source = make_image(self.root, "source")
        store = CASStore(self.root / "store")
        store.import_docker(source, REFERENCE, PLATFORM)
        before = store.snapshot_ref(REFERENCE, PLATFORM, "local")
        output = self.root / "export.tar"
        with failing_cleanup("cas-export-"):
            with self.assertRaisesRegex(OSError, "cleanup failed"):
                store.export_docker(REFERENCE, output, PLATFORM, "local")
        self.assertFalse(output.exists())
        self.assertEqual(store.snapshot_ref(REFERENCE, PLATFORM, "local"), before)
        store.export_docker(REFERENCE, output, PLATFORM, "local")
        ImageArchiveReader(output, self.root / "read").read(REFERENCE)

    def test_publication_copy_cleanup_failure_removes_only_owned_destination(self):
        source, target = self.root / "source", self.root / "output"
        source.write_bytes(b"valid content")
        unlink = file_publish.unlink_missing

        def fail_pending(path):
            if Path(path).name.startswith("publish-"):
                raise OSError("pending cleanup failed")
            return unlink(path)

        with patch.object(file_publish.os, "link", side_effect=OSError(errno.EPERM, "no links")), \
                patch.object(file_publish, "unlink_missing", fail_pending):
            with self.assertRaisesRegex(OSError, "pending cleanup failed"):
                file_publish.publish_new_file(source, target)
            self.assertFalse(target.exists())
            target.write_bytes(b"foreign output")
            with self.assertRaises(OSError):
                file_publish.publish_new_file(source, target)
            self.assertEqual(target.read_bytes(), b"foreign output")
        self.assertEqual(source.read_bytes(), b"valid content")

    def test_artifact_pack_final_display_failure_removes_output(self):
        source = self.root / "files"
        source.mkdir()
        (source / "payload").write_bytes(b"valid")
        output = self.root / "image.tar"
        display = Mock(is_tty=False)
        display.status.side_effect = OSError("console unavailable")
        with self.assertRaisesRegex(OSError, "console unavailable"):
            artifactory_download.pack_files(source, output, display)
        self.assertFalse(output.exists())

    def _build_inputs(self):
        context = self.root / "context"
        context.mkdir()
        (context / "payload").write_bytes(b"valid")
        recipe = context / "Dockerfile"
        recipe.write_text("FROM scratch\nCOPY payload /payload\n")
        return context, recipe

    def _use_fixed_hash_seed(self):
        """封闭构建确实要求解释器关闭哈希随机化，不能只伪造环境变量。"""
        if os.environ.get("PYTHONHASHSEED") == "0" and not sys.flags.hash_randomization:
            return False
        environment = dict(os.environ, PYTHONHASHSEED="0")
        result = subprocess.run(
            [sys.executable, "-m", "unittest",
             "test_publication_scope.PublicationScopeTests." + self._testMethodName],
            cwd=PROJECT / "tests", env=environment,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        return True

    def _locked_build_inputs(self):
        context, recipe = self._build_inputs()
        lock = self.root / "inputs.lock.json"
        lock.write_bytes(hermetic._json(hermetic.make_lock(
            recipe, context, tag="app:1", image_format="both",
            source_date_epoch=0)))
        return context, recipe, lock

    def test_hermetic_cleanup_failure_removes_images_and_report(self):
        if self._use_fixed_hash_seed():
            return
        context, recipe, lock = self._locked_build_inputs()
        output = self.root / "locked.tar"
        with failing_cleanup("pyimagebuilder-hermetic-"):
            with self.assertRaisesRegex(OSError, "cleanup failed"):
                hermetic.build_locked(lock, recipe, context, None, None,
                                      "app:1", output, image_format="both")
        self.assertFalse(output.exists())
        self.assertFalse((self.root / "locked.oci.tar").exists())
        self.assertFalse(Path(str(output) + ".hermetic.json").exists())
        hermetic.build_locked(lock, recipe, context, None, None,
                              "app:1", output, image_format="both")
        self.assertTrue(Path(str(output) + ".hermetic.json").is_file())

    def test_hermetic_interrupt_removes_preceding_products(self):
        if self._use_fixed_hash_seed():
            return
        context, recipe, lock = self._locked_build_inputs()
        output = self.root / "locked.tar"
        publish = hermetic.publish_new_file
        calls = []

        def interrupt_second(source, destination):
            if calls:
                raise KeyboardInterrupt("cancel second product")
            calls.append(destination)
            return publish(source, destination)

        with patch.object(hermetic, "publish_new_file", interrupt_second):
            with self.assertRaises(KeyboardInterrupt):
                hermetic.build_locked(lock, recipe, context, None, None,
                                      "app:1", output, image_format="both")
        self.assertEqual(len(calls), 1)
        self.assertFalse(output.exists())
        self.assertFalse((self.root / "locked.oci.tar").exists())
        self.assertFalse(Path(str(output) + ".hermetic.json").exists())

    def test_multiarch_cleanup_failure_removes_output_and_allows_retry(self):
        context, recipe = self._build_inputs()
        paths = []
        for arch in ("amd64", "arm64"):
            output = self.root / (arch + ".tar")
            builder.build(recipe, context, None, None, "app:1", output,
                          image_format="oci", target_platform="linux/" + arch)
            paths.append(output)
        output = self.root / "multiarch.tar"
        with failing_cleanup("pyimagebuilder-multiarch-"):
            with self.assertRaisesRegex(OSError, "cleanup failed"):
                multiarch.combine(paths[0], paths[1], output, "app:1")
        self.assertFalse(output.exists())
        multiarch.combine(paths[0], paths[1], output, "app:1")
        multiarch.verify(output, "app:1")

    def test_main_base_map_cleanup_failure_removes_all_outputs(self):
        context, recipe = self._build_inputs()
        output = self.root / "cli.tar"
        arguments = ["build", str(context), "-t", "app:1", "-o", str(output),
                     "--format", "both", "--attest", "--no-cache", "--offline",
                     "--image-store", str(self.root / "store")]
        with failing_cleanup("pyimagebuilder-base-map-"), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            try:
                status = builder_main.main(arguments)
            except OSError as exc:
                self.fail("CLI cleanup failure escaped its error handler: " + str(exc))
            self.assertEqual(status, 1)
        self.assertIn("cleanup failed", errors.getvalue())
        self.assertFalse(output.exists())
        self.assertFalse((self.root / "cli.oci.tar").exists())
        self.assertFalse(Path(str(output) + ".spdx.json").exists())
        self.assertFalse(Path(str(output) + ".provenance.json").exists())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(builder_main.main(arguments), 0)

    def test_main_final_digest_failure_removes_successful_build_products(self):
        context, recipe = self._build_inputs()
        output = self.root / "cli.tar"
        with patch.object(builder_main, "_archive_digest",
                          side_effect=OSError("final digest interrupted")), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(builder_main.main([
                "build", str(context), "-t", "app:1", "-o", str(output),
                "--format", "both", "--attest", "--no-cache", "--offline",
                "--image-store", str(self.root / "store")]), 1)
        self.assertFalse(output.exists())
        self.assertFalse((self.root / "cli.oci.tar").exists())
        self.assertFalse(Path(str(output) + ".spdx.json").exists())
        self.assertFalse(Path(str(output) + ".provenance.json").exists())

    def test_wsl_map_cleanup_failure_removes_only_successful_child_products(self):
        mapping = self.root / "base-map.json"
        mapping.write_text('{"app:base":"base.tar"}')
        output = self.root / "child.tar"
        args = SimpleNamespace(
            run=True, base_map=mapping, base_tar=None,
            dockerfile=self.root / "Dockerfile", context=self.root,
            output=output, tag="app:1", format="both", attest=True,
            sign_key=self.root / "private.key",
            workspace=None, run_network="none", run_sandbox="rootless",
            wsl_distro=None, wsl_python="python3", wsl_mount_root="/mnt",
            no_cache=True, cache_dir=None)
        products = [output, self.root / "child.oci.tar",
                    Path(str(output) + ".spdx.json"),
                    Path(str(output) + ".provenance.json"),
                    Path(str(output) + ".dsse.json")]

        def child(command):
            for path in products:
                Path(path).write_bytes(b"child product")
            return 0

        with patch.object(builder_main.platform, "system", return_value="Windows"), \
                patch.object(builder_main.shutil, "which", return_value="wsl.exe"), \
                patch.object(builder_main.subprocess, "call", child), \
                patch.object(builder_main, "_wsl_path", side_effect=lambda path, _: str(path)), \
                failing_cleanup("pyimagebuilder-wsl-map-"):
            with self.assertRaisesRegex(OSError, "cleanup failed"):
                builder_main._run_wsl(args)
        self.assertTrue(all(not Path(path).exists() for path in products))
        # 未成功返回的子进程不能证明目标归属，外层不得删除它未登记的文件。
        output.write_bytes(b"foreign output")
        with patch.object(builder_main.platform, "system", return_value="Windows"), \
                patch.object(builder_main.shutil, "which", return_value="wsl.exe"), \
                patch.object(builder_main.subprocess, "call", return_value=1), \
                patch.object(builder_main, "_wsl_path", side_effect=lambda path, _: str(path)), \
                failing_cleanup("pyimagebuilder-wsl-map-"):
            with self.assertRaisesRegex(OSError, "cleanup failed"):
                builder_main._run_wsl(args)
        self.assertEqual(output.read_bytes(), b"foreign output")


    def test_multifile_build_interrupt_removes_preceding_products(self):
        context, recipe = self._build_inputs()
        output = self.root / "image.tar"
        publish = builder.publish_new_file
        calls = []

        def second_interrupt(source, target):
            if calls:
                raise KeyboardInterrupt("cancel second product")
            calls.append(target)
            return publish(source, target)

        with patch.object(builder, "publish_new_file", second_interrupt):
            with self.assertRaises(KeyboardInterrupt):
                builder.build(recipe, context, None, None, "app:1", output,
                              image_format="both", attest=True)
        self.assertEqual(len(calls), 1)
        self.assertFalse(output.exists())
        self.assertFalse((self.root / "image.oci.tar").exists())
        self.assertEqual(list(self.root.glob("*.json")), [])

    def test_build_workspace_cleanup_failure_removes_all_products(self):
        context, recipe = self._build_inputs()
        output = self.root / "image.tar"
        with failing_cleanup("pyimagebuilder-"):
            with self.assertRaisesRegex(OSError, "cleanup failed"):
                builder.build(recipe, context, None, None, "app:1", output,
                              image_format="both", attest=True)
        self.assertFalse(output.exists())
        self.assertFalse((self.root / "image.oci.tar").exists())
        self.assertEqual(list(self.root.glob("*.json")), [])

    def test_fast_context_cleanup_failure_removes_both_image_outputs(self):
        source = make_base(self.root)
        config = self.root / "config.json"
        with contextlib.redirect_stdout(io.StringIO()):
            fast.main(["init", "--base-tar", str(source), "--base-image", "example/base:1",
                       "--flavor", "tomcat", "--owner", "0:0", "--config", str(config)])
        war = self.root / "app.war"
        war.write_bytes(b"WAR content")
        output = self.root / "app.tar"
        with failing_cleanup("pyimagebuilder-fast-"):
            with self.assertRaisesRegex(OSError, "cleanup failed"):
                fast.quick_build(war, "app", "1", config, output=output, image_format="both",
                                 cache_dir=self.root / "cache")
        self.assertFalse(output.exists())
        self.assertFalse((self.root / "app.oci.tar").exists())
        self.assertEqual(war.read_bytes(), b"WAR content")

    def test_rootfs_and_optimizer_cleanup_failures_remove_outputs(self):
        source = make_base(self.root)
        rootfs = self.root / "rootfs.tar"
        export_rootfs(source, rootfs, tag="example/base:1")
        operations = [
            ("pyimagebuilder-rootfs-", lambda out: export_rootfs(source, out, tag="example/base:1")),
            ("pyimagebuilder-import-", lambda out: import_rootfs(rootfs, "app:1", out)),
            ("pyimagebuilder-flatten-", lambda out: flatten_image(source, out, tag="example/base:1")),
            ("pyimagebuilder-optimize-", lambda out: optimize_image(source, out, "example/base:1")),
        ]
        for prefix, operation in operations:
            with self.subTest(prefix=prefix):
                output = self.root / (prefix + ".tar")
                with failing_cleanup(prefix):
                    with self.assertRaisesRegex(OSError, "cleanup failed"):
                        operation(output)
                self.assertFalse(output.exists())
                operation(output)
                self.assertTrue(output.exists())

    def test_cleanup_attempts_other_products_and_restores_ref_after_unlink_failure(self):
        from file_publish import publication_transaction
        store = CASStore(self.root / "store")
        old, new = make_image(self.root, "old"), make_image(self.root, "new")
        store.import_docker(old, REFERENCE, PLATFORM)
        before = store.snapshot_ref(REFERENCE, PLATFORM, "local")
        first, second = self.root / "first-output", self.root / "second-output"
        unlink = file_publish.unlink_missing

        def fail_second(path):
            if path == second:
                raise OSError("second cannot be deleted")
            return unlink(path)

        with patch.object(file_publish, "unlink_missing", fail_second):
            with self.assertRaisesRegex(OSError, "cannot be deleted"):
                with publication_transaction(store, REFERENCE, PLATFORM, "local") as published:
                    for path in (first, second):
                        path.write_bytes(b"owned")
                        published.append(path)
                    store.import_docker(new, REFERENCE, PLATFORM, replace=True)
                    raise KeyboardInterrupt("cancel operation")
        self.assertFalse(first.exists())
        self.assertTrue(second.exists())
        self.assertEqual(store.snapshot_ref(REFERENCE, PLATFORM, "local"), before)


if __name__ == "__main__":
    unittest.main()
