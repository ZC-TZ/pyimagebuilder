"""验证并发镜像更新时的引用发布、失败恢复与覆盖规则。"""

import io
import errno
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cas_store
import image_cli
import image_store
import store_lock
from cas_store import CASStore
from errors import BuildError
from image_reader import sha256_file
from image_store import _archive_path, pull_image
from image_writer import ImageArchiveWriter


REFERENCE = "example.test/p/app:1"
PLATFORM = "linux/amd64"


def make_image(root, name, reference=REFERENCE):
    layer = root / (name + "-layer.tar")
    payload = name.encode()
    with tarfile.open(layer, "w") as archive:
        member = tarfile.TarInfo("payload")
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))
    config = {"os": "linux", "architecture": "amd64", "config": {},
              "rootfs": {"type": "layers", "diff_ids": ["sha256:" + sha256_file(layer)]}}
    output = root / (name + ".tar")
    ImageArchiveWriter().write_new(output, config, [layer], reference)
    return output


class RefTransactionTests(unittest.TestCase):
    def test_artifactory_archive_failure_restores_ref_and_releases_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old, attempted, winner = [make_image(root, name) for name in ("old", "attempted", "winner")]
            store_root = root / "store"
            store = CASStore(store_root)
            store.import_docker(old, REFERENCE, PLATFORM, "artifactory")
            target = _archive_path(store_root, REFERENCE, PLATFORM, "artifactory")
            shutil.copyfile(old, target)
            previous = store.snapshot_ref(REFERENCE, PLATFORM, "artifactory")
            ready, resume = threading.Event(), threading.Event()
            replace = os.replace

            def download(opener, url, files, output, workers, reference, cas_store=None,
                         target_platform=PLATFORM, cas_only=False):
                shutil.copyfile(attempted, output)
                cas_store.import_docker(attempted, reference, target_platform, "artifactory", replace=True)

            def fail_publication(source, destination):
                if Path(destination) == target:
                    ready.set()
                    if not resume.wait(5):
                        raise AssertionError("test synchronization timed out")
                    raise OSError("archive publication interrupted")
                return replace(source, destination)

            with patch.object(image_store, "make_opener", return_value=object()), \
                    patch.object(image_store, "list_files", return_value=[]), \
                    patch.object(image_store, "download_and_pack", download), \
                    patch.object(image_store.os, "replace", side_effect=fail_publication), \
                    ThreadPoolExecutor(max_workers=1) as executor:
                operation = executor.submit(pull_image, REFERENCE, store=store_root,
                                            source="artifactory", refresh=True,
                                            password_env="PYIMAGEBUILDER_TEST_NO_PASSWORD")
                try:
                    self.assertTrue(ready.wait(5))
                    with self.assertRaisesRegex(BuildError, "busy"):
                        CASStore(store_root).import_docker(winner, REFERENCE, PLATFORM,
                                                          "artifactory", replace=True)
                finally:
                    resume.set()
                with self.assertRaisesRegex(OSError, "archive publication interrupted"):
                    operation.result(timeout=5)
            self.assertEqual(store.snapshot_ref(REFERENCE, PLATFORM, "artifactory"), previous)
            self.assertEqual(sha256_file(target), sha256_file(old))
            path, downloaded = pull_image(REFERENCE, store=store_root, source="artifactory", offline=True)
            self.assertFalse(downloaded)
            self.assertEqual(path, target)

    def test_failed_pull_cannot_undo_competing_publish(self):
        for existing in (False, True):
            for published in (False, True):
                with self.subTest(existing=existing, published=published), \
                        tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    old, attempted, winner = [make_image(root, name) for name in ("old", "attempted", "winner")]
                    store_root = root / "store"
                    store = CASStore(store_root)
                    if existing:
                        store.import_docker(old, REFERENCE, PLATFORM, "registry")
                    previous = store.snapshot_ref(REFERENCE, PLATFORM, "registry")
                    ready, resume = threading.Event(), threading.Event()

                    def download(reference, output, *args, cas_store=None, cas_only=False, **kwargs):
                        if published:
                            cas_store.import_docker(attempted, reference, PLATFORM, "registry", replace=True)
                        ready.set()
                        if not resume.wait(5):
                            raise AssertionError("test synchronization timed out")
                        raise BuildError("download interrupted")

                    with patch.object(image_store, "registry_pull", download), \
                            ThreadPoolExecutor(max_workers=1) as executor:
                        operation = executor.submit(pull_image, REFERENCE, store=store_root,
                                                    refresh=True, materialize_tar=False,
                                                    password_env="PYIMAGEBUILDER_TEST_NO_PASSWORD")
                        competing = None
                        try:
                            self.assertTrue(ready.wait(5))
                            try:
                                value = CASStore(store_root).import_docker(
                                    winner, REFERENCE, PLATFORM, "registry", replace=True)
                                competing = value["manifest"]
                            except BuildError as exc:
                                self.assertIn("busy", str(exc).lower())
                        finally:
                            resume.set()
                        with self.assertRaisesRegex(BuildError, "download interrupted"):
                            operation.result(timeout=5)
                    current = store.snapshot_ref(REFERENCE, PLATFORM, "registry")
                    if competing is not None:
                        self.assertIsNotNone(current, "failed pull removed a competing successful ref")
                        self.assertEqual(json.loads(current)["manifest"], competing,
                                         "failed pull reverted a competing successful ref")
                    else:
                        self.assertEqual(current, previous)
                    # 失败后必须释放锁，让后续合法更新能够成功。
                    winner_ref = store.import_docker(winner, REFERENCE, PLATFORM, "registry", replace=True)
                    self.assertEqual(store.resolve(REFERENCE, source="registry"), winner_ref)

    def test_failed_load_cannot_undo_competing_publish(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old, attempted, winner = [make_image(root, name) for name in ("old", "attempted", "winner")]
            store_root = root / "store"
            image_cli.load_archive(old, store_root)
            store = CASStore(store_root)
            previous = store.snapshot_ref(REFERENCE, PLATFORM, "local")
            target = _archive_path(store_root, REFERENCE, PLATFORM, "local")
            old_digest = sha256_file(target)
            ready, resume = threading.Event(), threading.Event()
            replace = os.replace

            def fail_publication(source, destination):
                if Path(destination) == target:
                    ready.set()
                    if not resume.wait(5):
                        raise AssertionError("test synchronization timed out")
                    raise OSError("archive publication interrupted")
                return replace(source, destination)

            with patch.object(image_cli.os, "replace", side_effect=fail_publication), \
                    ThreadPoolExecutor(max_workers=1) as executor:
                operation = executor.submit(image_cli.load_archive, attempted, store_root, replace=True)
                competing = None
                try:
                    self.assertTrue(ready.wait(5))
                    try:
                        value = CASStore(store_root).import_docker(winner, REFERENCE, PLATFORM, replace=True)
                        competing = value["manifest"]
                    except BuildError as exc:
                        self.assertIn("busy", str(exc).lower())
                finally:
                    resume.set()
                with self.assertRaisesRegex(OSError, "archive publication interrupted"):
                    operation.result(timeout=5)
            current = store.snapshot_ref(REFERENCE, PLATFORM, "local")
            if competing is not None:
                self.assertEqual(json.loads(current)["manifest"], competing,
                                 "failed load reverted a competing successful ref")
            else:
                self.assertEqual(current, previous)
            self.assertEqual(sha256_file(target), old_digest)
            image_cli.load_archive(winner, store_root, replace=True)
            self.assertEqual(sha256_file(target), sha256_file(winner))
            self.assertEqual(store.resolve(REFERENCE, source="local")["config"],
                             CASStore(root / "expected").import_docker(winner, REFERENCE, PLATFORM)["config"])

    def test_parallel_first_import_requires_explicit_replace(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first, second = make_image(root, "first"), make_image(root, "second")
            store_root = root / "store"
            store = CASStore(store_root)
            destination = store._ref(REFERENCE, PLATFORM, "local")
            ready, resume = threading.Event(), threading.Event()
            replace = os.replace
            first_thread = []

            def pause_publication(source, target):
                if Path(target) == destination and threading.get_ident() == first_thread[0]:
                    ready.set()
                    if not resume.wait(5):
                        raise AssertionError("test synchronization timed out")
                return replace(source, target)

            def publish_first():
                first_thread.append(threading.get_ident())
                return CASStore(store_root).import_docker(first, REFERENCE, PLATFORM)

            with patch.object(cas_store.os, "replace", side_effect=pause_publication), \
                    ThreadPoolExecutor(max_workers=1) as executor:
                operation = executor.submit(publish_first)
                second_error = None
                try:
                    self.assertTrue(ready.wait(5))
                    try:
                        CASStore(store_root).import_docker(second, REFERENCE, PLATFORM)
                    except BuildError as exc:
                        second_error = exc
                finally:
                    resume.set()
                first_ref = operation.result(timeout=5)
            self.assertIsNotNone(second_error, "both conflicting imports succeeded without --replace")
            self.assertEqual(store.resolve(REFERENCE, source="local"), first_ref)
            with self.assertRaisesRegex(BuildError, "conflicts"):
                store.import_docker(second, REFERENCE, PLATFORM)
            replacement = store.import_docker(second, REFERENCE, PLATFORM, replace=True)
            self.assertEqual(store.resolve(REFERENCE, source="local"), replacement)

    def test_lock_is_reentrant_and_excludes_independent_processes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = CASStore(root / "store")
            code = ("import sys; from cas_store import CASStore; from errors import BuildError\n"
                    "try:\n"
                    " with CASStore(sys.argv[1]).ref_lock(sys.argv[2], 'linux/amd64', 'local'):\n"
                    "  pass\n"
                    "except BuildError as exc:\n"
                    " print(str(exc)); sys.exit(7)\n")

            def child(script=code):
                return subprocess.run([sys.executable, "-c", script, str(root / "store"), REFERENCE],
                                      cwd=str(Path(__file__).resolve().parents[1]),
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      universal_newlines=True, timeout=10)

            with store.ref_lock(REFERENCE, PLATFORM, "local"):
                # 不同 CASStore 对象的同线程嵌套仍属于同一操作，不能再次争用内核锁。
                with CASStore(root / "store").ref_lock(REFERENCE, PLATFORM, "local"):
                    result = child()
                    self.assertEqual(result.returncode, 7, result.stderr)
                    self.assertIn("busy", result.stdout)
            self.assertEqual(child().returncode, 0)
            crashed = child("import os, sys; from cas_store import CASStore\n"
                            "with CASStore(sys.argv[1]).ref_lock(sys.argv[2], 'linux/amd64', 'local'):\n"
                            " os._exit(23)\n")
            self.assertEqual(crashed.returncode, 23)
            self.assertTrue(list(store.locks.glob("*.lock")))
            # 锁文件仍在，但异常退出后内核已释放锁，不需要手工删文件。
            self.assertEqual(child().returncode, 0)

    def test_different_references_and_sources_can_update_concurrently(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = make_image(root, "first")
            other_reference = "example.test/p/other:1"
            second = make_image(root, "second", other_reference)
            store_root = root / "store"
            store = CASStore(store_root)
            with store.ref_lock(REFERENCE, PLATFORM, "local"), ThreadPoolExecutor(max_workers=2) as executor:
                # 其他引用和其他来源拥有独立的锁，不应被当前引用阻塞。
                other = executor.submit(CASStore(store_root).import_docker,
                                        second, other_reference, PLATFORM)
                registry = executor.submit(CASStore(store_root).import_docker,
                                           first, REFERENCE, PLATFORM, "registry")
                other_ref, registry_ref = other.result(timeout=5), registry.result(timeout=5)
                busy = executor.submit(CASStore(store_root).import_docker, first, REFERENCE, PLATFORM)
                with self.assertRaisesRegex(BuildError, "busy"):
                    busy.result(timeout=5)
            self.assertEqual(store.resolve(other_reference, source="local"), other_ref)
            self.assertEqual(store.resolve(REFERENCE, source="registry"), registry_ref)

    def test_failed_lock_acquisition_releases_thread_guard(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = CASStore(Path(temporary) / "store")
            with patch.object(store_lock, "_lock_file", side_effect=OSError(errno.ENOSPC, "lock failure")):
                with self.assertRaisesRegex(OSError, "lock failure"):
                    with store.ref_lock(REFERENCE, PLATFORM, "local"):
                        self.fail("failed acquisition entered protected operation")
            with ThreadPoolExecutor(max_workers=1) as executor:
                def retry():
                    with store.ref_lock(REFERENCE, PLATFORM, "local"):
                        return True
                self.assertTrue(executor.submit(retry).result(timeout=5))

    def test_concurrent_lazy_archive_export_is_exclusive(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = make_image(root, "first")
            store_root = root / "store"
            store = CASStore(store_root)
            store.import_docker(source, REFERENCE, PLATFORM)
            ready, resume = threading.Event(), threading.Event()
            export = CASStore.export_docker
            first_thread = []

            def pause_export(cas, *args, **kwargs):
                if threading.get_ident() == first_thread[0]:
                    ready.set()
                    if not resume.wait(5):
                        raise AssertionError("test synchronization timed out")
                return export(cas, *args, **kwargs)

            def first_export():
                first_thread.append(threading.get_ident())
                return image_store.locate_archive(store_root, REFERENCE, PLATFORM)

            with patch.object(CASStore, "export_docker", pause_export), \
                    ThreadPoolExecutor(max_workers=1) as executor:
                operation = executor.submit(first_export)
                try:
                    self.assertTrue(ready.wait(5))
                    with self.assertRaisesRegex(BuildError, "busy"):
                        image_store.locate_archive(store_root, REFERENCE, PLATFORM)
                finally:
                    resume.set()
                kind, archive = operation.result(timeout=5)
            self.assertEqual(kind, "local")
            self.assertTrue(archive.is_file())
            self.assertEqual(image_store.locate_archive(store_root, REFERENCE, PLATFORM), (kind, archive))


if __name__ == "__main__":
    unittest.main()
