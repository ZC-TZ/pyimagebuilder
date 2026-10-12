"""核对 Artifactory 下载校验与 CAS 引用发布之间的事务边界。"""

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

import artifactory_download as downloader
from cas_store import CASStore
from errors import BuildError
from image_reader import ImageArchiveReader


REFERENCE = "example.test/repo/base:1"
PLATFORM = "linux/amd64"
SOURCE = "artifactory"
MEDIA = "application/vnd.oci.image.layer.v1.tar+gzip"


def digest(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def layer_bytes(payload):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        member = tarfile.TarInfo("payload")
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))
    return stream.getvalue()


def artifact_files(raw, blob=None, diff_id=None):
    blob = gzip.compress(raw) if blob is None else blob
    config = (json.dumps({"os": "linux", "architecture": "amd64", "config": {},
                          "rootfs": {"type": "layers", "diff_ids": [diff_id or digest(raw)]}},
                         indent=2) + "\n").encode()
    manifest = (json.dumps({"schemaVersion": 2,
                            "config": {"digest": digest(config), "size": len(config)},
                            "layers": [{"digest": digest(blob), "size": len(blob),
                                        "mediaType": MEDIA}]}, indent=4) + "\n").encode()
    return {"manifest.json": manifest, "sha256__" + digest(config)[7:]: config,
            "sha256__" + digest(blob)[7:]: blob}


def download(files, output, store, cas_only=True):
    """只替换网络文件传输；解压、校验、缓存与归档发布均执行真实实现。"""
    def transfer(_opener, _url, name, root, _size, _limiter, stats, _display):
        (root / name).write_bytes(files[name])
        stats.add(len(files[name]))

    with patch.object(downloader, "probe_file_sizes",
                      return_value={name: len(raw) for name, raw in files.items()}), \
            patch.object(downloader, "download_one", side_effect=transfer), \
            contextlib.redirect_stdout(io.StringIO()):
        downloader.download_and_pack(object(), "https://example.test/repo/base/1/",
                                     list(files), output, 2, REFERENCE, cas_store=store,
                                     target_platform=PLATFORM, cas_only=cas_only)


