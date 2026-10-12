"""验证缓存输入稳定性、并发更新、结构审计和镜像刷新中断恢复。"""

import json
import errno
import os
import shutil
import sys
import tarfile
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cache as cache_module
import artifactory_download
import image_cli
import file_publish
import image_store
from builder import build
from cache import CACHE_VERSION, LayerCache
from cas_store import CASStore
from errors import ArchiveError, BuildError
from image_reader import ImageArchiveReader, sha256_file
from image_writer import ImageArchiveWriter
from layer import LayerBuilder
from optimizer import audit_cache, prune_cache
from rootfs import RootFSIndex
from tests.test_phase1 import make_base, layer_file


class CacheConsistencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_changed_copy_input_cannot_publish_under_previous_fingerprint(self):
        for op in ("COPY", "ADD"):
            for flag in ("", "--link "):
                with self.subTest(op=op, flag=flag):
                    context = self.root / (op + ("-link" if flag else "-plain"))
                    context.mkdir()
                    payload = context / "app.war"
                    payload.write_bytes(b"original")
                    stamp = payload.stat().st_mtime_ns
                    recipe = context / "Dockerfile"
                    recipe.write_text("FROM scratch\n" + op + " " + flag +
                                      "app.war /app.war\n", encoding="utf-8")
                    directory = self.root / (context.name + "-cache")
                    output = self.root / (context.name + ".tar")
                    transfer = LayerBuilder.transfer_layer

                    def change_then_transfer(builder, *args, **kwargs):
                        payload.write_bytes(b"modified")
                        return transfer(builder, *args, **kwargs)

                    with patch.object(LayerBuilder, "transfer_layer", change_then_transfer):
                        with self.assertRaisesRegex(BuildError, "changed"):
                            build(recipe, context, None, None, "app:1", output,
                                  cache_dir=directory)
                    self.assertFalse(output.exists())
                    self.assertEqual(list((directory / "entries").glob("*.json")), [])
                    payload.write_bytes(b"original")
                    os.utime(payload, ns=(stamp, stamp))
                    for number in range(2):
                        stats = {}
                        target = self.root / (context.name + "-{}.tar".format(number))
                        build(recipe, context, None, None, "app:1", target,
                              cache_dir=directory, cache_stats=stats)
                        image = ImageArchiveReader(target, self.root / (target.stem + "-read")).read("app:1")
                        index = RootFSIndex()
                        for layer in image.layers:
                            index.apply_layer(layer)
                        self.assertEqual(index.read_file("app.war"), b"original")
                        self.assertEqual(stats["hits"], number)

    def test_changed_base_archive_cannot_receive_trusted_identity(self):
        archive = make_base(self.root)
        cache_root = self.root / "base-cache"
        with tarfile.open(archive) as source:
            offset = source.getmember("one/layer.tar").offset_data
        altered = bytearray(archive.read_bytes())
        position = altered.index(b"base", offset)
        altered[position:position + 4] = b"evil"
        publish = ImageArchiveReader._publish_layer
        changed = []

        def alter_before_publication(reader, *args):
            if not changed:
                changed.append(True)
                stamp = archive.stat().st_mtime_ns
                archive.write_bytes(altered)
                # Windows 时钟粒度可能合并连续写入；明确改变身份，避免用例依赖时钟调度。
                os.utime(archive, ns=(stamp + 1000000000, stamp + 1000000000))
            return publish(reader, *args)

        with patch.object(ImageArchiveReader, "_publish_layer", alter_before_publication):
            with self.assertRaisesRegex(ArchiveError, "changed"):
                ImageArchiveReader(archive, self.root / "first", cache_root).read("example/base:1")
        self.assertEqual(list(cache_root.rglob("*.json")), [])
        with self.assertRaisesRegex(ArchiveError, "DiffID mismatch"):
            ImageArchiveReader(archive, self.root / "retry", cache_root).read("example/base:1")

    def test_lookup_refresh_cannot_overwrite_competing_store(self):
        directory = LayerCache(self.root / "cache")
        old, new = self.root / "old.tar", self.root / "new.tar"
        layer_file(old, {"payload": b"old"})
        layer_file(new, {"payload": b"new"})
        old_id, new_id = ["sha256:" + sha256_file(path) for path in (old, new)]
        directory.store("key", old_id, old)
        entry = directory.entries / "key.json"
        metadata = json.loads(entry.read_text())
        metadata["file_identity"] = []
        entry.write_text(json.dumps(metadata))
        reached, resume = threading.Event(), threading.Event()
        results, errors = [], []
        verify = cache_module.verify_layer_tar

        def paused_verify(*args, **kwargs):
            if threading.current_thread().name == "cache-reader":
                reached.set()
                if not resume.wait(10):
                    raise AssertionError("Writer did not release test barrier")
            return verify(*args, **kwargs)

        def lookup():
            try:
                results.append(directory.lookup("key"))
            except BaseException as exc:
                errors.append(exc)

        with patch.object(cache_module, "verify_layer_tar", paused_verify):
            worker = threading.Thread(target=lookup, name="cache-reader")
            worker.start()
            try:
                self.assertTrue(reached.wait(10))
                LayerCache(directory.root).store("other", None, None)
                with self.assertRaisesRegex(BuildError, "busy"):
                    LayerCache(directory.root).store("key", new_id, new)
            finally:
                resume.set()
                worker.join(10)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results[0].diff_id, old_id)
        directory.store("key", new_id, new)
        self.assertEqual(directory.lookup("key").diff_id, new_id)

    def test_cache_audit_agrees_with_lookup_on_truncated_layer(self):
        directory = LayerCache(self.root / "cache", verify=True)
        info = tarfile.TarInfo("payload")
        info.size = 4096
        broken = info.tobuf() + b"x" * 512
        import hashlib
        digest = hashlib.sha256(broken).hexdigest()
        layer = directory.layers / (digest + ".tar")
        layer.write_bytes(broken)
        (directory.entries / "key.json").write_text(json.dumps({
            "version": CACHE_VERSION, "key": "key", "empty": False,
            "diff_id": "sha256:" + digest, "size": len(broken)}))
        self.assertTrue(tarfile.is_tarfile(layer))
        self.assertIsNone(directory.lookup("key"))
        report = audit_cache(directory.root)
        self.assertEqual(report["valid_entries"], 0)
        self.assertEqual(len(report["invalid_entries"]), 1)
        prune_cache(directory.root)
        self.assertFalse(layer.exists())

    def test_pull_interruption_restores_previous_ref_and_tar(self):
        for existing in (False, True):
            with self.subTest(existing=existing):
                case = self.root / str(existing)
                case.mkdir()
                base = make_base(case)
                store = case / "store"
                store.mkdir()
                cas = CASStore(store)
                reference = "example/base:1"
                archive = image_store._archive_path(store, reference, "linux/amd64", "registry")
                if existing:
                    shutil.copyfile(base, archive)
                    cas.import_docker(base, reference, "linux/amd64", source="registry")
                before = cas.snapshot_ref(reference, "linux/amd64", "registry")
                image = ImageArchiveReader(base, case / "read").read(reference)
                image.config["config"]["Labels"] = {"revision": "new"}
                changed = case / "new.tar"
                ImageArchiveWriter().write_new(changed, image.config, image.layers, reference)

                def downloader(ref, output, *_args, cas_store=None, **kwargs):
                    shutil.copyfile(changed, output)
                    cas_store.import_docker(changed, ref, "linux/amd64", source="registry", replace=True)

                replace = image_store.os.replace

                def interrupted(source, target):
                    if Path(target) == archive:
                        raise KeyboardInterrupt("cancel before tar commit")
                    return replace(source, target)

                with patch.object(image_store, "registry_pull", downloader), \
                        patch.object(image_store.os, "replace", interrupted):
                    with self.assertRaises(KeyboardInterrupt):
                        image_store.pull_image(reference, store=store, refresh=True)
                self.assertEqual(cas.snapshot_ref(reference, "linux/amd64", "registry"), before)
                self.assertEqual(archive.exists(), existing)
                if existing:
                    self.assertEqual(archive.read_bytes(), base.read_bytes())

    def test_artifactory_interruption_restores_published_ref(self):
        from tests.test_artifactory_publication import (artifact_files, download, layer_bytes,
                                                      REFERENCE, PLATFORM, SOURCE)
        cas = CASStore(self.root / "store")
        download(artifact_files(layer_bytes(b"old")), self.root / "old-unused.tar", cas)
        previous = cas.snapshot_ref(REFERENCE, PLATFORM, SOURCE)
        output = self.root / "new.tar"
        with patch.object(artifactory_download, "pack_files", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                download(artifact_files(layer_bytes(b"new")), output, cas, cas_only=False)
        self.assertEqual(cas.snapshot_ref(REFERENCE, PLATFORM, SOURCE), previous)
        self.assertFalse(output.exists())

    def test_local_load_interruption_restores_previous_ref(self):
        base = make_base(self.root)
        store = self.root / "store"
        reference = "example/base:1"
        image_cli.load_archive(base, store)
        cas = CASStore(store)
        before = cas.snapshot_ref(reference, "linux/amd64", "local")
        target = image_store._archive_path(store, reference, "linux/amd64", "local")
        image = ImageArchiveReader(base, self.root / "read").read(reference)
        image.config["config"]["Labels"] = {"revision": "new"}
        changed = self.root / "new.tar"
        ImageArchiveWriter().write_new(changed, image.config, image.layers, reference)
        replace = image_cli.os.replace

        def interrupted(source, destination):
            if Path(destination) == target:
                raise KeyboardInterrupt
            return replace(source, destination)

        with patch.object(image_cli.os, "replace", interrupted):
            with self.assertRaises(KeyboardInterrupt):
                image_cli.load_archive(changed, store, replace=True)
        self.assertEqual(cas.snapshot_ref(reference, "linux/amd64", "local"), before)
        self.assertEqual(target.read_bytes(), base.read_bytes())
        self.assertFalse(target.with_name(target.name + ".part").exists())

    def test_remove_interruption_restores_ref_for_surviving_tar(self):
        base = make_base(self.root)
        store = self.root / "store"
        reference = "example/base:1"
        image_cli.load_archive(base, store)
        cas = CASStore(store)
        before = cas.snapshot_ref(reference, "linux/amd64", "local")
        target = image_store._archive_path(store, reference, "linux/amd64", "local")
        unlink = Path.unlink

        def interrupted(path, *args, **kwargs):
            if path == target:
                raise KeyboardInterrupt
            return unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", interrupted):
            with self.assertRaises(KeyboardInterrupt):
                image_cli.remove_image(reference, store, source="local")
        self.assertEqual(cas.snapshot_ref(reference, "linux/amd64", "local"), before)
        self.assertEqual(target.read_bytes(), base.read_bytes())

    def test_legacy_base_binding_is_reverified_before_hit(self):
        base = make_base(self.root)
        directory = self.root / "base-cache"
        image = ImageArchiveReader(base, self.root / "first", directory).read("example/base:1")
        metadata_path = next((directory / "entries").glob("*.json"))
        metadata = json.loads(metadata_path.read_text())
        layer = directory / "layers" / (metadata["diff_id"][7:] + ".tar")
        layer_file(layer, {"payload": b"wrong old cache"})
        metadata["version"] = 2
        metadata["layer"] = ImageArchiveReader._identity(layer)
        metadata_path.write_text(json.dumps(metadata))
        repaired = ImageArchiveReader(base, self.root / "second", directory).read("example/base:1")
        self.assertEqual(["sha256:" + sha256_file(path) for path in repaired.layers],
                         image.config["rootfs"]["diff_ids"])

    def test_interrupted_cas_export_cannot_poison_compatibility_tar_cache(self):
        base = make_base(self.root)
        store = self.root / "store"
        reference = "example/base:1"
        cas = CASStore(store)
        cas.import_docker(base, reference, "linux/amd64", "registry")
        before = cas.snapshot_ref(reference, "linux/amd64", "registry")
        archive = image_store._archive_path(store, reference, "linux/amd64", "registry")

        def interrupted(writer, output, image, tag, **kwargs):
            kwargs["fileobj"].write(b"partial archive")
            raise KeyboardInterrupt

        with patch.object(ImageArchiveWriter, "write_image", interrupted):
            with self.assertRaises(KeyboardInterrupt):
                image_store.pull_image(reference, store=store, offline=True)
        self.assertFalse(archive.exists())
        self.assertEqual(cas.snapshot_ref(reference, "linux/amd64", "registry"), before)
        result, downloaded = image_store.pull_image(reference, store=store, offline=True)
        self.assertEqual(result, archive)
        self.assertFalse(downloaded)
        ImageArchiveReader(archive, self.root / "retry-read").read(reference)

    def test_interrupted_copy_publication_removes_only_its_partial_output(self):
        source, target = self.root / "source", self.root / "target.tar"
        source.write_bytes(b"verified source")
        copy = file_publish.shutil.copyfileobj

        def interrupted(stream, output, length):
            if not isinstance(output.name, int) and Path(output.name) == target:
                output.write(b"partial")
                raise KeyboardInterrupt
            return copy(stream, output, length)

        with patch.object(file_publish.os, "link", side_effect=OSError(errno.EPERM, "no links")), \
                patch.object(file_publish.shutil, "copyfileobj", interrupted):
            with self.assertRaises(KeyboardInterrupt):
                file_publish.publish_new_file(source, target)
        self.assertFalse(target.exists())
        self.assertEqual(list(self.root.glob("*.part")), [])
        self.assertEqual(source.read_bytes(), b"verified source")
        target.write_bytes(b"foreign output")
        with patch.object(file_publish.os, "link", side_effect=OSError(errno.EPERM, "no links")):
            with self.assertRaises(FileExistsError):
                file_publish.publish_new_file(source, target)
        self.assertEqual(target.read_bytes(), b"foreign output")


if __name__ == "__main__":
    unittest.main()
