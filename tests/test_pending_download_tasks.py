"""使用真实 Future 和线程池，核对失败后排队任务及临时目录的生命周期。"""

import contextlib
import io
import json
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import artifactory_download as downloader
import registry
from cas_store import CASStore
from errors import BuildError
from task_resources import cancel_pending_on_error
from test_registry_publication import digest, local_registry


class HeldExecutor:
    """暂停真实工作线程，在 shutdown 开始时记录 Future 状态，再放行收尾。

    基线没有取消逻辑时也会放行全部线程，因此测试不会靠超时或泄漏线程退出。
    第一项可独立放行，用于复现实际工作任务失败；后续任务等到 shutdown 才运行。
    """

    def __init__(self, fail_submit=None):
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.fail_submit = fail_submit
        self.first_gate = threading.Event()
        self.shutdown_gate = threading.Event()
        self.started = threading.Event()
        self.futures = []
        self.entered = []
        self.finished = []
        self.before_shutdown = None

    def __enter__(self):
        self.pool.__enter__()
        return self

    def submit(self, function, *args, **kwargs):
        index = len(self.futures)
        if index == self.fail_submit:
            raise OSError("submit failed")

        def run():
            self.entered.append(index)
            if index == 0:
                self.started.set()
                gate = self.first_gate
            else:
                gate = self.shutdown_gate
            if not gate.wait(5):
                raise AssertionError("worker was not released")
            try:
                return function(*args, **kwargs)
            finally:
                self.finished.append(index)

        future = self.pool.submit(run)
        self.futures.append(future)
        if index == 0 and not self.started.wait(5):
            raise AssertionError("worker did not start")
        return future

    def __exit__(self, *exception):
        self.before_shutdown = [(item.running(), item.cancelled(), item.done()) for item in self.futures]
        self.first_gate.set()
        self.shutdown_gate.set()
        return self.pool.__exit__(*exception)


