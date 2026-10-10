"""验证可选 history/OnBuild 的空值、原始身份和实际派生操作。"""

import hashlib
import json
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from builder import build
from cas_store import CASStore
from errors import BuildError
from image_cli import history_archive
from image_reader import ImageArchiveReader
from image_reader import BaseImage
from image_writer import ImageArchiveWriter
from oci_writer import OCIImageWriter
from optimizer import optimize_image
from rootfs import RootFSIndex
import registry
import conformance
from tests.test_phase1 import layer_file


ABSENT = object()
BASE = "example/optional:1"


def make_source(root, history=ABSENT, onbuild=ABSENT, kind="docker", empty=False, redundant=False):
    """用真实层和缩进配置构造外部镜像；不依赖 builder 补全可选字段。"""
    layers = []
    for number in range(0 if empty else 2 if redundant else 1):
        path = root / ("layer-{}.tar".format(number))
        layer_file(path, {"app/data": b"old" if number == 0 else b"new"})
        layers.append(path)
    config = {"os": "linux", "architecture": "amd64", "config": {},
              "rootfs": {"type": "layers", "diff_ids": [
                  "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest() for p in layers]}}
    if history is not ABSENT:
        config["history"] = history
    if onbuild is not ABSENT:
        config["config"]["OnBuild"] = onbuild
    raw = (json.dumps(config, indent=3) + "\n").encode()
    output = root / (kind + "-source.tar")
    writer = ImageArchiveWriter() if kind == "docker" else OCIImageWriter()
    writer.write_image(output, BaseImage(config, layers, [BASE], {}, config_raw=raw), BASE)
    return output, config, raw


def context_at(root, text):
    context = root / "context"
    context.mkdir()
    dockerfile = context / "Dockerfile"
    dockerfile.write_text(text, encoding="utf-8")
    return dockerfile, context


class OptionalImageMetadataTests(unittest.TestCase):
    def test_history_null_noop_build_keeps_original_bytes_across_stages(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, _, raw = make_source(root, history=None)
            dockerfile, context = context_at(root, "FROM " + BASE + " AS first\nFROM first\n")
            output = root / "built.tar"
            build(dockerfile, context, source, None, "example/app:1", output)
            image = ImageArchiveReader(output, root / "read").read("example/app:1")
            self.assertEqual(image.config_raw, raw)
            self.assertIsNone(image.config["history"])

    def test_onbuild_null_noop_build_keeps_original_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, _, raw = make_source(root, onbuild=None)
            dockerfile, context = context_at(root, "FROM " + BASE + "\n")
            output = root / "built.tar"
            build(dockerfile, context, source, None, "example/app:1", output)
            image = ImageArchiveReader(output, root / "read").read("example/app:1")
            self.assertEqual(image.config_raw, raw)
            self.assertIsNone(image.config["config"]["OnBuild"])

    def test_nullable_base_load_cas_copy_cache_and_reimport(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, _, raw = make_source(root, history=None, onbuild=None)
            store = CASStore(root / "store")
            store.import_docker(source, BASE, "linux/amd64")
            base = {"type": "cas", "reference": BASE, "platform": "linux/amd64",
                    "source": "local", "store": str(root / "store")}
            dockerfile, context = context_at(root, "FROM " + BASE + "\nCOPY app.war /app.war\n")
            (context / "app.war").write_bytes(b"WAR")
            outputs = []
            for number in range(2):
                output = root / ("built-{}.tar".format(number))
                stats = {}
                build(dockerfile, context, base, None, "example/app:1", output,
                      cache_dir=root / "cache", cache_stats=stats)
                self.assertEqual(stats["hits"], number)
                image = ImageArchiveReader(output, root / ("read-{}".format(number))).read("example/app:1")
                index = RootFSIndex()
                for layer in image.layers:
                    index.apply_layer(layer)
                self.assertEqual(index.read_file("app.war"), b"WAR")
                self.assertEqual(len(image.config["history"]), 2)
                self.assertNotEqual(image.config_raw, raw)
                outputs.append(output)
            self.assertEqual(outputs[0].read_bytes(), outputs[1].read_bytes())
            store.import_docker(outputs[1], "example/app:1", "linux/amd64")
            self.assertEqual(len(store.open_base("example/app:1").layers), 2)

    def test_new_onbuild_can_be_added_to_null_and_runs_once_in_child(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, _, _ = make_source(root, onbuild=None)
            dockerfile, context = context_at(root, "FROM " + BASE + "\nONBUILD COPY app.war /app.war\n")
            parent = root / "parent.tar"
            build(dockerfile, context, source, None, "example/parent:1", parent)
            child = root / "child"
            child.mkdir()
            (child / "app.war").write_bytes(b"downstream")
            (child / "Dockerfile").write_text("FROM example/parent:1 AS first\nFROM first\n")
            output = root / "child.tar"
            build(child / "Dockerfile", child, parent, None, "example/child:1", output)
            image = ImageArchiveReader(output, root / "read").read("example/child:1")
            index = RootFSIndex()
            for layer in image.layers:
                index.apply_layer(layer)
            self.assertEqual(index.read_file("app.war"), b"downstream")
            self.assertEqual(len(image.layers), 2)
            self.assertNotIn("OnBuild", image.config["config"])

    def test_history_missing_null_empty_is_reported_as_unavailable(self):
        for value in (ABSENT, None, []):
            with self.subTest(history=repr(value)), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source, _, raw = make_source(root, history=value)
                report = history_archive(source)
                self.assertEqual(report["history"], [])
                self.assertFalse(report["history_available"])
                self.assertEqual(report["unrecorded_layers"], 1)
                self.assertEqual(ImageArchiveReader(source, root / "read").read(BASE).config_raw, raw)

    def test_optimizer_accepts_empty_history_and_preserves_noop_config(self):
        for kind in ("docker", "oci"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source, _, raw = make_source(root, history=[], kind=kind)
                output = root / "optimized.tar"
                self.assertEqual(optimize_image(source, output)["removed_layer_numbers"], [])
                if kind == "docker":
                    self.assertEqual(ImageArchiveReader(output, root / "read").read(BASE).config_raw, raw)
                else:
                    self.assertEqual(output.read_bytes(), source.read_bytes())

    def test_real_pruning_without_history_stays_importable_and_buildable(self):
        for kind in ("docker", "oci"):
            for value in (ABSENT, None, []):
                with self.subTest(kind=kind, history=repr(value)), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    source, _, _ = make_source(root, history=value, kind=kind, redundant=True)
                    optimized = root / "optimized.tar"
                    report = optimize_image(source, optimized)
                    self.assertEqual(report["removed_layer_numbers"], [1])
                    store = CASStore(root / "store")
                    if kind == "docker":
                        store.import_docker(optimized, BASE, "linux/amd64")
                    else:
                        store.import_oci(optimized, BASE, "linux/amd64")
                    saved = root / "saved.tar"
                    store.export_docker(BASE, saved)
                    image = ImageArchiveReader(saved, root / "read").read(BASE)
                    self.assertEqual(len(image.layers), 1)
                    if value is ABSENT:
                        self.assertNotIn("history", image.config)
                    else:
                        self.assertEqual(image.config["history"], value)
                    dockerfile, context = context_at(root, "FROM " + BASE + "\nENV APP=yes\n")
                    built = root / "built.tar"
                    build(dockerfile, context, saved, None, "example/app:1", built)
                    derived = ImageArchiveReader(built, root / "derived").read("example/app:1")
                    self.assertEqual(len(derived.config["history"]), 2)

    def test_invalid_history_containers_and_empty_layer_types_are_rejected(self):
        for value in (False, 0, "", {}, [None], [{"empty_layer": 1}], [{"empty_layer": "false"}]):
            with self.subTest(history=value), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source, _, _ = make_source(root, history=value, empty=True)
                with self.assertRaisesRegex(BuildError, "history"):
                    history_archive(source)
                store = CASStore(root / "store")
                with self.assertRaisesRegex(BuildError, "history"):
                    store.import_docker(source, BASE, "linux/amd64")
                self.assertFalse(store.has_ref(BASE, "linux/amd64", "local"))

    def test_nullable_empty_layer_is_a_filesystem_history_entry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, _, _ = make_source(root, history=[{"created_by": "base", "empty_layer": None}])
            report = history_archive(source)
            self.assertTrue(report["history_available"])
            self.assertFalse(report["history"][0]["empty_layer"])
            self.assertGreater(report["history"][0]["size"], 0)

    def test_invalid_registry_history_is_rejected_before_layer_download_or_publication(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, config, _ = make_source(root, history=[{"empty_layer": "false"}])
            raw = json.dumps(config).encode()
            descriptor = {"mediaType": registry.CONFIG_TYPE, "size": len(raw),
                          "digest": "sha256:" + hashlib.sha256(raw).hexdigest()}
            manifest = {"schemaVersion": 2, "mediaType": registry.MANIFEST_TYPE,
                        "config": descriptor, "layers": [{"mediaType": registry.LAYER_TYPE,
                        "size": 0, "digest": config["rootfs"]["diff_ids"][0]}]}
            client = Mock()
            client.reference = registry.parse_reference("example/p/base:1")
            client.manifest_raw = json.dumps(manifest).encode()
            manifest_digest = "sha256:" + hashlib.sha256(client.manifest_raw).hexdigest()
            client.blob.side_effect = lambda _desc, target: Path(target).write_bytes(raw)
            store = CASStore(root / "store")
            with patch.object(registry, "RegistryClient", return_value=client), \
                    patch.object(registry, "_selected_manifest", return_value=(manifest, manifest_digest)):
                with self.assertRaisesRegex(BuildError, "history"):
                    registry.pull("example/p/base:1", root / "bad.tar", cas_store=store)
            self.assertEqual(client.blob.call_count, 1)
            self.assertFalse((root / "bad.tar").exists())
            self.assertFalse(store.has_ref("example/p/base:1", "linux/amd64", "registry"))

    def test_independent_conformance_reader_checks_nullable_and_invalid_metadata(self):
        for kind in ("docker", "oci"):
            for value in (ABSENT, None, [], False, [None], [{"empty_layer": "false"}]):
                with self.subTest(kind=kind, history=repr(value)), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    valid = value is ABSENT or value is None or value == []
                    source, config, _ = make_source(root, history=value, kind=kind, empty=not valid)
                    if valid:
                        config["config"] = None
                        writer = ImageArchiveWriter() if kind == "docker" else OCIImageWriter()
                        writer.write_new(source, config, [root / 'layer-0.tar'], BASE)
                        result = conformance.snapshot(source, BASE)
                        self.assertEqual(result["layer_count"], 1)
                        self.assertIn("app/data", result["tree"])
                    else:
                        with self.assertRaisesRegex(BuildError, "history"):
                            conformance.snapshot(source, BASE)

    def test_invalid_oci_and_direct_registry_import_do_not_publish_refs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, _, raw = make_source(root, history=False, kind="oci")
            store = CASStore(root / "store")
            with self.assertRaisesRegex(BuildError, "history"):
                OCIImageWriter().verify(source, BASE)
            with self.assertRaisesRegex(BuildError, "history"):
                store.import_oci(source, BASE, "linux/amd64")
            self.assertFalse(store.has_ref(BASE, "linux/amd64", "local"))
            with tarfile.open(source) as archive:
                index = json.load(archive.extractfile("index.json"))
                name = "blobs/sha256/" + index["manifests"][0]["digest"].split(":")[1]
                manifest_raw = archive.extractfile(name).read()
            config_path = root / "config.json"
            config_path.write_bytes(raw)
            with self.assertRaisesRegex(BuildError, "history"):
                store.import_registry(manifest_raw, config_path, [root / "layer-0.tar"],
                                      BASE, "linux/amd64", source="registry")
            self.assertFalse(store.has_ref(BASE, "linux/amd64", "registry"))


if __name__ == "__main__":
    unittest.main()
