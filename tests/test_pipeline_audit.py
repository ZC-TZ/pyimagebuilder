"""复现层并发入库的身份竞争和异常收尾覆盖，避免依赖线程调度概率。"""

import io
import os
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import artifactory_download as artifacts
import cas_store
from cache_identity import stat_identity, _windows_change_time
from cas_store import CASStore
from cache_identity import FileIdentityChanged
from image_reader import LayerIdentityChanged
from errors import ArchiveError
from test_artifactory_publication import artifact_files, digest, download, layer_bytes, REFERENCE, PLATFORM, SOURCE


class PipelineAuditTests(unittest.TestCase):
    def test_existing_blob_retries_only_identity_changes_with_full_verification(self):
        for changed in (FileIdentityChanged, LayerIdentityChanged):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as temporary:
                store = CASStore(Path(temporary) / "store")
                raw = b"valid CAS content"
                target = store._put_stream(io.BytesIO(raw), digest(raw), len(raw))
                pending = Path(temporary) / "incoming"
                pending.write_bytes(raw)
                original = cas_store.verified_layer_identity
                attempts = []

                def verify(*args):
                    attempts.append(args)
                    if len(attempts) == 1:
                        raise changed("identity changed during read")
                    return original(*args)

                with patch.object(cas_store, "verified_layer_identity", side_effect=verify):
                    self.assertEqual(store._publish_blob(pending, digest(raw), len(raw)), target)
                self.assertEqual(len(attempts), 2)
                self.assertEqual(target.read_bytes(), raw)

    def test_unstable_existing_blob_has_bounded_retry_and_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = CASStore(Path(temporary) / "store")
            raw = b"valid CAS content"
            target = store._put_stream(io.BytesIO(raw), digest(raw), len(raw))
            pending = Path(temporary) / "incoming"
            pending.write_bytes(raw)
            with patch.object(cas_store, "verified_layer_identity",
                              side_effect=LayerIdentityChanged("still changing")) as verify:
                with self.assertRaisesRegex(LayerIdentityChanged, "still changing"):
                    store._publish_blob(pending, digest(raw), len(raw))
            self.assertEqual(verify.call_count, 3)
            self.assertEqual(target.read_bytes(), raw)

    def test_digest_error_is_not_treated_as_retryable_identity_change(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = CASStore(Path(temporary) / "store")
            raw = b"valid CAS content"
            target = store._put_stream(io.BytesIO(raw), digest(raw), len(raw))
            target.write_bytes(b"x" * len(raw))
            pending = Path(temporary) / "incoming"
            pending.write_bytes(raw)
            with patch.object(cas_store, "verified_layer_identity", wraps=cas_store.verified_layer_identity) as verify:
                with self.assertRaises(ArchiveError):
                    store._publish_blob(pending, digest(raw), len(raw))
            self.assertEqual(verify.call_count, 1)
            self.assertEqual(target.read_bytes(), b"x" * len(raw))

    def test_same_decoded_layer_publication_tolerates_winner_link_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = CASStore(root / "store")
            raw = layer_bytes(b"same layer")
            incoming = [root / "first.tar", root / "second.tar"]
            for path in incoming:
                path.write_bytes(raw)
            target = store._blob(digest(raw))
            published, inspecting, first_finished = (threading.Event() for _ in range(3))
            original_publish = cas_store.publish_new_file
            checks = []

            def publish(source, destination):
                result = original_publish(source, destination)
                if Path(destination) == target:
                    published.set()
                    self.assertTrue(inspecting.wait(4), "duplicate did not inspect the winner")
                return result

            def identity(path):
                # 用真实 ChangeTime 模拟 POSIX 身份，Windows 上也能复现 link/unlink 的竞争。
                path = Path(path)
                stamp = stat_identity(path.stat())
                if os.name == "nt":
                    with open(path, "rb") as stream:
                        change = _windows_change_time(stream)
                    if change is None:
                        raise AssertionError("Windows ChangeTime unavailable")
                    stamp[-1] = change
                if path == target:
                    checks.append(stamp)
                    if len(checks) == 1:
                        inspecting.set()
                        self.assertTrue(first_finished.wait(4), "winner did not clean its private link")
                return stamp

            with patch.object(cas_store, "publish_new_file", side_effect=publish), \
                    patch.object(cas_store, "file_identity", side_effect=identity), \
                    ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(store.store_decoded_layer, incoming[0], digest(raw), owned=True)
                first.add_done_callback(lambda _: first_finished.set())
                self.assertTrue(published.wait(4))
                second = executor.submit(store.store_decoded_layer, incoming[1], digest(raw), owned=True)
                self.assertEqual(first.result(timeout=5), target)
                self.assertEqual(second.result(timeout=5), target)
            self.assertGreaterEqual(len(checks), 3)
            self.assertEqual(target.read_bytes(), raw)
            self.assertFalse(list(store.blobs.glob("*.part")))

    def test_download_failure_is_not_replaced_by_terminal_cleanup_failure(self):
        class Display:
            is_tty = False

            def log(self, message):
                pass

            def status(self, message):
                pass

            def finish_line(self):
                raise OSError("terminal cleanup failed")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transfer_error = RuntimeError("original transfer failure")
            with patch.object(artifacts, "ProgressDisplay", return_value=Display()), \
                    patch.object(artifacts, "probe_file_sizes", return_value={"x": 1}), \
                    patch.object(artifacts, "download_one", side_effect=transfer_error):
                with self.assertRaises(RuntimeError) as raised:
                    artifacts.download_and_pack(object(), "https://example.test/", ["x"],
                                                root / "image.tar", 1, REFERENCE)
            self.assertIs(raised.exception, transfer_error)
            self.assertFalse((root / "image.tar").exists())

    def test_prepare_error_and_cleanup_error_restore_old_ref_and_preserve_original(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = CASStore(root / "store")
            files = artifact_files(layer_bytes(b"valid"))
            download(files, root / "unused.tar", store)
            previous = store.snapshot_ref(REFERENCE, PLATFORM, SOURCE)
            error = RuntimeError("original layer preparation failure")
            display = artifacts.ProgressDisplay()
            with patch.object(artifacts, "ProgressDisplay", return_value=display), \
                    patch.object(display, "finish_line", side_effect=OSError("terminal cleanup failed")), \
                    patch.object(artifacts, "materialize_layer", side_effect=error):
                with self.assertRaises(RuntimeError) as raised:
                    download(files, root / "unused.tar", store)
            self.assertIs(raised.exception, error)
            self.assertEqual(store.snapshot_ref(REFERENCE, PLATFORM, SOURCE), previous)
            self.assertFalse(list(store.root.parent.glob("artifactory_download_*")))

    def test_terminal_cleanup_failure_without_primary_error_still_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = CASStore(root / "store")
            display = artifacts.ProgressDisplay()
            with patch.object(artifacts, "ProgressDisplay", return_value=display), \
                    patch.object(display, "finish_line", side_effect=OSError("terminal cleanup failed")):
                with self.assertRaisesRegex(OSError, "terminal cleanup failed"):
                    download(artifact_files(layer_bytes(b"valid")), root / "unused.tar", store)
            self.assertFalse(store.has_ref(REFERENCE, PLATFORM, SOURCE))

    def test_cancellation_is_not_replaced_by_terminal_cleanup_failure(self):
        from concurrent.futures import CancelledError
        for error in (CancelledError("cancelled"), KeyboardInterrupt()):
            with self.subTest(error=type(error)), tempfile.TemporaryDirectory() as temporary:
                display = artifacts.ProgressDisplay()
                with patch.object(artifacts, "ProgressDisplay", return_value=display), \
                        patch.object(display, "finish_line", side_effect=OSError("terminal cleanup failed")), \
                        patch.object(artifacts, "probe_file_sizes", return_value={"x": 1}), \
                        patch.object(artifacts, "download_one", side_effect=error):
                    with self.assertRaises(type(error)) as raised:
                        artifacts.download_and_pack(object(), "https://example.test/", ["x"],
                            Path(temporary) / "out.tar", 1, REFERENCE)
                self.assertIs(raised.exception, error)


if __name__ == "__main__":
    unittest.main()
