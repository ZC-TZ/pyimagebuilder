"""验证已有描述符接管失败后的回收，以及缓存/交付/密钥文件的失败清理。"""

import errno
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import artifactory_download
import attest
import file_publish
import image_reader
from cache import LayerCache
from cas_store import CASStore
from rootfs_materializer import RootFSMaterializer
from file_resources import fdopen_owned
from tests.test_cas_diff_binding import imported, layer
from tests.test_phase1 import make_base


class DescriptorOwnershipTests(unittest.TestCase):
    def assert_failed_adoption(self, operation, error, fail_at=1, allow_miss=False):
        descriptors, calls = [], []
        original = os.fdopen

        def fail(descriptor, *args, **kwargs):
            calls.append(descriptor)
            if len(calls) == fail_at:
                descriptors.append(descriptor)
                raise error
            return original(descriptor, *args, **kwargs)

        try:
            with patch.object(os, "fdopen", side_effect=fail):
                try:
                    operation()
                except BaseException as exc:
                    caught = exc
                else:
                    caught = None
            self.assertEqual(len(descriptors), 1, "Injected adoption failure was not reached")
            for descriptor in descriptors:
                try:
                    os.fstat(descriptor)
                except OSError as exc:
                    self.assertEqual(exc.errno, errno.EBADF)
                else:
                    self.fail("fdopen failure leaked descriptor {}".format(descriptor))
            if allow_miss and isinstance(error, OSError) and caught is None:
                return
            # 某些入口将 OSError 包装为 ArchiveError，但不能丢失原始异常链。
            seen = set()
            while caught is not None and id(caught) not in seen:
                if caught is error:
                    return
                seen.add(id(caught))
                caught = caught.__cause__ or caught.__context__
            self.fail("Original fdopen failure was not preserved")
        finally:
            # 未修复版本的复现也要释放真实描述符，避免污染下一项测试。
            for descriptor in descriptors:
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def check_case(self, prepare, fail_at=1, allow_miss=False):
        for error in (OSError(errno.ENOMEM, "stream adoption failed"), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                operation, check = prepare(root)
                self.assert_failed_adoption(operation, error, fail_at, allow_miss)
                self.assertEqual(list(root.rglob("*.part")), [])
                check()

    def test_instruction_layer_copy_failure_closes_temp_descriptor(self):
        def prepare(root):
            source = root / "layer.tar"
            source.write_bytes(layer(b"payload"))
            store = LayerCache(root / "cache")
            digest = "sha256:" + image_reader.sha256_file(source)
            return (lambda: store.store("key", digest, source),
                    lambda: self.assertFalse((store.entries / "key.json").exists()))
        self.check_case(prepare)

    def test_empty_instruction_entry_failure_closes_temp_descriptor(self):
        def prepare(root):
            store = LayerCache(root / "cache")
            return (lambda: store.store("key", None, None),
                    lambda: self.assertFalse((store.entries / "key.json").exists()))
        self.check_case(prepare)

    def test_instruction_identity_refresh_failure_preserves_entry(self):
        def prepare(root):
            source = root / "layer.tar"
            source.write_bytes(layer(b"payload"))
            store = LayerCache(root / "cache")
            store.store("key", "sha256:" + image_reader.sha256_file(source), source)
            entry = store.entries / "key.json"
            metadata = json.loads(entry.read_text(encoding="utf-8"))
            metadata["file_identity"] = None
            entry.write_text(json.dumps(metadata), encoding="utf-8")
            before = entry.read_bytes()
            return lambda: store.lookup("key"), lambda: self.assertEqual(entry.read_bytes(), before)
        self.check_case(prepare, allow_miss=True)

    def test_cas_blob_failure_closes_temp_descriptor(self):
        def prepare(root):
            store = CASStore(root / "store")
            return lambda: store._put_bytes(b"new blob"), lambda: self.assertEqual(list(store.blobs.iterdir()), [])
        self.check_case(prepare)

    def test_cas_ref_write_failure_preserves_existing_raw_ref(self):
        def prepare(root):
            store = CASStore(root / "store")
            value = store.import_docker(make_base(root), "example/base:1", "linux/amd64")
            before = store.snapshot_ref("example/base:1", "linux/amd64", "local")
            return (lambda: store._publish_ref(value, True),
                    lambda: self.assertEqual(store.snapshot_ref("example/base:1", "linux/amd64", "local"), before))
        self.check_case(prepare)

    def test_cas_ref_restore_failure_preserves_existing_raw_ref(self):
        def prepare(root):
            store = CASStore(root / "store")
            store.import_docker(make_base(root), "example/base:1", "linux/amd64")
            before = store.snapshot_ref("example/base:1", "linux/amd64", "local")
            return (lambda: store.restore_ref("example/base:1", "linux/amd64", "local", b"replacement snapshot"),
                    lambda: self.assertEqual(store.snapshot_ref("example/base:1", "linux/amd64", "local"), before))
        self.check_case(prepare)

    def test_cas_decode_failure_does_not_mark_layer_verified(self):
        def prepare(root):
            store = CASStore(root / "store")
            before = imported(root, store, layer(b"payload"))
            return (lambda: store.open_base("example/app:1", source="artifactory"),
                    lambda: self.assertEqual(store.resolve("example/app:1", source="artifactory"), before))
        self.check_case(prepare)

    def test_base_cache_entry_failure_does_not_publish_metadata(self):
        def prepare(root):
            store = root / "base-cache"
            reader = image_reader.ImageArchiveReader(make_base(root), root / "workspace", store)
            return (lambda: reader.read("example/base:1"),
                    lambda: self.assertEqual(list(store.rglob("*.json")), []))
        self.check_case(prepare)

    def test_cross_device_base_copy_failure_preserves_workspace_layer(self):
        def prepare(root):
            store, workspace = root / "base-cache", root / "workspace"
            reader = image_reader.ImageArchiveReader(make_base(root), workspace, store)
            replace = os.replace

            def cross_device(source, destination):
                if Path(source).parent == workspace and Path(destination).parent == store / "layers":
                    raise OSError(errno.EXDEV, "different filesystem")
                return replace(source, destination)

            def operation():
                with patch.object(os, "replace", side_effect=cross_device):
                    reader.read("example/base:1")

            def check():
                self.assertTrue((workspace / "base-layer-1.tar").is_file())
                self.assertEqual(list(store.rglob("*.tar")), [])
                self.assertEqual(list(store.rglob("*.json")), [])
            return operation, check
        self.check_case(prepare)

    def test_cross_device_delivery_failure_preserves_source(self):
        def prepare(root):
            source, target = root / "source", root / "output.tar"
            source.write_bytes(b"verified bytes")

            def operation():
                with patch.object(os, "link", side_effect=OSError(errno.EXDEV, "different filesystem")):
                    file_publish.publish_new_file(source, target)

            def check():
                self.assertEqual(source.read_bytes(), b"verified bytes")
                self.assertFalse(target.exists())
            return operation, check
        self.check_case(prepare)

    def test_artifact_pack_failure_removes_pending_archive(self):
        def prepare(root):
            files = root / "files"
            files.mkdir()
            (files / "payload").write_bytes(b"payload")
            output = root / "output.tar"
            display = SimpleNamespace(is_tty=False, status=lambda *_args: None, finish_line=lambda: None)
            return (lambda: artifactory_download.pack_files(files, output, display),
                    lambda: self.assertFalse(output.exists()))
        self.check_case(prepare)

    def test_rootfs_file_adoption_failure_closes_descriptor(self):
        def prepare(root):
            source = root / "layer.tar"
            source.write_bytes(layer(b"payload"))
            materializer = RootFSMaterializer(root / "rootfs")
            materializer._metadata = lambda *_args, **_kwargs: None
            return lambda: materializer.apply(source), lambda: None
        self.check_case(prepare)

    def test_key_adoption_failure_rolls_back_both_created_files(self):
        def prepare(root):
            private, public = root / "private.hex", root / "public.hex"

            def check():
                self.assertFalse(private.exists())
                self.assertFalse(public.exists())
            return lambda: attest.keygen(private, public), check
        for fail_at in (1, 2):
            with self.subTest(fail_at=fail_at):
                self.check_case(prepare, fail_at=fail_at)

    def test_key_public_open_interrupt_removes_completed_private_key(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            private, public = root / "private.hex", root / "public.hex"
            original = os.open
            error = KeyboardInterrupt()

            def interrupt_public(path, *args, **kwargs):
                if Path(path) == public:
                    raise error
                return original(path, *args, **kwargs)

            with patch.object(os, "open", side_effect=interrupt_public):
                with self.assertRaises(KeyboardInterrupt) as raised:
                    attest.keygen(private, public)
            self.assertIs(raised.exception, error)
            self.assertFalse(private.exists())
            self.assertFalse(public.exists())

    def test_key_public_conflict_preserves_other_owner_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            private, public = root / "private.hex", root / "public.hex"
            public.write_bytes(b"existing public key")
            with self.assertRaises(FileExistsError):
                attest.keygen(private, public)
            self.assertFalse(private.exists())
            self.assertEqual(public.read_bytes(), b"existing public key")


class OwnedDescriptorHelperTests(unittest.TestCase):
    def assert_closed(self, descriptor):
        with self.assertRaises(OSError) as closed:
            os.fstat(descriptor)
        self.assertEqual(closed.exception.errno, errno.EBADF)

    def test_native_fdopen_argument_failure_closes_descriptor(self):
        with tempfile.TemporaryDirectory() as temp:
            descriptor, path = tempfile.mkstemp(dir=temp)
            try:
                with self.assertRaises(ValueError):
                    fdopen_owned(descriptor, "invalid-mode")
                self.assert_closed(descriptor)
            finally:
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def test_success_transfers_ownership_to_stream(self):
        with tempfile.TemporaryDirectory() as temp:
            descriptor, path = tempfile.mkstemp(dir=temp)
            with fdopen_owned(descriptor, "w", encoding="utf-8") as stream:
                self.assertEqual(stream.fileno(), descriptor)
                stream.write("中文内容")
                os.fstat(descriptor)
            self.assert_closed(descriptor)
            self.assertEqual(Path(path).read_text(encoding="utf-8"), "中文内容")

    def test_body_failure_is_closed_by_callers_context(self):
        with tempfile.TemporaryDirectory() as temp:
            descriptor, path = tempfile.mkstemp(dir=temp)
            error = KeyboardInterrupt()
            with self.assertRaises(KeyboardInterrupt) as raised:
                with fdopen_owned(descriptor, "wb"):
                    raise error
            self.assertIs(raised.exception, error)
            self.assert_closed(descriptor)

    def test_close_error_does_not_replace_adoption_error(self):
        with tempfile.TemporaryDirectory() as temp:
            descriptor, path = tempfile.mkstemp(dir=temp)
            close = os.close
            error = OSError(errno.ENOMEM, "original adoption error")

            def reported_close(fd):
                close(fd)
                raise OSError(errno.EIO, "close reported failure")

            try:
                with patch.object(os, "fdopen", side_effect=error), \
                        patch.object(os, "close", side_effect=reported_close) as closing:
                    with self.assertRaises(OSError) as raised:
                        fdopen_owned(descriptor, "wb")
                    self.assertIs(raised.exception, error)
                    closing.assert_called_once_with(descriptor)
                self.assert_closed(descriptor)
            finally:
                try:
                    close(descriptor)
                except OSError:
                    pass

    def test_memory_error_during_adoption_closes_descriptor(self):
        with tempfile.TemporaryDirectory() as temp:
            descriptor, path = tempfile.mkstemp(dir=temp)
            error = MemoryError("file stream allocation failed")
            try:
                with patch.object(os, "fdopen", side_effect=error):
                    with self.assertRaises(MemoryError) as raised:
                        fdopen_owned(descriptor, "wb")
                    self.assertIs(raised.exception, error)
                self.assert_closed(descriptor)
            finally:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


if __name__ == "__main__":
    unittest.main()
