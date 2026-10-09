import hashlib
import io
import json
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

import ed25519
from attest import artifact_paths, keygen, verify_bundle
from builder import build
from errors import BuildError
from image_reader import sha256_file


class PhaseNineTests(unittest.TestCase):
    def test_rfc8032_vector_and_tampering(self):
        seed = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
        public = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
        signature = bytes.fromhex(
            "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901"
            "555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b")
        self.assertEqual(ed25519.public_key(seed), public)
        self.assertEqual(ed25519.sign(seed, b""), signature)
        self.assertTrue(ed25519.verify(public, b"", signature))
        self.assertFalse(ed25519.verify(public, b"changed", signature))
        self.assertFalse(ed25519.verify(public, b"", signature[:-1] + b"\x00"))

    def test_signed_bundle_verifies_and_detects_each_tamper(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = root / "context"
            context.mkdir()
            (context / "payload").write_bytes(b"payload")
            dockerfile = context / "Dockerfile"
            dockerfile.write_text("FROM scratch\nCOPY payload /payload\n")
            private, public = root / "private.hex", root / "public.hex"
            keygen(private, public)
            with self.assertRaises(FileExistsError):
                keygen(private, public)
            first = root / "first.tar"
            second = root / "second.tar"
            for output in (first, second):
                build(dockerfile, context, None, None, "example/attested:9", output,
                      image_format="both", sign_key=private)
            self.assertEqual(sha256_file(first), sha256_file(second))
            for left, right in zip(artifact_paths(first), artifact_paths(second)):
                self.assertEqual(left.read_bytes(), right.read_bytes())
            sbom, provenance, envelope = artifact_paths(first)
            oci = root / "first.oci.tar"
            self.assertTrue(verify_bundle(public, provenance, envelope, sbom, first, oci))
            self.assertEqual(json.loads(sbom.read_bytes())["files"][0]["fileName"], "./payload")
            self.assertEqual(len(json.loads(provenance.read_bytes())["subject"]), 2)

            def tamper_and_check(path):
                original = path.read_bytes()
                path.write_bytes(original + b"X")
                try:
                    with self.assertRaises(BuildError):
                        verify_bundle(public, provenance, envelope, sbom, first, oci)
                finally:
                    path.write_bytes(original)

            for path in (first, oci, sbom, provenance, envelope):
                tamper_and_check(path)
            original_envelope = envelope.read_bytes()
            try:
                for malformed in ([], {"payloadType": "application/vnd.in-toto+json",
                                       "payload": 123, "signatures": []},
                                  {"payloadType": "application/vnd.in-toto+json",
                                   "payload": "!", "signatures": []}):
                    envelope.write_text(json.dumps(malformed), encoding="utf-8")
                    with self.assertRaises(BuildError):
                        verify_bundle(public, provenance, envelope, sbom, first, oci)
            finally:
                envelope.write_bytes(original_envelope)
            wrong = root / "wrong.hex"
            wrong.write_text("00" * 32)
            with self.assertRaises(BuildError):
                verify_bundle(wrong, provenance, envelope, sbom, first, oci)
            with self.assertRaises(BuildError):
                verify_bundle(public, provenance, envelope, sbom, first)

    def test_unsigned_attestation_and_output_preflight(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = root / "context"
            context.mkdir()
            dockerfile = context / "Dockerfile"
            dockerfile.write_text("FROM scratch\n")
            output = root / "image.tar"
            build(dockerfile, context, None, None, "example/unsigned:9", output,
                  attest=True)
            sbom, provenance, envelope = artifact_paths(output)
            self.assertTrue(sbom.is_file() and provenance.is_file())
            self.assertFalse(envelope.exists())
            blocked = root / "blocked.tar"
            blocked_sbom = next(artifact_paths(blocked))
            blocked_sbom.write_text("existing")
            with self.assertRaises(BuildError):
                build(dockerfile, context, None, None, "example/unsigned:9", blocked,
                      attest=True)
            self.assertFalse(blocked.exists())

    def test_war_maven_metadata_is_in_sbom(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = root / "context"
            context.mkdir()
            with zipfile.ZipFile(context / "app.war", "w") as archive:
                archive.writestr("META-INF/maven/org.demo/app/pom.properties",
                                 "groupId=org.demo\nartifactId=app\nversion=1.2.3\n")
            dockerfile = context / "Dockerfile"
            dockerfile.write_text("FROM scratch\nCOPY app.war /app.war\n")
            output = root / "app.tar"
            build(dockerfile, context, None, None, "example/java:9", output, attest=True)
            sbom = json.loads(next(artifact_paths(output)).read_bytes())
            refs = [ref["referenceLocator"] for package in sbom["packages"]
                    for ref in package.get("externalRefs", [])]
            self.assertIn("pkg:maven/org.demo/app@1.2.3", refs)

    def test_sbom_uses_hardlink_bytes_before_target_is_replaced(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = root / "context"
            context.mkdir()
            with tarfile.open(context / "links.tar", "w") as archive:
                original = tarfile.TarInfo("original")
                original.size = 3
                archive.addfile(original, io.BytesIO(b"old"))
                alias = tarfile.TarInfo("alias")
                alias.type = tarfile.LNKTYPE
                alias.linkname = "original"
                archive.addfile(alias)
            (context / "replacement").write_bytes(b"new")
            dockerfile = context / "Dockerfile"
            dockerfile.write_text("FROM scratch\nADD links.tar /data/\n"
                                  "COPY replacement /data/original\n")
            private, public = root / "private.hex", root / "public.hex"
            keygen(private, public)
            output = root / "image.tar"
            build(dockerfile, context, None, None, "example/links:9", output,
                  sign_key=private)
            sbom_path, provenance, envelope = artifact_paths(output)
            self.assertTrue(verify_bundle(public, provenance, envelope, sbom_path, output))
            sbom = json.loads(sbom_path.read_bytes())
            checksums = {item["fileName"]: next(check["checksumValue"]
                         for check in item["checksums"] if check["algorithm"] == "SHA256")
                         for item in sbom["files"]}
            self.assertEqual(checksums["./data/alias"], hashlib.sha256(b"old").hexdigest())
            self.assertEqual(checksums["./data/original"], hashlib.sha256(b"new").hexdigest())


if __name__ == "__main__":
    unittest.main()
