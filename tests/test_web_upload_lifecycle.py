"""上传暂存目录只由当前管理器回收，路径复用不能转移删除所有权。"""

import gc
import io
import shutil
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from web_uploads import FastUploads
from errors import BuildError


class UploadLifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="upload-lifecycle-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.uploads = FastUploads()
        self.addCleanup(self.uploads.close)

    def upload(self):
        with patch("web_uploads.tempfile.gettempdir", return_value=str(self.root)):
            token = self.uploads.receive(io.BytesIO(b"application"), 11, "app.war")
        return token, self.uploads.path(token)

    def replace_directory(self, path):
        directory = path.parent
        directory.rename(self.root / "original-upload")
        directory.mkdir()
        sentinel = directory / "keep.txt"
        sentinel.write_bytes(b"replacement data")
        return sentinel

    def test_discard_preserves_a_replacement_directory_and_keeps_failed_quota(self):
        token, path = self.upload()
        sentinel = self.replace_directory(path)
        with self.assertRaises(OSError):
            self.uploads.discard(token)
        self.assertEqual(sentinel.read_bytes(), b"replacement data")
        self.assertIn(token, self.uploads.entries)
        self.assertTrue(self.uploads.entries[token].discarded)

    def test_garbage_collection_cannot_delete_a_replacement_directory(self):
        _, path = self.upload()
        sentinel = self.replace_directory(path)
        self.uploads.entries.clear()
        gc.collect()
        self.assertEqual(sentinel.read_bytes(), b"replacement data")

    def test_missing_directory_releases_quota_and_finish_is_idempotent(self):
        token, path = self.upload()
        shutil.rmtree(path.parent)
        self.uploads.finish(token)
        self.uploads.finish(token)
        self.assertNotIn(token, self.uploads.entries)

    def test_unclaim_missing_token_preserves_original_submission_error(self):
        token, path = self.upload()
        self.uploads.finish(token)
        self.uploads.release([token], retry=True)
        self.assertFalse(path.parent.exists())

    def test_replaced_file_is_not_removed_or_reused(self):
        token, path = self.upload()
        path.rename(path.with_name("original.war"))
        path.write_bytes(b"replacement package")
        with self.assertRaises(BuildError):
            self.uploads.path(token)
        with self.assertRaises(OSError):
            self.uploads.finish(token)
        self.assertEqual(path.read_bytes(), b"replacement package")

    def test_replaced_directory_cannot_be_claimed(self):
        token, path = self.upload()
        sentinel = self.replace_directory(path)
        path.write_bytes(b"replacement package")
        with self.assertRaises(BuildError):
            self.uploads.claim({"package": (token, "package")})
        self.assertTrue(sentinel.exists())

    def test_unexpected_files_are_preserved_without_recursive_cleanup(self):
        token, path = self.upload()
        sentinel = path.parent / "keep.txt"
        sentinel.write_bytes(b"external file")
        with self.assertRaises(OSError):
            self.uploads.finish(token)
        self.assertEqual(sentinel.read_bytes(), b"external file")
        self.assertIn(token, self.uploads.entries)
        self.assertFalse(path.exists())
        sentinel.unlink()
        self.uploads.finish(token)
        self.assertFalse(path.parent.exists())

    def test_unknown_directory_identity_never_authorizes_deletion(self):
        token, path = self.upload()
        self.uploads.entries[token].identity = None
        with self.assertRaises(OSError):
            self.uploads.finish(token)
        self.assertEqual(path.read_bytes(), b"application")

    def test_failed_release_still_cleans_other_tokens(self):
        first, first_path = self.upload()
        second, second_path = self.upload()
        sentinel = self.replace_directory(first_path)
        with self.assertRaises(OSError):
            self.uploads.release([first, second])
        self.assertTrue(sentinel.exists())
        self.assertFalse(second_path.parent.exists())
        self.assertEqual(list(self.uploads.entries), [first])

    def test_close_keeps_claimed_upload_until_task_releases_it(self):
        token, path = self.upload()
        self.uploads.claim({"package": (token, "package")})
        self.uploads.close()
        self.assertEqual(path.read_bytes(), b"application")
        self.uploads.release([token])
        self.assertFalse(path.parent.exists())
        self.assertEqual(self.uploads.entries, {})

    def test_concurrent_finish_is_idempotent(self):
        token, path = self.upload()
        gate = threading.Barrier(2)

        def finish():
            gate.wait(5)
            self.uploads.finish(token)

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(finish) for _ in range(2)]
            for future in futures:
                future.result(timeout=5)
        self.assertFalse(path.parent.exists())
        self.assertNotIn(token, self.uploads.entries)

    def test_close_during_receive_waits_for_receiver_to_release_its_file(self):
        entered, proceed = threading.Event(), threading.Event()

        class PausedSource:
            def read1(self, size):
                entered.set()
                if not proceed.wait(5):
                    raise RuntimeError("upload test gate timed out")
                return b"application"

        with patch("web_uploads.tempfile.gettempdir", return_value=str(self.root)), \
                ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(self.uploads.receive, PausedSource(), 11, "app.war")
            try:
                self.assertTrue(entered.wait(5))
                self.uploads.close()
                self.assertEqual(len(self.uploads.entries), 1)
            finally:
                proceed.set()
            with self.assertRaises(BuildError):
                future.result(timeout=5)
        self.assertEqual(self.uploads.entries, {})
        self.assertEqual(list(self.root.iterdir()), [])

    def test_failed_receive_keeps_original_error_when_cleanup_also_fails(self):
        original = ValueError("original upload read failure")

        class FailedSource:
            def read1(self, size):
                raise original

        real_unlink = Path.unlink

        def denied(path, *args, **kwargs):
            if path.parent.parent == self.root:
                raise PermissionError("cleanup blocked")
            return real_unlink(path, *args, **kwargs)

        with patch("web_uploads.tempfile.gettempdir", return_value=str(self.root)), \
                patch.object(Path, "unlink", new=denied):
            with self.assertRaises(ValueError) as caught:
                self.uploads.receive(FailedSource(), 11, "app.war")
        self.assertIs(caught.exception, original)
        self.assertEqual(len(self.uploads.entries), 1)
        self.uploads.close()
        self.assertEqual(self.uploads.entries, {})


if __name__ == "__main__":
    unittest.main()