class ArtifactoryPublicationTests(unittest.TestCase):
    def test_bad_diff_id_cannot_replace_ref_even_with_decoded_cache(self):
        for cached in (False, True):
            with self.subTest(cached=cached), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                store = CASStore(root / "store")
                old = layer_bytes(b"old")
                if cached:
                    download(artifact_files(old), root / "unused.tar", store)
                previous = store.snapshot_ref(REFERENCE, PLATFORM, SOURCE)
                wrong = artifact_files(layer_bytes(b"new"), diff_id=digest(old))
                with self.assertRaises((BuildError, RuntimeError)):
                    download(wrong, root / "unused.tar", store)
                self.assertEqual(store.snapshot_ref(REFERENCE, PLATFORM, SOURCE), previous)

    def test_invalid_gzip_cannot_use_other_blobs_decoded_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = CASStore(root / "store")
            raw = layer_bytes(b"old")
            download(artifact_files(raw), root / "unused.tar", store)
            previous = store.snapshot_ref(REFERENCE, PLATFORM, SOURCE)
            with self.assertRaises((BuildError, RuntimeError)):
                download(artifact_files(raw, blob=b"invalid gzip"), root / "unused.tar", store)
            self.assertEqual(store.snapshot_ref(REFERENCE, PLATFORM, SOURCE), previous)

    def test_bad_archive_layer_keeps_previous_ref(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = CASStore(root / "store")
            old = layer_bytes(b"old")
            download(artifact_files(old), root / "unused.tar", store)
            previous = store.snapshot_ref(REFERENCE, PLATFORM, SOURCE)
            with self.assertRaises((BuildError, RuntimeError)):
                download(artifact_files(layer_bytes(b"new"), diff_id=digest(old)),
                         root / "bad.tar", store, cas_only=False)
            self.assertEqual(store.snapshot_ref(REFERENCE, PLATFORM, SOURCE), previous)
            self.assertFalse((root / "bad.tar").exists())

    def test_pack_failure_restores_ref_and_allows_next_update(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = CASStore(root / "store")
            download(artifact_files(layer_bytes(b"old")), root / "unused.tar", store)
            previous = store.snapshot_ref(REFERENCE, PLATFORM, SOURCE)
            with patch.object(downloader, "pack_files", side_effect=OSError("pack interrupted")):
                with self.assertRaisesRegex(OSError, "pack interrupted"):
                    download(artifact_files(layer_bytes(b"new")), root / "new.tar", store,
                             cas_only=False)
            self.assertEqual(store.snapshot_ref(REFERENCE, PLATFORM, SOURCE), previous)
            download(artifact_files(layer_bytes(b"winner")), root / "unused.tar", store)
            self.assertNotEqual(store.snapshot_ref(REFERENCE, PLATFORM, SOURCE), previous)

    def test_cas_only_saves_verified_layers_before_publishing_ref(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = CASStore(root / "store")
            raw = layer_bytes(b"new")
            files = artifact_files(raw)
            original = store.import_registry

            def publish(*args, **kwargs):
                self.assertTrue(store._blob(digest(raw)).is_file(), "发布引用前必须保存已验证解码层")
                self.assertEqual(store._blob(digest(raw)).read_bytes(), raw)
                return original(*args, **kwargs)

            with patch.object(store, "import_registry", side_effect=publish):
                download(files, root / "unused.tar", store)
            ref = store.resolve(REFERENCE, PLATFORM, SOURCE)
            self.assertEqual(store._blob(ref["manifest"]).read_bytes(), files["manifest.json"])
            self.assertFalse((root / "unused.tar").exists())
            with patch("cas_store.gzip.open", side_effect=AssertionError("must reuse decoded cache")):
                image = store.open_base(REFERENCE, PLATFORM, SOURCE)
            self.assertEqual(image.layers[0].read_bytes(), raw)

    def test_invalid_tar_leaves_no_new_ref(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = CASStore(root / "store")
            with self.assertRaises((BuildError, RuntimeError)):
                download(artifact_files(b"not a tar"), root / "unused.tar", store)
            self.assertFalse(store.has_ref(REFERENCE, PLATFORM, SOURCE))

    def test_unsupported_or_misdeclared_encoding_still_fails(self):
        for media in ("application/example.unknown", "application/vnd.oci.image.layer.v1.tar"):
            with self.subTest(media=media), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                store = CASStore(root / "store")
                files = artifact_files(layer_bytes(b"payload"))
                manifest = json.loads(files["manifest.json"])
                manifest["layers"][0]["mediaType"] = media
                files["manifest.json"] = json.dumps(manifest).encode()
                with self.assertRaises(BuildError):
                    download(files, root / "unused.tar", store)
                self.assertFalse(store.has_ref(REFERENCE, PLATFORM, SOURCE))

    def test_later_bad_layer_does_not_publish_partial_image(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = CASStore(root / "store")
            raw = layer_bytes(b"first")
            files = artifact_files(raw)
            config_key = next(name for name in files if name.startswith("sha256__") and
                              files[name].startswith(b"{"))
            config = json.loads(files.pop(config_key))
            config["rootfs"]["diff_ids"].append(digest(layer_bytes(b"expected second")))
            config_raw = json.dumps(config).encode()
            files["sha256__" + digest(config_raw)[7:]] = config_raw
            bad_blob = gzip.compress(layer_bytes(b"wrong second"))
            manifest = json.loads(files["manifest.json"])
            manifest["config"] = {"digest": digest(config_raw), "size": len(config_raw)}
            manifest["layers"].append({"digest": digest(bad_blob), "size": len(bad_blob),
                                       "mediaType": MEDIA})
            files["manifest.json"] = json.dumps(manifest).encode()
            files["sha256__" + digest(bad_blob)[7:]] = bad_blob
            with self.assertRaises((BuildError, RuntimeError)):
                download(files, root / "unused.tar", store)
            self.assertFalse(store.has_ref(REFERENCE, PLATFORM, SOURCE))
            self.assertEqual(store._blob(digest(raw)).read_bytes(), raw)
            # 首层虽已入库，失败镜像没有 ref；可由既有 GC 规则回收。
            self.assertGreater(store.prune_orphans(dry_run=True)["orphan_blobs"], 0)

    def test_archive_success_retains_identity_and_reuses_decoded_layers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = CASStore(root / "store")
            raw = layer_bytes(b"new")
            files = artifact_files(raw)
            output = root / "image.tar"
            download(files, output, store, cas_only=False)
            ref = store.resolve(REFERENCE, PLATFORM, SOURCE)
            with patch("cas_store.gzip.open", side_effect=AssertionError("must reuse decoded cache")):
                cached = store.open_base(REFERENCE, PLATFORM, SOURCE)
            extracted = root / "extracted"
            extracted.mkdir()
            archived = ImageArchiveReader(output, extracted).read(REFERENCE)
            self.assertEqual(archived.config_raw, cached.config_raw)
            self.assertEqual(archived.layers[0].read_bytes(), raw)
            self.assertEqual(store._blob(ref["manifest"]).read_bytes(), files["manifest.json"])

    def test_direct_download_rollback_cannot_overwrite_concurrent_update(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = CASStore(root / "store")
            download(artifact_files(layer_bytes(b"old")), root / "unused.tar", store)
            previous = store.snapshot_ref(REFERENCE, PLATFORM, SOURCE)
            ready, resume = threading.Event(), threading.Event()
            blob = root / "winner-layer.gz"
            raw = layer_bytes(b"winner")
            blob.write_bytes(gzip.compress(raw))
            files = artifact_files(raw, blob.read_bytes())
            manifest_raw = files["manifest.json"]
            manifest = json.loads(manifest_raw)
            config = root / "winner-config.json"
            config.write_bytes(files["sha256__" + manifest["config"]["digest"][7:]])

            def fail_pack(*_args):
                ready.set()
                if not resume.wait(5):
                    raise AssertionError("test synchronization timed out")
                raise OSError("pack interrupted")

            with patch.object(downloader, "pack_files", side_effect=fail_pack), \
                    ThreadPoolExecutor(max_workers=1) as executor:
                operation = executor.submit(download, artifact_files(layer_bytes(b"attempted")),
                                            root / "image.tar", store, False)
                try:
                    self.assertTrue(ready.wait(5))
                    with self.assertRaisesRegex(BuildError, "busy"):
                        CASStore(root / "store").import_registry(manifest_raw, config, [blob],
                                                            REFERENCE, PLATFORM, replace=True)
                finally:
                    resume.set()
                with self.assertRaisesRegex(OSError, "pack interrupted"):
                    operation.result(timeout=5)
            self.assertEqual(store.snapshot_ref(REFERENCE, PLATFORM, SOURCE), previous)
            store.import_registry(manifest_raw, config, [blob], REFERENCE, PLATFORM, replace=True)
            self.assertNotEqual(store.snapshot_ref(REFERENCE, PLATFORM, SOURCE), previous)


if __name__ == "__main__":
    unittest.main()
