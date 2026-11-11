"""回归优化器的源字节绑定、平台选择和失败发布边界。"""

import contextlib
import io
import json
import shutil
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import image_cli
import optimizer
import web_delivery_worker as worker
from cas_store import CASStore
from file_publish import publication_transaction, publish_new_file
from errors import ArchiveError, BaseImageNotFound
from image_reader import DigestReader
from image_writer import ImageArchiveWriter
from oci_writer import OCIImageWriter
from test_image_identity_contract import TAG, add_bytes, fixture, payloads
from test_optimizer_links import image as make_image, TAG as LAYER_TAG


def members(path):
    with tarfile.open(path, "r:") as archive:
        return {item.name: archive.extractfile(item).read() for item in archive}


def write_members(path, items):
    with tarfile.open(path, "w") as archive:
        for name, raw in items.items():
            add_bytes(archive, name, raw)


class OptimizerAuditTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="optimizer-audit-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_actual_oci_layer_copy_must_match_verified_digest(self):
        source, _, _ = fixture(self.root, "oci")
        copy = optimizer.shutil.copyfileobj

        def changed_payload(reader, target, size):
            if isinstance(reader, DigestReader):
                raw = reader.raw.read()
                reader.raw = io.BytesIO(bytes([raw[0] ^ 1]) + raw[1:])
            return copy(reader, target, size)

        output = self.root / "optimized.tar"
        with patch.object(optimizer.shutil, "copyfileobj", side_effect=changed_payload):
            with self.assertRaisesRegex(ArchiveError, "layer content changed"):
                optimizer.optimize_image(source, output)
        self.assertFalse(output.exists())

    def test_oci_metadata_changed_after_initial_verification_is_rejected(self):
        source, _, _ = fixture(self.root, "oci")
        payload = members(source)
        offset = None
        with tarfile.open(source, "r:") as archive:
            offset = archive.getmember("index.json").offset_data
        replacement = payload["index.json"].replace(b"example.index", b"changed.index")
        self.assertNotEqual(replacement, payload["index.json"])
        verify = OCIImageWriter.verify

        def changed_index(writer, path, tag, **kwargs):
            snapshot = verify(writer, path, tag, **kwargs)
            if Path(path) == source:
                with source.open("r+b") as stream:
                    stream.seek(offset)
                    stream.write(replacement)
            return snapshot

        output = self.root / "optimized.tar"
        with patch.object(OCIImageWriter, "verify", changed_index):
            with self.assertRaisesRegex(ArchiveError, "metadata changed|Source archive changed"):
                optimizer.optimize_image(source, output)
        self.assertFalse(output.exists())

    def test_noop_oci_output_must_preserve_raw_index_not_only_rootfs(self):
        source, _, _ = fixture(self.root, "oci")
        items = members(source)
        items["index.json"] = items["index.json"].replace(b"example.index", b"changed.index")
        replacement = self.root / "replacement.tar"
        write_members(replacement, items)
        copy = optimizer.shutil.copyfileobj

        def replace_final(reader, target, size):
            if Path(target.name).name == "optimized.tar":
                with replacement.open("rb") as other:
                    return copy(other, target, size)
            return copy(reader, target, size)

        output = self.root / "optimized.tar"
        with patch.object(optimizer.shutil, "copyfileobj", side_effect=replace_final):
            with self.assertRaisesRegex(ArchiveError, "metadata identity"):
                optimizer.optimize_image(source, output)
        self.assertFalse(output.exists())

    def test_derived_docker_output_must_preserve_expected_runtime_config(self):
        source = make_image(self.root / "derived", [[("file", tarfile.REGTYPE, b"old")],
                                                     [("file", tarfile.REGTYPE, b"new")]])
        original = ImageArchiveWriter.write_new

        def replace_user(writer, path, config, layers, tag, *args, **kwargs):
            config = dict(config, config={"User": "0:0"})
            return original(writer, path, config, layers, tag, *args, **kwargs)

        output = self.root / "optimized.tar"
        with patch.object(ImageArchiveWriter, "write_new", replace_user):
            with self.assertRaisesRegex(ArchiveError, "metadata identity"):
                optimizer.optimize_image(source, output, LAYER_TAG)
        self.assertFalse(output.exists())

    def test_same_docker_tag_can_be_selected_by_platform_in_web_and_cli(self):
        amd64, _, _ = fixture(self.root, arch="amd64")
        arm64, _, expected = fixture(self.root, arch="arm64")
        left, right = members(amd64), members(arm64)
        rows = json.loads(left["manifest.json"]) + json.loads(right["manifest.json"])
        left.update(right)
        left["manifest.json"] = json.dumps(rows).encode()
        source = self.root / "two-platforms.tar"
        write_members(source, left)
        with self.assertRaises(BaseImageNotFound):
            optimizer.analyze_image(source, TAG)
        self.assertEqual(optimizer.analyze_image(source, TAG, platform="linux/arm64")["architecture"], "arm64")
        output = self.root / "web.tar"
        plan = {"action": "optimize", "tag": TAG, "platform": "linux/arm64", "format": "docker", "output": str(output)}
        with contextlib.redirect_stdout(io.StringIO()):
            worker._deliver_archive(plan, {}, None, source, source.stat().st_size, self.root)
        selected = optimizer.load_image(output, self.root / "web-read", with_image=True)[2]
        self.assertEqual(selected.config_raw, expected)
        cli = self.root / "cli.tar"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(optimizer.main(["optimize", "--archive", str(source), "--output", str(cli),
                                             "--platform", "linux/arm64"]), 0)
        self.assertEqual(optimizer.load_image(cli, self.root / "cli-read", with_image=True)[2].config_raw, expected)

    def test_cas_workspace_cleanup_error_rolls_back_only_optimized_output(self):
        source, _, _ = fixture(self.root)
        root = self.root / "cas"
        store = CASStore(root)
        store.import_docker(source, TAG, "linux/amd64", "local")
        manifest = store.resolve(TAG, "linux/amd64", "local")["manifest"]
        output = self.root / "cas-result.tar"
        factory = tempfile.TemporaryDirectory

        class CleanupFailure:
            def __init__(self, *args, **kwargs):
                self.inner = factory(*args, **kwargs)
                self.fail = kwargs.get("prefix") == "pyimagebuilder-delivery-cas-"
            def __enter__(self):
                return self.inner.__enter__()
            def __exit__(self, *args):
                self.inner.__exit__(*args)
                if self.fail:
                    raise OSError("CAS workspace cleanup failed")

        plan = {"action": "optimize", "format": "cas", "tag": TAG, "platform": "linux/amd64",
                "source": "local", "manifest": manifest, "store": str(root), "output": str(output)}
        with patch.object(worker.tempfile, "TemporaryDirectory", CleanupFailure), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(OSError, "CAS workspace cleanup"):
                worker._deliver_cas(plan, {}, root)
        self.assertFalse(output.exists())
        self.assertEqual(store.resolve(TAG, "linux/amd64", "local")["manifest"], manifest)


        ref_lock = CASStore.ref_lock
        depth = 0

        @contextlib.contextmanager
        def fail_outer_release(instance, *args, **kwargs):
            nonlocal depth
            depth += 1
            try:
                with ref_lock(instance, *args, **kwargs):
                    yield
                if depth == 1:
                    raise OSError("CAS lock release failed")
            finally:
                depth -= 1

        for action in ("optimize", "save"):
            with self.subTest(action=action), patch.object(CASStore, "ref_lock", fail_outer_release), \
                    contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(OSError, "CAS lock release"):
                    worker._deliver_cas(dict(plan, action=action), {}, root)
            self.assertFalse(output.exists())
            self.assertEqual(store.resolve(TAG, "linux/amd64", "local")["manifest"], manifest)

    def test_cli_and_web_broken_result_pipe_roll_back_completed_optimization(self):
        source, _, _ = fixture(self.root)

        class BrokenOutput(io.StringIO):
            def write(self, _):
                raise BrokenPipeError("result pipe closed")

        cli = self.root / "cli.tar"
        with contextlib.redirect_stdout(BrokenOutput()), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                optimizer.main(["optimize", "--archive", str(source), "--output", str(cli)])
        self.assertFalse(cli.exists())
        web = self.root / "web.tar"
        with patch.object(sys, "argv", ["worker", '{"action":"optimize"}']), \
                patch.object(worker, "execute", side_effect=lambda _: optimizer.optimize_image(source, web)), \
                contextlib.redirect_stdout(BrokenOutput()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(worker.main(), 1)
        self.assertFalse(web.exists())

    def test_oci_wrong_platform_is_rejected_before_publication(self):
        source, _, _ = fixture(self.root, "oci", arch="arm64")
        output = self.root / "optimized.tar"
        with self.assertRaisesRegex(ArchiveError, "platform does not match"):
            optimizer.optimize_image(source, output, platform="linux/amd64")
        self.assertFalse(output.exists())

    def test_retag_verifies_staged_output_without_replacing_competing_file(self):
        source, _, _ = fixture(self.root)
        output = self.root / "tagged.tar"
        read = image_cli._read

        def competing_writer(path, tag, platform):
            self.assertNotEqual(path, output)
            self.assertFalse(output.exists())
            result = read(path, tag, platform)
            output.write_bytes(b"other writer")
            return result

        with patch.object(image_cli, "_read", side_effect=competing_writer):
            with self.assertRaises(FileExistsError):
                image_cli.tag_archive(source, "example/retag:2", output)
        self.assertEqual(output.read_bytes(), b"other writer")

    def test_retag_rejects_valid_output_with_different_config(self):
        source, _, _ = fixture(self.root)
        other, _, _ = fixture(self.root, arch="arm64")
        replacement = self.root / "replacement.tar"
        output = self.root / "tagged.tar"
        tag = "example/retag:2"
        image_cli.tag_archive(other, tag, replacement)

        with patch.object(image_cli, "_write_retagged",
                          side_effect=lambda _, staged, *args: shutil.copyfile(replacement, staged)):
            with self.assertRaisesRegex(ArchiveError, "original config bytes"):
                image_cli.tag_archive(source, tag, output)
        self.assertFalse(output.exists())

    def test_retag_updates_manifest_with_explicit_pax_size(self):
        source, _, raw_config = fixture(self.root)
        legacy = self.root / "legacy.tar"
        with tarfile.open(legacy, "w", format=tarfile.PAX_FORMAT) as archive:
            for name, raw in members(source).items():
                member = tarfile.TarInfo(name)
                member.size = len(raw)
                if name in ("manifest.json", "repositories"):
                    member.pax_headers["size"] = str(member.size)
                archive.addfile(member, io.BytesIO(raw))
        output = self.root / "tagged.tar"
        tag = "example/a-much-longer-retag:2"
        image_cli.tag_archive(legacy, tag, output)
        result = members(output)
        self.assertEqual(json.loads(result["manifest.json"])[0]["RepoTags"], [tag])
        self.assertIn("example/a-much-longer-retag", json.loads(result["repositories"]))
        self.assertEqual(payloads(output)[0], raw_config)

    def test_web_broken_result_pipe_reclaims_compression_archive_and_report(self):
        source, _, _ = fixture(self.root)
        output = self.root / "compressed.tar.gz"
        report = self.root / "report.json"
        plan = {"action": "compress", "format": "docker", "archive": str(source),
                "tag": TAG, "platform": "linux/amd64", "stamp": worker.file_stamp(source.stat()),
                "output": str(output), "report": str(report), "compress_level": 1, "upload_limit": 1000000}

        def fail_summary(message, **kwargs):
            if message.startswith("{\n"):
                self.assertTrue(output.is_file())
                self.assertTrue(report.is_file())
                raise BrokenPipeError("result pipe closed")

        with patch.object(sys, "argv", ["worker", json.dumps(plan)]), \
                patch.object(worker, "print", side_effect=fail_summary, create=True), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(worker.main(), 1)
        self.assertFalse(output.exists())
        self.assertFalse(report.exists())
        self.assertTrue(source.is_file())

    def test_outer_rollback_preserves_replacement_and_unobserved_file(self):
        output = self.root / "output.tar"
        replacement = self.root / "replacement.tar"
        existing = self.root / "existing.tar"
        owned = self.root / "owned.tar"
        replacement.write_bytes(b"replacement")
        existing.write_bytes(b"existing")
        with self.assertRaisesRegex(OSError, "outer rollback"):
            with publication_transaction() as outer:
                initial = self.root / "initial.tar"
                initial.write_bytes(b"initial")
                with publication_transaction() as inner:
                    for destination in (output, owned):
                        publish_new_file(initial, destination)
                        inner.append(destination)
                replacement.replace(output)
                outer.track_observed(output)
                outer.track_observed(existing)
                outer.track_observed(owned)
                raise OSError("outer rollback")
        self.assertEqual(output.read_bytes(), b"replacement")
        self.assertEqual(existing.read_bytes(), b"existing")
        self.assertFalse(owned.exists())
