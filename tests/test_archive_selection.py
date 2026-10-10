"""验证 Docker 归档的平台选择、真实镜像身份及标签结构校验。"""

import contextlib
import hashlib
import io
import json
import os
import sys
import tarfile
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import image_cli
import main as builder_main
from builder import build
from cas_store import CASStore
from errors import BuildError
from fast import _require_base_tag
from image_reader import ImageArchiveReader
from image_store import _archive_path, _check_archive_platform, pull_image
from image_reader import BaseImage
from image_writer import ImageArchiveWriter
from rootfs import RootFSIndex
from oci_writer import OCIImageWriter
from registry import pull as registry_pull, _oci_archive, MANIFEST_TYPE, CONFIG_TYPE, LAYER_TYPE
from tests.test_phase1 import add_bytes, layer_file


REFERENCE = "example/base:1"


def make_variants(root):
    """合并两个单平台 Docker archive，标签相同但层内容不同。"""
    entries, members, ids = [], {}, {}
    for arch in ("amd64", "arm64"):
        layer = root / (arch + "-layer.tar")
        layer_file(layer, {"platform": arch.encode()})
        diff_id = "sha256:" + hashlib.sha256(layer.read_bytes()).hexdigest()
        config = {"os": "linux", "architecture": arch, "config": {"Env": ["ARCH=" + arch]},
                  "rootfs": {"type": "layers", "diff_ids": [diff_id]},
                  "history": [{"created_by": arch}]}
        single = root / (arch + ".tar")
        ImageArchiveWriter().write_new(single, config, [layer], REFERENCE)
        with tarfile.open(single) as source:
            entry = json.load(source.extractfile("manifest.json"))[0]
            entries.append(entry)
            for member in source:
                if member.name not in ("manifest.json", "repositories"):
                    members[member.name] = source.extractfile(member).read()
            ids[arch] = "sha256:" + hashlib.sha256(members[entry["Config"]]).hexdigest()
    output = root / "multi.tar"
    with tarfile.open(output, "w") as archive:
        add_bytes(archive, "manifest.json", json.dumps(entries).encode())
        for name, raw in sorted(members.items()):
            add_bytes(archive, name, raw)
    return output, ids


def rewrite_archive(source, output, config_name=None, prefix="", tags=None, pretty=False):
    """改写名称或标签，保留选中配置的原始字节。"""
    with tarfile.open(source) as archive:
        manifest = json.load(archive.extractfile("manifest.json"))
        original = manifest[0]["Config"]
        if config_name is not None:
            manifest[0]["Config"] = config_name
        if tags is not None:
            manifest[0]["RepoTags"] = tags
        raw_config = archive.extractfile(original).read()
        if pretty:
            raw_config = (json.dumps(json.loads(raw_config), indent=2) + "\n").encode()
        with tarfile.open(output, "w") as target:
            add_bytes(target, prefix + "manifest.json", json.dumps(manifest).encode())
            for member in archive:
                if member.name == "manifest.json":
                    continue
                name = config_name if member.name == original and config_name is not None else member.name
                add_bytes(target, prefix + name, raw_config if member.name == original else
                          archive.extractfile(member).read())
    return "sha256:" + hashlib.sha256(raw_config).hexdigest()


