"""验证 Registry 拉取接口发布 CAS 引用和归档产物时的失败事务。"""

import contextlib
import gzip
import hashlib
import io
import json
import sys
import tarfile
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import registry
import file_publish
from cas_store import CASStore
from errors import BuildError
from image_reader import ImageArchiveReader
from oci_writer import CONFIG_TYPE, MANIFEST_TYPE, OCIImageWriter
from test_ref_transactions import make_image


REFERENCE = "example.test/p/app:1"
PLATFORM = "linux/amd64"
SOURCE = "registry"


def digest(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


@contextlib.contextmanager
def local_registry():
    """只替换网络输入，层解压、摘要校验、CAS、归档和发布器均用真实实现。"""
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        member = tarfile.TarInfo("payload")
        payload = b"downloaded image"
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))
    raw = stream.getvalue()
    encoded = gzip.compress(raw)
    config_raw = (json.dumps({"os": "linux", "architecture": "amd64", "config": {},
                              "rootfs": {"type": "layers", "diff_ids": [digest(raw)]}},
                             indent=2) + "\n").encode()
    blobs = {digest(encoded): encoded, digest(config_raw): config_raw}
    manifest = {"schemaVersion": 2, "mediaType": MANIFEST_TYPE,
                "config": {"mediaType": CONFIG_TYPE, "digest": digest(config_raw), "size": len(config_raw)},
                "layers": [{"mediaType": registry.OCI_GZIP_LAYER, "digest": digest(encoded),
                            "size": len(encoded)}]}
    manifest_raw = (json.dumps(manifest, indent=4) + "\n").encode()

    class Client:
        def __init__(self, reference, *_args):
            self.reference = registry.parse_reference(reference)
            self.manifest_raw = manifest_raw

        def blob(self, descriptor, destination):
            Path(destination).write_bytes(blobs[descriptor["digest"]])

    with patch.object(registry, "RegistryClient", Client), \
            patch.object(registry, "_selected_manifest", return_value=(manifest, digest(manifest_raw))):
        yield config_raw, manifest_raw


