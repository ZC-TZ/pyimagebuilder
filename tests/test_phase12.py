import json
import io
import copy
import threading
import sys
import tarfile
import tempfile
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from builder import build
from dockerfile_parser import parse
from errors import BaseImageNotFound, BuildError, UnsupportedInstruction
from image_reader import ImageArchiveReader
from oci_writer import OCIImageWriter
from multiarch import combine, verify as verify_multiarch
from registry import pull, push_index
from test_phase11 import RegistryHandler


class PhaseTwelveTests(unittest.TestCase):
    def test_from_platform_parser(self):
        parsed = parse("FROM --platform=linux/arm64 example/base:1 AS build\n"
                       "FROM --platform=linux/amd64 scratch\n")
        self.assertEqual(parsed[0].value.platform, "linux/arm64")
        self.assertEqual(parsed[0].value.alias, "build")
        self.assertEqual(parsed[1].value.platform, "linux/amd64")
        with self.assertRaises(UnsupportedInstruction):
            parse("FROM --platform=windows/arm64 scratch\n")

    def test_arm64_scratch_and_base_on_amd64_host(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = root / "context"
            context.mkdir()
            (context / "payload").write_bytes(b"arm64 artifact")
            dockerfile = context / "Dockerfile"
            dockerfile.write_text("FROM scratch\nCOPY payload /payload\n")
            base = root / "base.tar"
            build(dockerfile, context, None, None, "example/arm:1", base,
                  target_platform="linux/arm64")
            with tempfile.TemporaryDirectory() as unpacked:
                image = ImageArchiveReader(base, Path(unpacked)).read(
                    "example/arm:1", "linux/arm64")
                self.assertEqual(image.config["architecture"], "arm64")
            with tempfile.TemporaryDirectory() as unpacked:
                with self.assertRaises(BaseImageNotFound):
                    ImageArchiveReader(base, Path(unpacked)).read(
                        "example/arm:1", "linux/amd64")
            dockerfile.write_text("FROM example/arm:1\nCOPY payload /another\n")
            output = root / "derived.oci.tar"
            build(dockerfile, context, base, None, "example/arm:2", output,
                  image_format="oci", target_platform="linux/arm64")
            OCIImageWriter().verify(output, "example/arm:2")
            with tarfile.open(output, "r:") as archive:
                index = json.load(archive.extractfile("index.json"))
                self.assertEqual(index["manifests"][0]["platform"],
                                 {"architecture": "arm64", "os": "linux"})
            with self.assertRaises(BuildError):
                build(dockerfile, context, base, None, "example/wrong:1",
                      root / "wrong.tar", target_platform="linux/amd64")

    def test_platform_specific_base_map_and_cross_stage_copy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = root / "context"
            context.mkdir()
            (context / "amd").write_bytes(b"amd64")
            (context / "arm").write_bytes(b"arm64")
            dockerfile = context / "Dockerfile"
            dockerfile.write_text("FROM scratch\nCOPY amd /identity\n")
            amd = root / "amd.tar"
            build(dockerfile, context, None, None, "example/base:1", amd)
            dockerfile.write_text("FROM scratch\nCOPY arm /identity\n")
            arm = root / "arm.tar"
            build(dockerfile, context, None, None, "example/base:1", arm,
                  target_platform="linux/arm64")
            mapping = root / "base-map.json"
            mapping.write_text(json.dumps({"example/base:1": {
                "linux/amd64": str(amd), "linux/arm64": str(arm)}}))
            combined = root / "both-bases.tar"
            with tarfile.open(amd, "r:") as first, tarfile.open(arm, "r:") as second, \
                 tarfile.open(combined, "w") as target:
                manifests = (json.load(first.extractfile("manifest.json")) +
                             json.load(second.extractfile("manifest.json")))
                raw = json.dumps(manifests).encode()
                header = tarfile.TarInfo("manifest.json")
                header.size = len(raw)
                target.addfile(header, io.BytesIO(raw))
                seen = {"manifest.json"}
                for source in (first, second):
                    for member in source:
                        if member.name in seen:
                            continue
                        seen.add(member.name)
                        target.addfile(copy.copy(member), source.extractfile(member)
                                       if member.isfile() else None)
            with tempfile.TemporaryDirectory() as unpacked:
                selected = ImageArchiveReader(combined, Path(unpacked)).read(
                    "example/base:1", "linux/arm64")
                self.assertEqual(selected.config["architecture"], "arm64")
            dockerfile.write_text("FROM --platform=linux/amd64 example/base:1 AS build\n"
                                  "FROM --platform=linux/arm64 example/base:1\n"
                                  "COPY --from=build /identity /amd-identity\n")
            output = root / "mixed.tar"
            build(dockerfile, context, None, mapping, "example/mixed:1", output,
                  target_platform="linux/arm64")
            with tempfile.TemporaryDirectory() as unpacked:
                image = ImageArchiveReader(output, Path(unpacked)).read(
                    "example/mixed:1", "linux/arm64")
                self.assertEqual(image.config["architecture"], "arm64")

    def test_foreign_run_requires_explicit_emulation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = root / "context"
            context.mkdir()
            dockerfile = context / "Dockerfile"
            dockerfile.write_text("FROM scratch\nRUN true\n")
            with self.assertRaises(UnsupportedInstruction):
                build(dockerfile, context, None, None, "example/foreign:1",
                      root / "foreign.tar", enable_run=True,
                      target_platform="linux/arm64")
            self.assertFalse((root / "foreign.tar").exists())

    def test_multiarch_layout_and_registry_index(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = root / "context"
            context.mkdir()
            (context / "payload").write_bytes(b"same bytes")
            dockerfile = context / "Dockerfile"
            dockerfile.write_text("FROM scratch\nCOPY payload /payload\n")
            amd, arm = root / "amd.oci.tar", root / "arm.oci.tar"
            build(dockerfile, context, None, None, "example/amd:1", amd,
                  image_format="oci", target_platform="linux/amd64")
            build(dockerfile, context, None, None, "example/arm:1", arm,
                  image_format="oci", target_platform="linux/arm64")
            first, second = root / "multi1.tar", root / "multi2.tar"
            combine(amd, arm, first, "example/multi:1")
            combine(amd, arm, second, "example/multi:1")
            self.assertEqual(first.read_bytes(), second.read_bytes())
            verify_multiarch(first, "example/multi:1")
            RegistryHandler.blobs = {}
            RegistryHandler.manifests = {}
            RegistryHandler.token_requests = 0
            try:
                server = ThreadingHTTPServer(("127.0.0.1", 0), RegistryHandler)
            except OSError as exc:
                self.skipTest("Local socket unavailable: " + str(exc))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                reference = "127.0.0.1:{}/p/app:multi".format(server.server_port)
                digest = push_index(first, reference, "robot$test", "secret",
                                    insecure_http=True)
                self.assertTrue(digest.startswith("sha256:"))
                remote_index = json.loads(RegistryHandler.manifests["multi"])
                self.assertEqual([item["platform"]["architecture"]
                                  for item in remote_index["manifests"]], ["amd64", "arm64"])
                for arch in ("amd64", "arm64"):
                    output = root / ("pulled-" + arch + ".tar")
                    pull(reference, output, "robot$test", "secret", insecure_http=True,
                         target_platform="linux/" + arch)
                    with tempfile.TemporaryDirectory() as unpacked:
                        image = ImageArchiveReader(output, Path(unpacked)).read(
                            reference, "linux/" + arch)
                        self.assertEqual(image.config["architecture"], arch)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