class ArchiveSelectionTests(unittest.TestCase):
    def test_same_tag_platform_selection_across_inspect_verify_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive, ids = make_variants(root)
            for arch in ("amd64", "arm64"):
                for tag in (None, REFERENCE):
                    with self.subTest(arch=arch, tag=tag):
                        platform = "linux/" + arch
                        result = image_cli.inspect_archive(archive, tag, platform)
                        self.assertEqual(result["platform"], platform)
                        self.assertEqual(result["image_id"], ids[arch])
                        self.assertEqual(image_cli.verify_archive(archive, tag, platform)["layers_verified"], 1)
                        self.assertEqual(image_cli.history_archive(archive, tag, platform)["history"][0]["created_by"], arch)

    def test_platform_load_offline_build_and_save_keep_selected_payload(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive, _ = make_variants(root)
            store_root = root / "store"
            context = root / "context"
            context.mkdir()
            dockerfile = context / "Dockerfile"
            dockerfile.write_text("FROM example/base:1\nCOPY app.war /app.war\n", encoding="utf-8")
            (context / "app.war").write_bytes(b"WAR")
            for arch in ("amd64", "arm64"):
                platform = "linux/" + arch
                loaded = image_cli.load_archive(archive, store_root, platform=platform)
                self.assertEqual(loaded["platform"], platform)
                base, downloaded = pull_image(REFERENCE, store=store_root, offline=True,
                                              materialize_tar=False, platform=platform)
                self.assertFalse(downloaded)
                output = root / (arch + "-built.tar")
                build(dockerfile, context, base, None, "app:1", output, target_platform=platform)
                image = ImageArchiveReader(output, root / (arch + "-read")).read("app:1", platform)
                index = RootFSIndex()
                for layer in image.layers:
                    index.apply_layer(layer)
                self.assertEqual(index.read_file("platform"), arch.encode())
                self.assertEqual(index.read_file("app.war"), b"WAR")
                saved = root / (arch + "-saved.tar")
                image_cli.save_archive(REFERENCE, saved, store_root, platform=platform, source="local")
                self.assertEqual(image_cli.inspect_archive(saved, platform=platform)["platform"], platform)
                CASStore(store_root).resolve(REFERENCE, platform, "local")

    def test_both_cli_entrypoints_accept_explicit_platform(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive, ids = make_variants(root)
            for main in (image_cli.main, builder_main.main):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = main(["inspect", str(archive), "--platform", "linux/arm64"])
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(output.getvalue())["image_id"], ids["arm64"])

    def test_ambiguous_platform_and_missing_tag_still_fail(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive, _ = make_variants(root)
            with self.assertRaises(BuildError):
                image_cli.inspect_archive(archive, REFERENCE)
            with self.assertRaises(BuildError):
                image_cli.inspect_archive(archive, "missing:1", "linux/amd64")
            second = root / "duplicate-platform.tar"
            with tarfile.open(archive) as source, tarfile.open(second, "w") as target:
                entries = json.load(source.extractfile("manifest.json"))
                add_bytes(target, "manifest.json", json.dumps([entries[0], entries[0]]).encode())
                for member in source:
                    if member.name != "manifest.json":
                        add_bytes(target, member.name, source.extractfile(member).read())
            with self.assertRaises(BuildError):
                image_cli.load_archive(second, root / "store", platform="linux/amd64")

    def test_inspect_id_uses_raw_config_digest_for_aliases_and_prefixes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_variants(root)
            with tarfile.open(root / "amd64.tar") as source:
                config_name = json.load(source.extractfile("manifest.json"))[0]["Config"]
            for name, prefix in (("./" + config_name, "./"), ("nested/config.json", ""), ("config.json", "././")):
                with self.subTest(name=name, prefix=prefix):
                    output = root / "rewritten.tar"
                    expected = rewrite_archive(root / "amd64.tar", output, config_name=name, prefix=prefix,
                                               pretty=name != "./" + config_name)
                    self.assertEqual(image_cli.inspect_archive(output)["image_id"], expected)

    def test_non_array_tags_are_rejected_at_archive_entrypoints(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_variants(root)
            for tags in ("prefix-" + REFERENCE + "-suffix", {REFERENCE: True}, [1]):
                with self.subTest(tags=tags):
                    output = root / "bad-tags.tar"
                    rewrite_archive(root / "amd64.tar", output, tags=tags)
                    for operation in (
                            lambda: ImageArchiveReader(output, root / "read").read(REFERENCE),
                            lambda: _check_archive_platform(output, REFERENCE, "linux/amd64"),
                            lambda: _require_base_tag(output, REFERENCE),
                            lambda: image_cli.load_archive(output, root / "store", tag=REFERENCE)):
                        with self.assertRaises(BuildError):
                            operation()
                    self.assertFalse((root / "store").exists())

    def test_cas_export_preserves_raw_config_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_variants(root)
            source = root / "pretty.tar"
            expected = rewrite_archive(root / "amd64.tar", source, config_name="config.json", pretty=True)
            store = CASStore(root / "store")
            value = store.import_docker(source, REFERENCE, "linux/amd64")
            self.assertEqual(value["config"], expected)
            output = root / "exported.tar"
            store.export_docker(REFERENCE, output)
            self.assertEqual(image_cli.inspect_archive(output)["image_id"], expected)
            with tarfile.open(output) as archive:
                entry = json.load(archive.extractfile("manifest.json"))[0]
                self.assertEqual(archive.extractfile(entry["Config"]).read(), store._blob(expected).read_bytes())
            self.assertEqual(store.open_base(REFERENCE).config_digest, expected)

    def test_registry_pull_and_docker_to_oci_preserve_raw_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_variants(root)
            source = root / "pretty.tar"
            config_digest = rewrite_archive(root / "amd64.tar", source, config_name="config.json", pretty=True)
            with tarfile.open(source) as archive:
                entry = json.load(archive.extractfile("manifest.json"))[0]
                config_raw = archive.extractfile(entry["Config"]).read()
                layers = [archive.extractfile(name).read() for name in entry["Layers"]]
            blobs = {config_digest: config_raw}
            descriptors = []
            for raw in layers:
                digest = "sha256:" + hashlib.sha256(raw).hexdigest()
                blobs[digest] = raw
                descriptors.append({"digest": digest, "size": len(raw), "mediaType": LAYER_TYPE})
            manifest_raw = json.dumps({"schemaVersion": 2, "mediaType": MANIFEST_TYPE,
                "config": {"digest": config_digest, "size": len(config_raw), "mediaType": CONFIG_TYPE},
                "layers": descriptors}).encode()

            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *_args):
                    pass

                def do_GET(self):
                    raw = manifest_raw if "/manifests/" in self.path else blobs[self.path.rsplit("/", 1)[-1]]
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(raw)))
                    if "/manifests/" in self.path:
                        self.send_header("Content-Type", MANIFEST_TYPE)
                        self.send_header("Docker-Content-Digest", "sha256:" + hashlib.sha256(raw).hexdigest())
                    self.end_headers()
                    self.wfile.write(raw)

            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                reference = "127.0.0.1:{}/p/app:1".format(server.server_port)
                output, oci = root / "pulled.tar", root / "pulled.oci.tar"
                registry_pull(reference, output, insecure_http=True, image_format="both", oci_output=oci)
                self.assertEqual(image_cli.inspect_archive(output)["image_id"], config_digest)
                for path in (oci, _oci_archive(source, root / "conversion", REFERENCE)):
                    with tarfile.open(path) as archive:
                        index = json.load(archive.extractfile("index.json"))
                        manifest = json.load(archive.extractfile("blobs/sha256/" + index["manifests"][0]["digest"].split(":")[1]))
                        self.assertEqual(manifest["config"]["digest"], config_digest)
                        self.assertEqual(archive.extractfile("blobs/sha256/" + config_digest.split(":")[1]).read(), config_raw)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

    def test_raw_config_mismatch_is_rejected_before_writer_creates_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = {"os": "linux", "architecture": "amd64", "rootfs": {"type": "layers", "diff_ids": []}}
            for writer in (ImageArchiveWriter(), OCIImageWriter()):
                output = root / "bad.tar"
                with self.assertRaises(BuildError):
                    writer.write_image(output, BaseImage(config, [], [REFERENCE], {}, config_raw=b'{}'), REFERENCE)
                self.assertFalse(output.exists())

    def test_legacy_tar_adapter_is_repaired_from_cas_without_download(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_variants(root)
            source = root / "pretty.tar"
            expected = rewrite_archive(root / "amd64.tar", source, config_name="config.json", pretty=True)
            store_root = root / "store"
            store = CASStore(store_root)
            store.import_docker(source, REFERENCE, "linux/amd64", "registry")
            image = ImageArchiveReader(source, root / "read").read(REFERENCE)
            adapter = _archive_path(store_root, REFERENCE, "linux/amd64", "registry")
            ImageArchiveWriter().write_new(adapter, image.config, image.layers, REFERENCE)
            self.assertNotEqual(image_cli.inspect_archive(adapter)["image_id"], expected)
            previous_ref = store.snapshot_ref(REFERENCE, "linux/amd64", "registry")
            previous_tar = adapter.read_bytes()
            replace = os.replace

            def fail_adapter(source, destination):
                if Path(destination) == adapter:
                    raise OSError("adapter replacement interrupted")
                return replace(source, destination)

            with patch("image_store.os.replace", side_effect=fail_adapter):
                with self.assertRaisesRegex(OSError, "adapter replacement interrupted"):
                    pull_image(REFERENCE, store=store_root, offline=True)
            self.assertEqual(adapter.read_bytes(), previous_tar)
            self.assertEqual(store.snapshot_ref(REFERENCE, "linux/amd64", "registry"), previous_ref)
            self.assertEqual(list(store_root.glob("tar-identity-*")), [])
            with patch("image_store.registry_pull", side_effect=AssertionError("unexpected download")):
                reused, downloaded = pull_image(REFERENCE, store=store_root, offline=True)
                self.assertFalse(downloaded)
                self.assertEqual(image_cli.inspect_archive(reused)["image_id"], expected)
                saved = root / "saved.tar"
                image_cli.save_archive(REFERENCE, saved, store_root, source="registry")
                self.assertEqual(image_cli.inspect_archive(saved)["image_id"], expected)
            self.assertEqual(store.snapshot_ref(REFERENCE, "linux/amd64", "registry"), previous_ref)


if __name__ == "__main__":
    unittest.main()
