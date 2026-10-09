"""验证镜像分析、保守优化和缓存清理。"""

import hashlib
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path

from cache import CACHE_VERSION, LayerCache, cache_key
from errors import ArchiveError, BuildError
from image_reader import ImageArchiveReader
from image_writer import ImageArchiveWriter
from oci_writer import OCIImageWriter
from optimizer import analyze_image, audit_cache, optimize_image, prune_cache
from rootfs import RootFSIndex


def make_layer(path, files):
    with tarfile.open(path, "w") as archive:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mtime = 0
            archive.addfile(info, io.BytesIO(content))
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


class PhaseFourteenTests(unittest.TestCase):
    def _image(self, root, kind):
        old = root / "old.tar"
        new = root / "new.tar"
        first = make_layer(old, {"app/data.txt": b"obsolete content"})
        second = make_layer(new, {"app/data.txt": b"new content",
                                  "app/copy.txt": b"new content"})
        config = {"architecture": "amd64", "os": "linux",
                  "rootfs": {"type": "layers", "diff_ids": [first, second]},
                  "config": {}, "history": [{"created_by": "old"}, {"created_by": "new"}]}
        source = root / (kind + ".tar")
        writer = ImageArchiveWriter() if kind == "docker" else OCIImageWriter()
        writer.write(source, config, [old, new], "example/phase14:1")
        writer.verify(source, "example/phase14:1")
        return source

    def test_analysis_and_safe_optimization_both_formats(self):
        for kind in ("docker", "oci"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                source = self._image(root, kind)
                report = analyze_image(source, large_mib=0)
                self.assertEqual(report["format"], kind)
                self.assertEqual(report["layers"][0]["hidden_file_bytes"], len(b"obsolete content"))
                self.assertTrue(report["duplicate_content"])
                optimized = root / "optimized.tar"
                result = optimize_image(source, optimized)
                self.assertEqual(result["removed_layer_numbers"], [1])
                self.assertLess(result["after_bytes"], result["before_bytes"])
                if kind == "docker":
                    with tempfile.TemporaryDirectory() as extracted:
                        image = ImageArchiveReader(optimized, Path(extracted)).read("example/phase14:1")
                        self.assertEqual(len(image.layers), 1)
                        index = RootFSIndex()
                        for layer in image.layers:
                            index.apply_layer(layer)
                        self.assertEqual(index.kind("app/data.txt"), "file")
                else:
                    OCIImageWriter().verify(optimized, "example/phase14:1")

    def test_cache_audit_and_explicit_prune(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cache = LayerCache(root / "cache")
            layer = root / "layer.tar"
            diff = make_layer(layer, {"kept": b"x"})
            cache.store(cache_key("valid"), diff, layer)
            orphan = cache.layers / ("f" * 64 + ".tar")
            orphan.write_bytes(b"orphan")
            stale = cache.entries / ("a" * 64 + ".json")
            stale.write_text(json.dumps({"version": CACHE_VERSION - 1,
                                         "key": "a" * 64}), encoding="utf-8")
            report = audit_cache(cache.root)
            self.assertEqual(report["valid_entries"], 1)
            self.assertEqual(len(report["invalid_entries"]), 1)
            self.assertEqual(len(report["orphan_layers"]), 1)
            self.assertTrue(stale.exists())
            prune_cache(cache.root)
            self.assertFalse(stale.exists())
            self.assertFalse(orphan.exists())
            self.assertIsNotNone(cache.lookup(cache_key("valid")))

    def test_analysis_does_not_call_hardlink_backing_content_hidden(self):
        for kind in ("docker", "oci"):
            for remove_alias in (False, True):
                with self.subTest(kind=kind, remove_alias=remove_alias), tempfile.TemporaryDirectory() as temp:
                    root = Path(temp)
                    old, new = root / "old.tar", root / "new.tar"
                    make_layer(old, {"original": b"old"})
                    with tarfile.open(old, "a") as archive:
                        info = tarfile.TarInfo("alias")
                        info.type = tarfile.LNKTYPE
                        info.linkname = "original"
                        archive.addfile(info)
                    layers = [old, new]
                    diffs = ["sha256:" + hashlib.sha256(old.read_bytes()).hexdigest(),
                             make_layer(new, {"original": b"new"})]
                    if remove_alias:
                        deleted = root / "deleted.tar"
                        diffs.append(make_layer(deleted, {".wh.alias": b""}))
                        layers.append(deleted)
                    config = {"architecture": "amd64", "os": "linux", "config": {},
                              "rootfs": {"type": "layers", "diff_ids": diffs}}
                    source = root / "image.tar"
                    writer = ImageArchiveWriter() if kind == "docker" else OCIImageWriter()
                    writer.write(source, config, layers, "example/links:1")
                    report = analyze_image(source)
                    self.assertEqual(report["hidden_file_bytes"], 3 if remove_alias else 0)

    def test_malformed_metadata_has_controlled_errors_and_no_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            layer = root / "layer.tar"
            diff = make_layer(layer, {"file": b"data"})
            config = {"architecture": "amd64", "os": "linux", "config": {},
                      "rootfs": {"type": "layers", "diff_ids": [diff]}, "history": [None]}
            source, output = root / "source.tar", root / "output.tar"
            ImageArchiveWriter().write(source, config, [layer], "example/bad:1")
            with self.assertRaisesRegex(BuildError, "history"):
                optimize_image(source, output)
            self.assertFalse(output.exists())
            for name, metadata in (
                ("index.json", {"manifests": [{"annotations": []}]}),
                ("manifest.json", [{"RepoTags": 123}]),
            ):
                raw = json.dumps(metadata).encode("utf-8")
                with tarfile.open(source, "w") as archive:
                    member = tarfile.TarInfo(name)
                    member.size = len(raw)
                    archive.addfile(member, io.BytesIO(raw))
                with self.assertRaises((BuildError, ArchiveError)):
                    analyze_image(source)


if __name__ == "__main__":
    unittest.main()
