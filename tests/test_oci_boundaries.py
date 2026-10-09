"""验证 OCI 原始身份、归档路径别名及 CAS/Registry/多平台消费的一致性。"""

import contextlib
import hashlib
import io
import json
import sys
import tarfile
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from builder import build
from cas_store import CASStore
from errors import ArchiveError
from image_reader import ImageArchiveReader
from multiarch import combine, verify as verify_multiarch
from oci_writer import OCIImageWriter
from registry import pull, push, push_index
from rootfs import RootFSIndex
from test_phase11 import RegistryHandler


TAG = "example/app:1"


def digest(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def make_oci(root, architecture="amd64"):
    layer = root / (architecture + "-layer.tar")
    payload = architecture.encode()
    with tarfile.open(layer, "w") as archive:
        member = tarfile.TarInfo("payload")
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))
    config = {"os": "linux", "architecture": architecture, "config": {},
              "rootfs": {"type": "layers", "diff_ids": [digest(layer.read_bytes())]}}
    output = root / (architecture + ".oci.tar")
    OCIImageWriter().write(output, config, [layer], TAG)
    return output


def rewrite_oci(source, output, prefix="", preserve_identity_case=False):
    """仅改变外层成员名，或生成带额外注解和格式空白的合法 manifest。"""
    with tarfile.open(source) as archive:
        data = {member.name: archive.extractfile(member).read() for member in archive}
    index = json.loads(data["index.json"])
    descriptor = index["manifests"][0]
    name = "blobs/sha256/" + descriptor["digest"].split(":")[1]
    manifest_raw = data[name]
    if preserve_identity_case:
        manifest = json.loads(data.pop(name))
        manifest["annotations"] = {"example.description": "内网应用"}
        manifest_raw = (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode()
        descriptor["digest"], descriptor["size"] = digest(manifest_raw), len(manifest_raw)
        data["blobs/sha256/" + descriptor["digest"].split(":")[1]] = manifest_raw
        data["index.json"] = json.dumps(index).encode()
    with tarfile.open(output, "w") as archive:
        for name, raw in sorted(data.items()):
            member = tarfile.TarInfo(prefix + name)
            member.size = len(raw)
            archive.addfile(member, io.BytesIO(raw))
    return manifest_raw


@contextlib.contextmanager
def local_registry():
    RegistryHandler.blobs, RegistryHandler.manifests = {}, {}
    RegistryHandler.token_requests = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), RegistryHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "127.0.0.1:{}/p/app:1".format(server.server_port)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


