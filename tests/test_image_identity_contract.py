"""用非规范 JSON 验证转存身份、无变化构建和真正派生构建的区别。"""

import copy
import gzip
import hashlib
import io
import json
import sys
import tarfile
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from builder import build
from cas_store import CASStore
from errors import ArchiveError, BuildError
from image_reader import BaseImage, ImageArchiveReader, archive_members
from image_writer import ImageArchiveWriter
from image_cli import load_archive, save_archive, tag_archive
from oci_writer import OCIImageWriter, CONFIG_TYPE, MANIFEST_TYPE, LAYER_TYPE
from optimizer import optimize_image
from registry import _prepare_remote_manifest, push_index
from registry import pull, OCI_GZIP_LAYER
from multiarch import combine
from artifactory_download import convert_registry_to_docker_archive, ProgressDisplay
from attest import artifact_paths, keygen, verify_bundle
from tests.test_phase1 import add_bytes, layer_file


TAG = "example/identity:1"


def digest(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def fixture(root, kind="docker", arch="amd64", empty=False, sparse=False, prefix=""):
    """构造缩进、中文、数值类型与额外字段皆非默认编码的合法镜像。"""
    layers = []
    if not empty:
        layer = root / (arch + "-layer.tar")
        layer_file(layer, {"payload": b"base"})
        layers.append(layer)
    config = {"x-example": {"unicode": "中文", "number": -0.0, "flag": True},
              "os": "linux", "architecture": arch,
              "rootfs": {"type": "layers", "diff_ids": [digest(p.read_bytes()) for p in layers]},
              "config": None if sparse else {},
              "history": [{"created_by": "original"} for _ in layers]}
    if sparse:
        del config["history"]
    raw = (json.dumps(config, ensure_ascii=True, indent=3) + "\n").encode()
    original = root / (kind + "-" + arch + "-canonical.tar")
    writer = ImageArchiveWriter() if kind == "docker" else OCIImageWriter()
    writer.write(original, config, layers, TAG, config_raw=raw)
    output = root / (kind + "-" + arch + ".tar")
    with tarfile.open(original) as archive:
        members = {member.name: archive.extractfile(member).read() for member in archive}
    if kind == "oci":
        index = json.loads(members["index.json"])
        entry = index["manifests"][0]
        old_name = "blobs/sha256/" + entry["digest"].split(":")[1]
        manifest = json.loads(members.pop(old_name))
        manifest["annotations"] = {"example.manifest": "保留"}
        for descriptor in manifest["layers"]:
            descriptor["annotations"] = {"example.layer": "保留"}
        manifest_raw = (json.dumps(manifest, ensure_ascii=True, indent=4) + "\n").encode()
        entry.update(digest=digest(manifest_raw), size=len(manifest_raw))
        entry["annotations"]["example.descriptor"] = "保留"
        index["annotations"] = {"example.index": "保留"}
        members["blobs/sha256/" + digest(manifest_raw).split(":")[1]] = manifest_raw
        members["index.json"] = (json.dumps(index, indent=2) + "\n").encode()
    with tarfile.open(output, "w") as archive:
        for name, content in members.items():
            add_bytes(archive, prefix + name, content)
    return output, config, raw


def payloads(path):
    """从实际归档读取原始配置与 OCI manifest/index，不用重新编码计算身份。"""
    with tarfile.open(path) as archive:
        members = archive_members(archive)
        def read(name):
            return archive.extractfile(members[name]).read()
        if "manifest.json" in members:
            entry = json.loads(read("manifest.json"))[0]
            return read(entry["Config"]), None, None
        index_raw = read("index.json")
        index = json.loads(index_raw)
        manifest_raw = read("blobs/sha256/" + index["manifests"][0]["digest"].split(":")[1])
        manifest = json.loads(manifest_raw)
        return read("blobs/sha256/" + manifest["config"]["digest"].split(":")[1]), manifest_raw, index_raw


class UploadClient:
    def __init__(self):
        self.reference = SimpleNamespace(full=TAG, version="1")
        self.uploads = []
        self.manifests = []

    def upload_file(self, path, descriptor):
        raw = Path(path).read_bytes()
        assert digest(raw) == descriptor["digest"]
        assert len(raw) == descriptor["size"]
        self.uploads.append((raw, descriptor))

    def _url(self, suffix):
        return suffix

    def _scope(self, access):
        return access

    def request(self, method, url, scope, **kwargs):
        raw = kwargs["body"]
        self.manifests.append(raw)
        return 201, {"docker-content-digest": digest(raw)}, b""


class ImageIdentityContractTests(unittest.TestCase):
    def test_manifest_transfer_requires_matching_config_and_declared_manifest_digest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, config, raw = fixture(root, "oci")
            config_raw, manifest_raw, _ = payloads(source)
            image = BaseImage(config, [root / "amd64-layer.tar"], [TAG], {}, digest(raw), config_raw,
                              manifest_raw=manifest_raw, manifest_digest=digest(manifest_raw))
            for wrong_digest in (True, False):
                with self.subTest(wrong_digest=wrong_digest):
                    invalid = copy.deepcopy(image)
                    if wrong_digest:
                        invalid.manifest_digest = "sha256:" + "0" * 64
                    else:
                        manifest = json.loads(manifest_raw)
                        manifest["config"]["digest"] = "sha256:" + "0" * 64
                        invalid.manifest_raw = json.dumps(manifest).encode()
                        invalid.manifest_digest = digest(invalid.manifest_raw)
                    output = root / "bad.tar"
                    with self.assertRaises(ArchiveError):
                        OCIImageWriter().write_image(output, invalid, TAG)
                    self.assertFalse(output.exists())

    def test_non_json_numeric_constants_are_rejected_before_writing(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                config = {"os": "linux", "architecture": "amd64", "config": {},
                          "rootfs": {"type": "layers", "diff_ids": []}, "x-value": value}
                for writer in (ImageArchiveWriter(), OCIImageWriter()):
                    for raw in (None, json.dumps(config).encode()):
                        output = root / "bad.tar"
                        with self.assertRaises(ArchiveError):
                            writer.write(output, config, [], TAG, config_raw=raw)
                        self.assertFalse(output.exists())

    def test_combine_preserves_child_blob_bytes_and_descriptor_annotations(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            amd, _, _ = fixture(root, "oci", "amd64")
            arm, _, _ = fixture(root, "oci", "arm64")
            output = root / "combined.tar"
            combine(amd, arm, output, "example/combined:2")
            with tarfile.open(output) as archive:
                index = json.load(archive.extractfile("index.json"))
                for item, source in zip(index["manifests"], (amd, arm)):
                    self.assertEqual(item["annotations"]["example.descriptor"], "保留")
                    self.assertEqual(archive.extractfile("blobs/sha256/" + item["digest"].split(":")[1]).read(),
                                     payloads(source)[1])

    def test_registry_to_oci_and_cas_build_keep_manifest_when_unchanged(self):
        for compressed in (False, True):
            with self.subTest(compressed=compressed), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source, _, raw = fixture(root, "oci")
                original_manifest = payloads(source)[1]
                manifest = json.loads(original_manifest)
                with tarfile.open(source) as archive:
                    blobs = {"sha256:" + item.name.split("/")[-1]: archive.extractfile(item).read()
                             for item in archive if item.name.startswith("blobs/")}
                if compressed:
                    entry = manifest["layers"][0]
                    data = io.BytesIO()
                    with gzip.GzipFile(fileobj=data, mode="wb", mtime=0) as target:
                        target.write(blobs[entry["digest"]])
                    blob = data.getvalue()
                    entry.update(mediaType=OCI_GZIP_LAYER, digest=digest(blob), size=len(blob))
                    blobs[digest(blob)] = blob
                    original_manifest = (json.dumps(manifest, indent=4) + "\n").encode()

                class Handler(BaseHTTPRequestHandler):
                    def log_message(self, *_args):
                        pass

                    def do_GET(self):
                        body = (original_manifest if "/manifests/" in self.path else
                                blobs[self.path.rsplit("/", 1)[-1]])
                        self.send_response(200)
                        self.send_header("Content-Length", str(len(body)))
                        if "/manifests/" in self.path:
                            self.send_header("Content-Type", MANIFEST_TYPE)
                            self.send_header("Docker-Content-Digest", digest(body))
                        self.end_headers()
                        self.wfile.write(body)

                server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    reference = "127.0.0.1:{}/p/app:1".format(server.server_port)
                    store = CASStore(root / "store")
                    output = root / "pulled.oci.tar"
                    pull(reference, output, insecure_http=True, image_format="oci", cas_store=store)
                    self.assertEqual(payloads(output)[0], raw)
                    pulled_manifest = payloads(output)[1]
                    if compressed:
                        self.assertNotEqual(digest(pulled_manifest), digest(original_manifest))
                    else:
                        self.assertEqual(pulled_manifest, original_manifest)
                    self.assertEqual(json.loads(pulled_manifest)["annotations"], manifest["annotations"])
                    self.assertEqual(json.loads(pulled_manifest)["layers"][0]["annotations"],
                                     manifest["layers"][0]["annotations"])
                    ref = store.resolve(reference, source="registry")
                    self.assertEqual(store._blob(ref["manifest"]).read_bytes(), original_manifest)
                    context = root / "context"
                    context.mkdir()
                    dockerfile = context / "Dockerfile"
                    dockerfile.write_text("FROM " + reference + " AS first\nFROM first\n")
                    built = root / "built.oci.tar"
                    base = {"type": "cas", "store": str(store.root.parent), "source": "registry",
                            "reference": reference, "platform": "linux/amd64"}
                    build(dockerfile, context, base, None, "example/new:2", built,
                          image_format="oci")
                    self.assertEqual(payloads(built)[0], raw)
                    self.assertEqual(payloads(built)[1], pulled_manifest)
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=3)

    def test_actual_oci_pruning_changes_identity_but_preserves_extension_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, config, raw = fixture(root, "oci")
            newer = root / "newer.tar"
            layer_file(newer, {"payload": b"new"})
            with tarfile.open(source) as archive:
                members = {member.name: archive.extractfile(member).read() for member in archive}
            index = json.loads(members["index.json"])
            manifest = json.loads(payloads(source)[1])
            config["rootfs"]["diff_ids"].append(digest(newer.read_bytes()))
            config["history"].append({"created_by": "new"})
            changed_raw = (json.dumps(config, indent=3) + "\n").encode()
            old_config = "blobs/sha256/" + digest(raw).split(":")[1]
            old_manifest = "blobs/sha256/" + index["manifests"][0]["digest"].split(":")[1]
            del members[old_config]
            del members[old_manifest]
            manifest["config"].update(digest=digest(changed_raw), size=len(changed_raw))
            manifest["config"]["annotations"] = {"example.config": "preserve"}
            manifest["layers"].append({"mediaType": LAYER_TYPE, "digest": digest(newer.read_bytes()),
                                        "size": newer.stat().st_size,
                                        "annotations": {"example.new-layer": "preserve"}})
            manifest_raw = json.dumps(manifest, indent=2).encode()
            index["manifests"][0].update(digest=digest(manifest_raw), size=len(manifest_raw))
            members["blobs/sha256/" + digest(changed_raw).split(":")[1]] = changed_raw
            members["blobs/sha256/" + digest(manifest_raw).split(":")[1]] = manifest_raw
            members["blobs/sha256/" + digest(newer.read_bytes()).split(":")[1]] = newer.read_bytes()
            members["index.json"] = json.dumps(index, indent=4).encode()
            with tarfile.open(source, "w") as archive:
                for name, content in members.items():
                    add_bytes(archive, name, content)
            output = root / "optimized.tar"
            report = optimize_image(source, output)
            self.assertEqual(report["removed_layer_numbers"], [1])
            cfg_raw, result_manifest, result_index = payloads(output)
            self.assertNotEqual(digest(cfg_raw), digest(changed_raw))
            result_manifest, result_index = json.loads(result_manifest), json.loads(result_index)
            self.assertEqual(result_manifest["annotations"], manifest["annotations"])
            self.assertEqual(result_manifest["config"]["annotations"], manifest["config"]["annotations"])
            self.assertEqual(result_manifest["layers"][0]["annotations"], manifest["layers"][1]["annotations"])
            self.assertEqual(result_index["annotations"], index["annotations"])
            self.assertEqual(result_index["manifests"][0]["annotations"]["example.descriptor"], "保留")

    def test_artifactory_conversion_keeps_raw_config_and_original_manifest_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, _, raw = fixture(root, "oci")
            raw_root = root / "raw"
            raw_root.mkdir()
            manifest_raw = payloads(source)[1]
            (raw_root / "manifest.json").write_bytes(manifest_raw)
            with tarfile.open(source) as archive:
                for member in archive:
                    if member.name.startswith("blobs/"):
                        (raw_root / member.name.split("/")[-1]).write_bytes(archive.extractfile(member).read())
            destination = root / "converted"
            convert_registry_to_docker_archive(raw_root, destination, TAG, ProgressDisplay())
            self.assertEqual((destination / (digest(raw).split(":")[1] + ".json")).read_bytes(), raw)
            self.assertEqual((raw_root / "manifest.json").read_bytes(), manifest_raw)

    def test_noop_attested_build_preserves_id_and_signs_actual_output_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, _, raw = fixture(root, sparse=True)
            context = root / "context"
            context.mkdir()
            dockerfile = context / "Dockerfile"
            dockerfile.write_text("FROM " + TAG + "\n")
            private, public = root / "private.hex", root / "public.hex"
            keygen(private, public)
            output = root / "signed.tar"
            build(dockerfile, context, source, None, "example/signed:1", output,
                  image_format="both", sign_key=private)
            sbom, provenance, envelope = artifact_paths(output)
            self.assertTrue(verify_bundle(public, provenance, envelope, sbom, output, root / "signed.oci.tar"))
            self.assertEqual(payloads(output)[0], raw)
            self.assertEqual(payloads(root / "signed.oci.tar")[0], raw)

    def test_transfer_rejects_modified_parsed_value_even_with_original_digest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, _, _ = fixture(root)
            image = ImageArchiveReader(source, root / "read").read(TAG)
            image.config["x-example"]["flag"] = 1
            for writer in (ImageArchiveWriter(), OCIImageWriter()):
                output = root / "modified.tar"
                with self.assertRaises(ArchiveError):
                    writer.write_image(output, image, TAG)
                self.assertFalse(output.exists())

    def test_noop_optimization_keeps_config_and_oci_manifest_index_bytes(self):
        for kind in ("docker", "oci"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source, _, _ = fixture(root, kind)
                output = root / "optimized.tar"
                report = optimize_image(source, output)
                self.assertEqual(report["removed_layer_numbers"], [])
                self.assertEqual(payloads(output), payloads(source))

    def test_optimizer_accepts_normalized_archive_names_without_changing_identity(self):
        for kind in ("docker", "oci"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source, _, _ = fixture(root, kind, prefix="././")
                output = root / "optimized.tar"
                optimize_image(source, output)
                self.assertEqual(payloads(output), payloads(source))

    def test_from_only_build_keeps_raw_identity_across_formats_and_stages(self):
        for sparse in (False, True):
            for stages in (False, True):
                with self.subTest(sparse=sparse, stages=stages), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    source, _, raw = fixture(root, sparse=sparse)
                    context = root / "context"
                    context.mkdir()
                    dockerfile = context / "Dockerfile"
                    dockerfile.write_text("ARG BASE=" + TAG + "\nFROM ${BASE} AS original\n" +
                                          ("FROM original AS final\n" if stages else ""))
                    output = root / "built.tar"
                    build(dockerfile, context, source, None, "example/retagged:2", output,
                          image_format="both", source_date_epoch=42)
                    self.assertEqual(payloads(output)[0], raw)
                    self.assertEqual(payloads(root / "built.oci.tar")[0], raw)

    def test_real_copy_build_generates_new_config_and_keeps_original_extension(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, config, raw = fixture(root, sparse=True)
            context = root / "context"
            context.mkdir()
            (context / "app.war").write_bytes(b"WAR")
            dockerfile = context / "Dockerfile"
            dockerfile.write_text("FROM " + TAG + "\nCOPY app.war /app.war\n")
            output = root / "built.tar"
            build(dockerfile, context, source, None, "example/app:2", output)
            image = ImageArchiveReader(output, root / "read").read("example/app:2")
            self.assertNotEqual(image.config_digest, digest(raw))
            self.assertEqual(image.config["x-example"], config["x-example"])
            self.assertEqual(len(image.layers), 2)

    def test_transfer_api_requires_original_bytes_and_matches_declared_digest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, _, raw = fixture(root)
            image = ImageArchiveReader(source, root / "read").read(TAG)
            for writer in (ImageArchiveWriter(), OCIImageWriter()):
                for bad_raw, bad_digest in ((None, digest(raw)), (raw, "sha256:" + "0" * 64)):
                    with self.subTest(writer=type(writer).__name__, bad_raw=bad_raw is None):
                        invalid = copy.deepcopy(image)
                        invalid.config_raw, invalid.config_digest = bad_raw, bad_digest
                        output = root / "bad.tar"
                        with self.assertRaises(ArchiveError):
                            writer.write_image(output, invalid, TAG)
                        self.assertFalse(output.exists())

    def test_raw_guard_differentiates_bool_int_float_and_signed_zero(self):
        for actual, altered in ((True, 1), (1, 1.0), (-0.0, 0.0)):
            with self.subTest(actual=repr(actual), altered=repr(altered)), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                config = {"os": "linux", "architecture": "amd64", "rootfs": {"type": "layers", "diff_ids": []},
                          "x-example": {"value": actual}}
                raw_value = copy.deepcopy(config)
                raw_value["x-example"]["value"] = altered
                for writer in (ImageArchiveWriter(), OCIImageWriter()):
                    output = root / "bad.tar"
                    with self.assertRaises(ArchiveError):
                        writer.write(output, config, [], TAG, config_raw=json.dumps(raw_value).encode())
                    self.assertFalse(output.exists())

    def test_cas_docker_import_uses_the_config_bytes_it_verified(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, _, raw = fixture(root)
            reader = ImageArchiveReader.read
            def replaced_archive(instance, *args, **kwargs):
                image = reader(instance, *args, **kwargs)
                with tarfile.open(source) as archive:
                    members = {member.name: archive.extractfile(member).read() for member in archive}
                entry = json.loads(members["manifest.json"])[0]
                changed = copy.deepcopy(image.config)
                changed["x-example"]["unicode"] = "changed after verification"
                members[entry["Config"]] = json.dumps(changed).encode()
                with tarfile.open(source, "w") as archive:
                    for name, content in members.items():
                        add_bytes(archive, name, content)
                return image
            store = CASStore(root / "store")
            with patch.object(ImageArchiveReader, "read", replaced_archive):
                value = store.import_docker(source, TAG, "linux/amd64")
            self.assertEqual(value["config"], digest(raw))
            self.assertEqual(store.open_base(TAG).config_raw, raw)

    def test_cas_oci_import_does_not_publish_replaced_config_or_manifest(self):
        for changed_kind in ("config", "manifest"):
            with self.subTest(changed_kind=changed_kind), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source, _, _ = fixture(root, "oci")
                verify = OCIImageWriter.verify
                def replaced_archive(instance, path, tag):
                    result = verify(instance, path, tag)
                    with tarfile.open(source) as archive:
                        members = {member.name: archive.extractfile(member).read() for member in archive}
                    entry = json.loads(members["index.json"])["manifests"][0]
                    manifest_name = "blobs/sha256/" + entry["digest"].split(":")[1]
                    manifest = json.loads(members[manifest_name])
                    name = manifest_name if changed_kind == "manifest" else (
                        "blobs/sha256/" + manifest["config"]["digest"].split(":")[1])
                    members[name] += b"\n"
                    with tarfile.open(source, "w") as archive:
                        for key, content in members.items():
                            add_bytes(archive, key, content)
                    return result
                store = CASStore(root / "store")
                with patch.object(OCIImageWriter, "verify", replaced_archive):
                    with self.assertRaises(ArchiveError):
                        store.import_oci(source, TAG, "linux/amd64")
                self.assertFalse(store.has_ref(TAG, "linux/amd64", "local"))

    def test_cas_publication_rejects_mismatched_raw_manifest_before_ref(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, _, _ = fixture(root)
            store = CASStore(root / "store")
            value = store.import_docker(source, TAG, "linux/amd64")
            manifest = json.loads(store._blob(value["manifest"]).read_bytes())
            manifest["config"]["digest"] = "sha256:" + "0" * 64
            with self.assertRaises(ArchiveError):
                store._publish("example/bad:1", "linux/amd64", "local", value["config"],
                               value["layers"], manifest_raw=json.dumps(manifest).encode())
            self.assertFalse(store.has_ref("example/bad:1", "linux/amd64", "local"))

    def test_zero_layer_push_preserves_original_manifest_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, _, raw = fixture(root, "oci", empty=True)
            client = UploadClient()
            with tarfile.open(source) as archive:
                index = json.load(archive.extractfile("index.json"))
                remote = _prepare_remote_manifest(client, archive, index["manifests"][0], root, "push")
            self.assertEqual(remote, payloads(source)[1])
            self.assertEqual(client.uploads[0][0], raw)

    def test_push_compression_keeps_descriptor_annotations_and_config_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, _, raw = fixture(root, "oci")
            client = UploadClient()
            with tarfile.open(source) as archive:
                index = json.load(archive.extractfile("index.json"))
                remote = _prepare_remote_manifest(client, archive, index["manifests"][0], root, "push")
            manifest = json.loads(remote)
            original = json.loads(payloads(source)[1])
            self.assertNotEqual(digest(remote), digest(payloads(source)[1]))
            self.assertEqual(manifest["config"], original["config"])
            self.assertEqual(manifest["annotations"], original["annotations"])
            self.assertEqual(manifest["layers"][0]["annotations"], original["layers"][0]["annotations"])
            self.assertEqual(client.uploads[0][0], raw)

    def test_push_index_keeps_index_and_descriptor_extensions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            amd, _, _ = fixture(root, "oci", "amd64", empty=True)
            arm, _, _ = fixture(root, "oci", "arm64", empty=True)
            combined = root / "multi.tar"
            combine(amd, arm, combined, TAG)
            with tarfile.open(combined) as archive:
                members = {member.name: archive.extractfile(member).read() for member in archive}
            index = json.loads(members["index.json"])
            index["annotations"] = {"example.index": "preserve"}
            for item in index["manifests"]:
                item["annotations"]["example.descriptor"] = "preserve"
            members["index.json"] = (json.dumps(index, indent=4) + "\n").encode()
            with tarfile.open(combined, "w") as archive:
                for name, raw in members.items():
                    add_bytes(archive, name, raw)
            client = UploadClient()
            with patch("registry.RegistryClient", return_value=client), patch("registry._check_push_tag"):
                push_index(combined, TAG)
            self.assertEqual(client.manifests[-1], members["index.json"])

    def test_load_retag_save_preserves_config_after_cas_only_reexport(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, _, raw = fixture(root)
            tagged = root / "tagged.tar"
            tag_archive(source, "example/retagged:2", tagged)
            store_root = root / "store"
            load_archive(tagged, store_root)
            output = root / "saved.tar"
            CASStore(store_root).export_docker("example/retagged:2", output)
            self.assertEqual(payloads(output)[0], raw)
            saved = root / "saved-again.tar"
            save_archive("example/retagged:2", saved, store_root)
            self.assertEqual(payloads(saved)[0], raw)


if __name__ == "__main__":
    unittest.main()
