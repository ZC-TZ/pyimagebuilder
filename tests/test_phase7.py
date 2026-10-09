import hashlib
import io
import json
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from builder import build
from errors import ArchiveError, BuildError
from image_reader import ImageArchiveReader
from oci_writer import CONFIG_TYPE, INDEX_TYPE, LAYER_TYPE, MANIFEST_TYPE, OCIImageWriter
from test_phase1 import make_base


class PhaseSevenTests(unittest.TestCase):
    def test_dual_format_matches_config_and_uncompressed_layers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = root / "context"
            context.mkdir()
            (context / "app.txt").write_bytes(b"payload")
            dockerfile = context / "Dockerfile"
            dockerfile.write_text("FROM scratch\nCOPY app.txt /app.txt\nCMD [\"/app.txt\"]\n")
            docker_tar = root / "result.tar"
            build(dockerfile, context, None, None, "example/dual:7", docker_tar,
                  image_format="both")
            oci_tar = root / "result.oci.tar"
            self.assertTrue(docker_tar.is_file())
            self.assertTrue(oci_tar.is_file())
            OCIImageWriter().verify(oci_tar, "example/dual:7")

            with tarfile.open(oci_tar, "r:") as archive:
                names = archive.getnames()
                self.assertEqual(names[:2], ["oci-layout", "index.json"])
                self.assertNotIn("manifest.json", names)
                index = json.load(archive.extractfile("index.json"))
                self.assertEqual(index["mediaType"], INDEX_TYPE)
                reference = index["manifests"][0]
                self.assertEqual(reference["mediaType"], MANIFEST_TYPE)
                self.assertEqual(reference["platform"], {"architecture": "amd64", "os": "linux"})
                self.assertEqual(reference["annotations"]["org.opencontainers.image.ref.name"],
                                 "example/dual:7")
                manifest = json.load(archive.extractfile("blobs/sha256/" +
                                                          reference["digest"].split(":")[1]))
                self.assertEqual(manifest["config"]["mediaType"], CONFIG_TYPE)
                self.assertEqual(manifest["layers"][0]["mediaType"], LAYER_TYPE)
                config_raw = archive.extractfile("blobs/sha256/" +
                                                 manifest["config"]["digest"].split(":")[1]).read()
                oci_config = json.loads(config_raw)
                layer = manifest["layers"][0]
                layer_raw = archive.extractfile("blobs/sha256/" + layer["digest"].split(":")[1]).read()
                self.assertEqual(layer["digest"], "sha256:" + hashlib.sha256(layer_raw).hexdigest())
                self.assertEqual(layer["digest"], oci_config["rootfs"]["diff_ids"][0])

            with tempfile.TemporaryDirectory() as extracted:
                docker_image = ImageArchiveReader(docker_tar, Path(extracted)).read("example/dual:7")
                self.assertEqual(docker_image.config, oci_config)
                self.assertEqual(docker_image.layers[0].read_bytes(), layer_raw)

    def test_oci_only_and_explicit_secondary_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = root / "context"
            context.mkdir()
            dockerfile = context / "Dockerfile"
            dockerfile.write_text("FROM scratch\nCMD [\"/bin/true\"]\n")
            only = root / "only.tar"
            build(dockerfile, context, None, None, "example/empty:7", only, image_format="oci")
            OCIImageWriter().verify(only, "example/empty:7")
            with tarfile.open(only, "r:") as archive:
                self.assertEqual(len(json.load(archive.extractfile("index.json"))["manifests"]), 1)
            docker = root / "docker.tar"
            named = root / "chosen-oci.tar"
            build(dockerfile, context, None, None, "example/empty:7", docker,
                  image_format="both", oci_output=named)
            OCIImageWriter().verify(named, "example/empty:7")
            self.assertFalse((root / "docker.oci.tar").exists())
            with self.assertRaises(BuildError):
                build(dockerfile, context, None, None, "example/empty:7", root / "bad.tar",
                      image_format="docker", oci_output=root / "other.tar")

    def test_base_layers_keep_order_in_oci_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = make_base(root)
            context = root / "context"
            context.mkdir()
            (context / "extra").write_bytes(b"new")
            dockerfile = context / "Dockerfile"
            dockerfile.write_text("FROM example/base:1\nCOPY extra /extra\n")
            output = root / "base-oci.tar"
            build(dockerfile, context, base, None, "example/base-oci:7", output,
                  image_format="oci")
            OCIImageWriter().verify(output, "example/base-oci:7")
            with tarfile.open(output, "r:") as archive:
                reference = json.load(archive.extractfile("index.json"))["manifests"][0]
                manifest = json.load(archive.extractfile(
                    "blobs/sha256/" + reference["digest"].split(":")[1]))
                config = json.load(archive.extractfile(
                    "blobs/sha256/" + manifest["config"]["digest"].split(":")[1]))
                self.assertEqual(len(manifest["layers"]), 3)
                self.assertEqual([layer["digest"] for layer in manifest["layers"]],
                                 config["rootfs"]["diff_ids"])

    def test_oci_verifier_rejects_changed_blob(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = root / "context"
            context.mkdir()
            (context / "payload").write_bytes(b"correct")
            dockerfile = context / "Dockerfile"
            dockerfile.write_text("FROM scratch\nCOPY payload /payload\n")
            good = root / "good.tar"
            build(dockerfile, context, None, None, "example/check:7", good, image_format="oci")
            bad = root / "bad.tar"
            with tarfile.open(good, "r:") as original, tarfile.open(bad, "w") as altered:
                reference = json.load(original.extractfile("index.json"))["manifests"][0]
                manifest_name = "blobs/sha256/" + reference["digest"].split(":")[1]
                manifest = json.load(original.extractfile(manifest_name))
                layer_name = "blobs/sha256/" + manifest["layers"][0]["digest"].split(":")[1]
                for member in original:
                    stream = original.extractfile(member)
                    data = stream.read() if stream else b""
                    if member.name == layer_name:
                        data = b"X" + data[1:]
                    copy = tarfile.TarInfo(member.name)
                    copy.size = len(data)
                    altered.addfile(copy, io.BytesIO(data))
            with self.assertRaises(ArchiveError):
                OCIImageWriter().verify(bad, "example/check:7")


if __name__ == "__main__":
    unittest.main()