class OCIBoundaryTests(unittest.TestCase):
    def read_payload(self, image):
        index = RootFSIndex()
        for layer in image.layers:
            index.apply_layer(layer)
        return index.read_file("payload")

    def paired_image(self, root):
        amd64, arm64 = make_oci(root), make_oci(root, "arm64")
        output = root / "multi.tar"
        combine(amd64, arm64, output, TAG)
        return output

    def test_import_keeps_original_manifest_bytes_and_digest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "annotated.tar"
            raw = rewrite_oci(make_oci(root), source, preserve_identity_case=True)
            OCIImageWriter().verify(source, TAG)
            cas = CASStore(root / "store")
            ref = cas.import_oci(source, TAG, "linux/amd64")
            self.assertEqual(ref["manifest"], digest(raw))
            self.assertEqual(cas._blob(ref["manifest"]).read_bytes(), raw)
            self.assertEqual(cas.resolve(TAG, source="local"), ref)
            self.assertEqual(self.read_payload(cas.open_base(TAG, source="local")), b"amd64")

    def test_prefixed_import_export_and_derived_build(self):
        for prefix in ("./", "././"):
            with self.subTest(prefix=prefix), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / "prefixed.tar"
                raw = rewrite_oci(make_oci(root), source, prefix, preserve_identity_case=True)
                OCIImageWriter().verify(source, TAG)
                cas = CASStore(root / "store")
                cas.import_oci(source, TAG, "linux/amd64")
                self.assertEqual(cas.resolve(TAG, source="local")["manifest"], digest(raw))
                exported = cas.export_docker(TAG, root / "exported.tar", source="local")
                image = ImageArchiveReader(exported, root / "layers").read(TAG)
                self.assertEqual(self.read_payload(image), b"amd64")
                context = root / "context"
                context.mkdir()
                (context / "Dockerfile").write_text("FROM " + TAG + "\nENV AUDIT=1\n")
                output = root / "derived.tar"
                base = {"type": "cas", "store": str(root / "store"), "reference": TAG,
                        "platform": "linux/amd64", "source": "local"}
                build(context / "Dockerfile", context, base, None, "example/derived:1", output)
                derived = ImageArchiveReader(output, root / "derived-layers").read("example/derived:1")
                self.assertEqual(self.read_payload(derived), b"amd64")
                self.assertIn("AUDIT=1", derived.config["config"]["Env"])

    def test_prefixed_single_push_and_pull(self):
        with tempfile.TemporaryDirectory() as temporary, local_registry() as reference:
            root = Path(temporary)
            source = root / "prefixed.tar"
            rewrite_oci(make_oci(root), source, "./")
            push(source, reference, "robot$test", "secret", insecure_http=True)
            output = root / "pulled.tar"
            pull(reference, output, "robot$test", "secret", insecure_http=True)
            image = ImageArchiveReader(output, root / "layers").read(reference)
            self.assertEqual(self.read_payload(image), b"amd64")

    def test_prefixed_sources_combine_to_identical_archive(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            canonical = self.paired_image(root)
            sources = []
            for architecture in ("amd64", "arm64"):
                path = root / (architecture + "-prefixed.tar")
                rewrite_oci(root / (architecture + ".oci.tar"), path, "./")
                sources.append(path)
            output = root / "combined.tar"
            combine(sources[0], sources[1], output, TAG)
            verify_multiarch(output, TAG)
            self.assertEqual(output.read_bytes(), canonical.read_bytes())

    def test_prefixed_multi_platform_verify(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "prefixed-multi.tar"
            rewrite_oci(self.paired_image(root), source, "./")
            verify_multiarch(source, TAG)

    def test_prefixed_multi_push_and_platform_pull(self):
        with tempfile.TemporaryDirectory() as temporary, local_registry() as reference:
            root = Path(temporary)
            source = root / "prefixed-multi.tar"
            rewrite_oci(self.paired_image(root), source, "./")
            push_index(source, reference, "robot$test", "secret", insecure_http=True)
            for architecture in ("amd64", "arm64"):
                output = root / (architecture + "-pulled.tar")
                pull(reference, output, "robot$test", "secret", insecure_http=True,
                     target_platform="linux/" + architecture)
                image = ImageArchiveReader(output, root / (architecture + "-layers")).read(
                    reference, "linux/" + architecture)
                self.assertEqual(self.read_payload(image), architecture.encode())

    def test_normalized_duplicate_index_is_rejected(self):
        for multi in (False, True):
            with self.subTest(multi=multi), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / "duplicate.tar"
                rewrite_oci(self.paired_image(root) if multi else make_oci(root), source, "./")
                with tarfile.open(source) as archive:
                    raw = archive.extractfile("./index.json").read()
                with tarfile.open(source, "a") as archive:
                    member = tarfile.TarInfo("index.json")
                    member.size = len(raw)
                    archive.addfile(member, io.BytesIO(raw))
                if multi:
                    with self.assertRaisesRegex(ArchiveError, "Duplicate"):
                        verify_multiarch(source, TAG)
                else:
                    cas = CASStore(root / "store")
                    with self.assertRaisesRegex(ArchiveError, "Duplicate"):
                        cas.import_oci(source, TAG, "linux/amd64")
                    self.assertFalse(cas.has_ref(TAG, "linux/amd64", "local"))


if __name__ == "__main__":
    unittest.main()
