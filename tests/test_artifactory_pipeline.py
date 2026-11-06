"""验证层准备与下载重叠、并发上限、顺序及失败后的线程/引用收尾。"""

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

import artifactory_download as artifacts
from cas_store import CASStore
from progress import make_reporter
from test_layer_progress import fixture
from test_artifactory_publication import REFERENCE, PLATFORM, SOURCE, download


class ArtifactoryPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = CASStore(self.root / "store")
        self.files = fixture(5)
        manifest = json.loads(self.files["manifest.json"])
        self.layer_names = ["sha256__" + item["digest"][7:] for item in manifest["layers"]]
        config_key = "sha256__" + manifest["config"]["digest"][7:]
        self.diff_ids = json.loads(self.files[config_key])["rootfs"]["diff_ids"]
        self.config_key = config_key

    def run_download(self, transfer, reporter=None, sizes=None):
        with patch.object(artifacts, "probe_file_sizes", return_value={
                name: len(raw) for name, raw in self.files.items()} if sizes is None else sizes), \
                patch.object(artifacts, "download_one", side_effect=transfer):
            artifacts.download_and_pack(object(), "https://example.test/repo/base/1/", list(self.files),
                self.root / "unused.tar", 2, REFERENCE, cas_store=self.store, cas_only=True, reporter=reporter)

    def transfer(self, _opener, _url, name, directory, _size, limiter, stats, _display):
        limiter.check_cancelled()
        (directory / name).write_bytes(self.files[name])
        stats.add(len(self.files[name]))

    def test_metadata_first_and_ready_layer_prepared_before_tail_download_completes(self):
        prepared = threading.Event()
        calls, completed = [], []
        slow = self.layer_names[0]
        original = self.store.store_decoded_layer

        def store(path, diff_id, **kwargs):
            result = original(path, diff_id, **kwargs)
            completed.append(diff_id)
            prepared.set()
            return result

        def transfer(*args):
            name = args[2]
            calls.append(name)
            if name == slow:
                self.assertTrue(prepared.wait(4), "layer preparation waited for the entire download")
            self.transfer(*args)

        with patch.object(self.store, "store_decoded_layer", side_effect=store):
            self.run_download(transfer)
        self.assertEqual(calls[:2], ["manifest.json", self.config_key])
        self.assertNotEqual(completed[0], self.diff_ids[0])
        image = self.store.open_base(REFERENCE, PLATFORM, SOURCE)
        self.assertEqual(image.config["rootfs"]["diff_ids"], self.diff_ids)
        self.assertEqual([path.name for path in image.layers], [value[7:] for value in self.diff_ids])

    def test_prepare_pool_has_two_workers_and_does_not_claim_partial_ref(self):
        lock = threading.Lock()
        two_running, release = threading.Event(), threading.Event()
        state = {"active": 0, "peak": 0}
        original = artifacts.materialize_layer

        def materialize(*args, **kwargs):
            with lock:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
                if state["active"] == 2:
                    two_running.set()
            try:
                self.assertTrue(release.wait(4))
                return original(*args, **kwargs)
            finally:
                with lock:
                    state["active"] -= 1

        with patch.object(artifacts, "materialize_layer", side_effect=materialize), \
                ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(self.run_download, self.transfer)
            try:
                self.assertTrue(two_running.wait(4))
                self.assertFalse(self.store.has_ref(REFERENCE, PLATFORM, SOURCE))
            finally:
                release.set()
            future.result(timeout=5)
        self.assertEqual(state, {"active": 0, "peak": 2})

    def test_preparation_error_cancels_downloads_and_preserves_previous_ref(self):
        download(self.files, self.root / "initial.tar", self.store)
        previous = self.store.snapshot_ref(REFERENCE, PLATFORM, SOURCE)
        failed, worker_stopped = threading.Event(), threading.Event()
        bad = self.layer_names[0]
        original = artifacts.materialize_layer

        def materialize(blob, *args, **kwargs):
            if blob.name == bad:
                failed.set()
                raise RuntimeError("test preparation failure")
            return original(blob, *args, **kwargs)

        def transfer(*args):
            if args[2] in self.layer_names and args[2] != bad:
                try:
                    self.assertTrue(failed.wait(4))
                    limiter = args[5]
                    # 以协作取消信号退出，验证失败主线程会先发信号再等待线程池。
                    for _ in range(400):
                        limiter.check_cancelled()
                        threading.Event().wait(0.01)
                    self.fail("download did not receive cancellation")
                finally:
                    worker_stopped.set()
            return self.transfer(*args)

        with patch.object(artifacts, "materialize_layer", side_effect=materialize):
            with self.assertRaisesRegex(RuntimeError, "test preparation failure"):
                sizes = {name: len(raw) for name, raw in self.files.items()}
                sizes[bad] = max(sizes.values()) + 1
                self.run_download(transfer, sizes=sizes)
        self.assertTrue(worker_stopped.is_set())
        self.assertEqual(self.store.snapshot_ref(REFERENCE, PLATFORM, SOURCE), previous)
        self.assertFalse(list(self.store.root.parent.glob("artifactory_download_*")))
        self.assertFalse((self.root / "unused.tar").exists())

    def test_bad_config_fails_before_any_layer_download(self):
        self.files[self.config_key] = b"{}"
        calls = []

        def transfer(*args):
            calls.append(args[2])
            return self.transfer(*args)

        with self.assertRaisesRegex(RuntimeError, "摘要校验失败"):
            self.run_download(transfer)
        self.assertEqual(calls, ["manifest.json", self.config_key])
        self.assertFalse(self.store.has_ref(REFERENCE, PLATFORM, SOURCE))

    def test_duplicate_layer_digest_uses_unique_work_files_and_keeps_manifest_positions(self):
        manifest = json.loads(self.files["manifest.json"])
        config = json.loads(self.files.pop(self.config_key))
        manifest["layers"].append(manifest["layers"][0])
        config["rootfs"]["diff_ids"].append(config["rootfs"]["diff_ids"][0])
        from test_artifactory_publication import digest
        raw = json.dumps(config).encode()
        key = "sha256__" + digest(raw)[7:]
        manifest["config"] = {"digest": digest(raw), "size": len(raw)}
        self.files[key], self.files["manifest.json"] = raw, json.dumps(manifest).encode()
        self.run_download(self.transfer)
        image = self.store.open_base(REFERENCE, PLATFORM, SOURCE)
        self.assertEqual(len(image.layers), 6)
        self.assertEqual(image.layers[0], image.layers[-1])

    def test_empty_image_and_separate_overlap_timings(self):
        self.files = fixture(0)
        stream = io.StringIO()
        reporter = make_reporter(progress="json", stream=stream)
        self.addCleanup(reporter.close)
        self.run_download(self.transfer, reporter)
        image = self.store.open_base(REFERENCE, PLATFORM, SOURCE)
        self.assertEqual(image.layers, [])
        self.assertIn("Wait for Artifactory layers", reporter.timings)
        self.assertIn("Import Artifactory CAS blobs", reporter.timings)
        self.assertIn("Prepare Artifactory layers (overlap)", reporter.timings)


if __name__ == "__main__":
    unittest.main()