class PendingDownloadTests(unittest.TestCase):
    def assert_cancelled_before_shutdown(self, executor, minimum):
        """先验证排队任务已取消，再验证启动过的工作线程确实完成收尾。"""
        self.assertGreaterEqual(sum(cancelled for _, cancelled, _ in executor.before_shutdown), minimum)
        self.assertTrue(all(running or done for running, _, done in executor.before_shutdown))
        self.assertEqual(sorted(executor.entered), sorted(executor.finished))
        self.assertTrue(all(future.done() for future in executor.futures))

    def registry_failure(self, phase, failure, submit_failure=False, worker_failure=False):
        held = HeldExecutor(fail_submit=2 if submit_failure else None)
        pools = []
        observed = []
        real_pool = registry.ThreadPoolExecutor
        real_completed = registry.as_completed

        def factory(**kwargs):
            current = len(pools)
            pools.append(current)
            return held if current == phase else real_pool(**kwargs)

        def completions(futures):
            if len(pools) - 1 == phase:
                if worker_failure:
                    held.first_gate.set()
                    return real_completed(futures)
                raise failure
            return real_completed(futures)

        with tempfile.TemporaryDirectory() as temporary, local_registry():
            root = Path(temporary)
            store = CASStore(root / "store")
            previous = store.snapshot_ref("example.test/p/app:1", "linux/amd64", "registry")
            selected = registry._selected_manifest
            blob = registry.RegistryClient.blob

            # config blob 的字节在 pull 中有摘要检查，使用真实配置构造多层输入。
            manifest, _ = selected(None, None)
            config_path = root / "config.json"
            blob(registry.RegistryClient("example.test/p/app:1"), manifest["config"], config_path)
            config = json.loads(config_path.read_text())
            config["rootfs"]["diff_ids"] *= 6
            config_raw = json.dumps(config).encode()
            manifest["config"] = dict(manifest["config"], digest=digest(config_raw), size=len(config_raw))
            manifest["layers"] *= 6
            manifest_raw = json.dumps(manifest).encode()

            class Client:
                def __init__(self, reference, *_args):
                    self.reference = registry.parse_reference(reference)
                    self.manifest_raw = manifest_raw

                def blob(self, descriptor, destination):
                    if descriptor["digest"] == digest(config_raw):
                        Path(destination).write_bytes(config_raw)
                    else:
                        observed.append(Path(destination).parent.exists())
                        if worker_failure and phase == 0:
                            raise failure
                        blob(self, descriptor, destination)

            with patch.object(registry, "RegistryClient", Client), \
                    patch.object(registry, "_selected_manifest", return_value=(manifest, digest(manifest_raw))), \
                    patch.object(registry, "ThreadPoolExecutor", side_effect=factory), \
                    patch.object(registry, "as_completed", side_effect=completions), \
                    patch.object(registry, "sha256_file", side_effect=failure if worker_failure and phase == 1
                                 else registry.sha256_file):
                expected = OSError if submit_failure else type(failure)
                with self.assertRaises(expected) as raised:
                    registry.pull("example.test/p/app:1", root / "output.tar", workers=1, cas_store=store)
                if not submit_failure:
                    self.assertIs(raised.exception, failure)
            self.assertFalse((root / "output.tar").exists())
            self.assertEqual(store.snapshot_ref("example.test/p/app:1", "linux/amd64", "registry"), previous)
        self.assertTrue(observed and all(observed))
        self.assert_cancelled_before_shutdown(held, 1 if submit_failure else (4 if worker_failure else 5))

    def test_registry_download_interrupt(self):
        self.registry_failure(0, KeyboardInterrupt())

    def test_registry_download_submission_failure(self):
        self.registry_failure(0, OSError(), submit_failure=True)

    def test_registry_download_worker_failure(self):
        self.registry_failure(0, BuildError("bad layer"), worker_failure=True)

    def test_registry_materialization_interrupt(self):
        self.registry_failure(1, KeyboardInterrupt())

    def test_registry_materialization_submission_failure(self):
        self.registry_failure(1, OSError(), submit_failure=True)

    def test_registry_materialization_worker_failure(self):
        self.registry_failure(1, BuildError("bad DiffID"), worker_failure=True)

    def test_artifactory_probe_interrupt_and_submission_failure(self):
        for submission in (False, True):
            with self.subTest(submission=submission):
                held = HeldExecutor(fail_submit=2 if submission else None)
                failure = KeyboardInterrupt()
                with patch.object(downloader, "ThreadPoolExecutor", return_value=held), \
                        patch.object(downloader, "probe_file_size", return_value=10), \
                        patch.object(downloader, "as_completed", side_effect=failure):
                    with self.assertRaises(OSError if submission else KeyboardInterrupt):
                        downloader.probe_file_sizes(object(), "https://example.test/", list("abcdef"))
                self.assert_cancelled_before_shutdown(held, 1 if submission else 5)

    def test_artifactory_download_failure_interrupt_and_partial_submission(self):
        for mode in ("interrupt", "submit", "worker"):
            submission = mode == "submit"
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                held = HeldExecutor(fail_submit=2 if submission else None)
                observed = []
                failure = BuildError("download failed") if mode == "worker" else KeyboardInterrupt()
                real_wait = downloader.wait

                def pending_results(*args, **kwargs):
                    if mode == "worker":
                        held.first_gate.set()
                        return real_wait(*args, **kwargs)
                    raise failure

                def transfer(_opener, _url, name, workspace, *_args):
                    observed.append(workspace.exists())
                    (workspace / name).write_bytes(b"x")
                    if mode == "worker":
                        raise failure

                store = CASStore(root / "store")
                previous = store.snapshot_ref("example.test/repo/base:1", "linux/amd64", "artifactory")
                with patch.object(downloader, "ThreadPoolExecutor", return_value=held), \
                        patch.object(downloader, "probe_file_sizes", return_value={name: 1 for name in "abcdef"}), \
                        patch.object(downloader, "download_one", side_effect=transfer), \
                        patch.object(downloader, "wait", side_effect=pending_results), \
                        contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(OSError if submission else type(failure)) as raised:
                        downloader.download_and_pack(object(), "https://example.test/", list("abcdef"),
                                                     root / "output.tar", 1, "example.test/repo/base:1",
                                                     cas_store=store)
                    if not submission:
                        self.assertIs(raised.exception, failure)
                self.assertTrue(observed and all(observed))
                self.assertFalse((root / "output.tar").exists())
                self.assertEqual(store.snapshot_ref("example.test/repo/base:1", "linux/amd64", "artifactory"),
                                 previous)
                self.assert_cancelled_before_shutdown(held, 1 if submission else (4 if mode == "worker" else 5))

    def test_normal_exit_preserves_all_tasks(self):
        results = []
        with ThreadPoolExecutor(max_workers=2) as executor, cancel_pending_on_error(executor) as submit:
            futures = [submit(results.append, index) for index in range(6)]
        self.assertEqual(sorted(results), list(range(6)))
        self.assertTrue(all(future.done() and not future.cancelled() for future in futures))

    def test_empty_group_keeps_original_exception(self):
        failure = KeyboardInterrupt()
        with ThreadPoolExecutor(max_workers=1) as executor:
            with self.assertRaises(KeyboardInterrupt) as raised:
                with cancel_pending_on_error(executor):
                    raise failure
        self.assertIs(raised.exception, failure)


if __name__ == "__main__":
    unittest.main()
