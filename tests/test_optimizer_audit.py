"""回归优化器的源字节绑定、平台选择和失败发布边界。"""

import contextlib
import io
import json
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import optimizer
import web_delivery_worker as worker
from cas_store import CASStore
from errors import ArchiveError, BaseImageNotFound
from image_reader import DigestReader
from image_writer import ImageArchiveWriter
from oci_writer import OCIImageWriter
from test_image_identity_contract import TAG, add_bytes, fixture
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

        with patch.object(CASStore, "ref_lock", fail_outer_release), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(OSError, "CAS lock release"):
                worker._deliver_cas(plan, {}, root)
        self.assertFalse(output.exists())

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
