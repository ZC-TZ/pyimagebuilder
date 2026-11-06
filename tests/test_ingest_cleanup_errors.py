"""清理失败不能盖住 CAS 的校验错误或取消；正常完成后的清理失败仍须报告。"""

import hashlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cas_store
from cas_store import CASStore
from errors import ArchiveError
from file_resources import cleanup_on_exit


class IngestCleanupErrorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = CASStore(self.root / "store")
        self.raw = b"valid incoming blob"
        self.digest = "sha256:" + hashlib.sha256(self.raw).hexdigest()

    def test_owned_verification_error_survives_cleanup_error(self):
        source = self.root / "incoming.blob"
        source.write_bytes(b"wrong incoming blob")
        with patch.object(cas_store, "unlink_missing", side_effect=OSError("cleanup failed")):
            with self.assertRaises(ArchiveError):
                self.store._store_owned_blob(source, self.digest, len(self.raw))
        self.assertFalse(self.store._blob(self.digest).exists())

    def test_owned_cancellation_survives_cleanup_error(self):
        source = self.root / "incoming.blob"
        source.write_bytes(self.raw)
        cancelled = KeyboardInterrupt("cancelled by caller")
        with patch.object(cas_store, "verified_layer_identity", side_effect=cancelled), \
                patch.object(cas_store, "unlink_missing", side_effect=OSError("cleanup failed")):
            with self.assertRaises(KeyboardInterrupt) as caught:
                self.store._store_owned_blob(source, self.digest, len(self.raw))
        self.assertIs(caught.exception, cancelled)
        self.assertFalse(self.store._blob(self.digest).exists())

    def test_stream_read_error_survives_cleanup_error(self):
        error = OSError("original read failed")

        class BrokenInput:
            def read(self, _size):
                raise error

        with patch.object(cas_store, "unlink_missing", side_effect=OSError("cleanup failed")):
            with self.assertRaises(OSError) as caught:
                self.store._put_stream(BrokenInput(), self.digest, len(self.raw))
        self.assertIs(caught.exception, error)

    def test_cleanup_error_after_success_is_not_suppressed(self):
        for owned in (False, True):
            with self.subTest(owned=owned):
                store = CASStore(self.root / str(owned))
                source = self.root / "incoming.blob"
                source.write_bytes(self.raw)
                error = OSError("success cleanup failed")
                with patch.object(cas_store, "unlink_missing", side_effect=error):
                    with self.assertRaises(OSError) as caught:
                        if owned:
                            store._store_owned_blob(source, self.digest, len(self.raw))
                        else:
                            store._put_stream(io.BytesIO(self.raw), self.digest, len(self.raw))
                self.assertIs(caught.exception, error)
                self.assertEqual(store._blob(self.digest).read_bytes(), self.raw)

    def test_success_inside_callers_exception_still_reports_cleanup_failure(self):
        error = OSError("cleanup failed inside caller exception handler")

        def fail():
            raise error

        try:
            raise ValueError("an unrelated error handled by the caller")
        except ValueError:
            with self.assertRaises(OSError) as caught:
                with cleanup_on_exit(fail):
                    pass
        self.assertIs(caught.exception, error)

    def test_ref_write_and_restore_keep_primary_error_and_previous_bytes(self):
        value = {"reference": "base:1", "platform": "linux/amd64", "source": "local",
                 "manifest": self.digest}
        self.store._publish_ref(value, True)
        previous = self.store.snapshot_ref("base:1", "linux/amd64", "local")
        for restore in (False, True):
            with self.subTest(restore=restore):
                error = OSError("original flush failed")
                with patch.object(cas_store.os, "fsync", side_effect=error), \
                        patch.object(cas_store, "unlink_missing", side_effect=OSError("cleanup failed")):
                    with self.assertRaises(OSError) as caught:
                        if restore:
                            self.store._restore_ref_locked("base:1", "linux/amd64", "local", previous)
                        else:
                            self.store._publish_ref(value, True)
                self.assertIs(caught.exception, error)
                self.assertEqual(self.store.snapshot_ref("base:1", "linux/amd64", "local"), previous)


if __name__ == "__main__":
    unittest.main()