class RegistryPublicationTests(unittest.TestCase):
    def test_failed_output_publication_restores_ref(self):
        for existing in (False, True):
            for image_format in ("docker", "oci", "both"):
                with self.subTest(existing=existing, image_format=image_format), \
                        tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    store = CASStore(root / "store")
                    if existing:
                        store.import_docker(make_image(root, "old"), REFERENCE, PLATFORM, SOURCE)
                    previous = store.snapshot_ref(REFERENCE, PLATFORM, SOURCE)
                    with local_registry(), patch.object(registry, "publish_new_file",
                                                        side_effect=OSError("publication interrupted")):
                        with self.assertRaisesRegex(OSError, "publication interrupted"):
                            registry.pull(REFERENCE, root / "image.tar", image_format=image_format,
                                          cas_store=store)
                    self.assertEqual(store.snapshot_ref(REFERENCE, PLATFORM, SOURCE), previous)
                    self.assertFalse((root / "image.tar").exists())

    def test_second_output_collision_keeps_foreign_file_and_rolls_back_first(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = CASStore(root / "store")
            store.import_docker(make_image(root, "old"), REFERENCE, PLATFORM, SOURCE)
            previous = store.snapshot_ref(REFERENCE, PLATFORM, SOURCE)
            output, second = root / "image.tar", root / "image.oci.tar"
            publish = registry.publish_new_file

            def racing_writer(source, destination):
                if Path(destination) == second:
                    second.write_bytes(b"other writer")
                return publish(source, destination)

            with local_registry(), patch.object(registry, "publish_new_file", side_effect=racing_writer):
                with self.assertRaises(FileExistsError):
                    registry.pull(REFERENCE, output, image_format="both", cas_store=store)
            self.assertFalse(output.exists())
            self.assertEqual(second.read_bytes(), b"other writer")
            self.assertEqual(store.snapshot_ref(REFERENCE, PLATFORM, SOURCE), previous)

    def test_cleanup_failure_still_restores_ref(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = CASStore(root / "store")
            store.import_docker(make_image(root, "old"), REFERENCE, PLATFORM, SOURCE)
            previous = store.snapshot_ref(REFERENCE, PLATFORM, SOURCE)
            output, second = root / "image.tar", root / "image.oci.tar"
            publish, unlink = registry.publish_new_file, file_publish.unlink_missing

            def fail_second(source, destination):
                if Path(destination) == second:
                    raise OSError("publication interrupted")
                return publish(source, destination)

            def fail_cleanup(path):
                if Path(path) == output:
                    raise OSError("cleanup interrupted")
                return unlink(path)

            with local_registry(), patch.object(registry, "publish_new_file", side_effect=fail_second), \
                    patch.object(file_publish, "unlink_missing", side_effect=fail_cleanup):
                with self.assertRaisesRegex(OSError, "cleanup interrupted"):
                    registry.pull(REFERENCE, output, image_format="both", cas_store=store)
            self.assertEqual(store.snapshot_ref(REFERENCE, PLATFORM, SOURCE), previous)

    def test_competing_update_is_blocked_until_rollback_finishes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = CASStore(root / "store")
            old, winner = make_image(root, "old"), make_image(root, "winner")
            store.import_docker(old, REFERENCE, PLATFORM, SOURCE)
            previous = store.snapshot_ref(REFERENCE, PLATFORM, SOURCE)
            ready, resume = threading.Event(), threading.Event()

            def fail_publication(*_args):
                ready.set()
                if not resume.wait(5):
                    raise AssertionError("test synchronization timed out")
                raise OSError("publication interrupted")

            with local_registry(), patch.object(registry, "publish_new_file", side_effect=fail_publication), \
                    ThreadPoolExecutor(max_workers=1) as executor:
                operation = executor.submit(registry.pull, REFERENCE, root / "image.tar", cas_store=store)
                try:
                    self.assertTrue(ready.wait(5))
                    with self.assertRaisesRegex(BuildError, "busy"):
                        CASStore(root / "store").import_docker(winner, REFERENCE, PLATFORM, SOURCE, replace=True)
                finally:
                    resume.set()
                with self.assertRaisesRegex(OSError, "publication interrupted"):
                    operation.result(timeout=5)
            self.assertEqual(store.snapshot_ref(REFERENCE, PLATFORM, SOURCE), previous)
            updated = store.import_docker(winner, REFERENCE, PLATFORM, SOURCE, replace=True)
            self.assertEqual(store.resolve(REFERENCE, PLATFORM, SOURCE), updated)

    def test_cas_only_error_after_import_restores_ref(self):
        for existing in (False, True):
            with self.subTest(existing=existing), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                store = CASStore(root / "store")
                if existing:
                    store.import_docker(make_image(root, "old"), REFERENCE, PLATFORM, SOURCE)
                previous = store.snapshot_ref(REFERENCE, PLATFORM, SOURCE)
                original = store.import_registry

                def interrupted(*args, **kwargs):
                    original(*args, **kwargs)
                    raise OSError("import interrupted")

                with local_registry(), patch.object(store, "import_registry", side_effect=interrupted):
                    with self.assertRaisesRegex(OSError, "import interrupted"):
                        registry.pull(REFERENCE, root / "unused.tar", cas_store=store, cas_only=True)
                self.assertEqual(store.snapshot_ref(REFERENCE, PLATFORM, SOURCE), previous)

    def test_successful_both_output_matches_cas_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = CASStore(root / "store")
            output = root / "image.tar"
            with local_registry() as (config_raw, manifest_raw):
                registry.pull(REFERENCE, output, image_format="both", cas_store=store)
            ref = store.resolve(REFERENCE, PLATFORM, SOURCE)
            self.assertEqual(store._blob(ref["manifest"]).read_bytes(), manifest_raw)
            image = ImageArchiveReader(output, root / "extracted").read(REFERENCE)
            self.assertEqual(image.config_raw, config_raw)
            self.assertEqual(ref["config"], digest(config_raw))
            OCIImageWriter().verify(root / "image.oci.tar", REFERENCE)
            self.assertTrue(ref["layers"][0]["diff_id_verified"])

    def test_publication_interrupt_rolls_back_with_or_without_cas(self):
        for has_store in (False, True):
            with self.subTest(has_store=has_store), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                store = CASStore(root / "store") if has_store else None
                if store is not None:
                    store.import_docker(make_image(root, "old"), REFERENCE, PLATFORM, SOURCE)
                previous = store.snapshot_ref(REFERENCE, PLATFORM, SOURCE) if store is not None else None
                output, second = root / "image.tar", root / "image.oci.tar"
                publish = registry.publish_new_file

                def interrupt_second(source, destination):
                    if Path(destination) == second:
                        raise KeyboardInterrupt("publication cancelled")
                    return publish(source, destination)

                with local_registry(), patch.object(registry, "publish_new_file", side_effect=interrupt_second):
                    with self.assertRaises(KeyboardInterrupt):
                        registry.pull(REFERENCE, output, image_format="both", cas_store=store)
                self.assertFalse(output.exists())
                self.assertFalse(second.exists())
                if store is not None:
                    self.assertEqual(store.snapshot_ref(REFERENCE, PLATFORM, SOURCE), previous)

    def test_successful_cas_only_does_not_create_archive(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = CASStore(root / "store")
            output = root / "unused.tar"
            with local_registry() as (config_raw, manifest_raw):
                registry.pull(REFERENCE, output, cas_store=store, cas_only=True)
            ref = store.resolve(REFERENCE, PLATFORM, SOURCE)
            self.assertEqual(store._blob(ref["manifest"]).read_bytes(), manifest_raw)
            self.assertEqual(store._blob(ref["config"]).read_bytes(), config_raw)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
