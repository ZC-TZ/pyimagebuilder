"""检查同层路径冲突的失败边界，以及运行配置对照是否遗漏字段。"""

import io
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

import conformance
from errors import ArchiveError
from image_reader import sha256_file
from image_writer import ImageArchiveWriter
from oci_writer import OCIImageWriter
from rootfs import RootFSIndex
from rootfs_archive import export_rootfs, flatten_image, import_rootfs
from rootfs_materializer import RootFSMaterializer


def layer_at(path, entries):
    """按指定顺序写成员，复现父路径出现在子路径之前或之后的情况。"""
    with tarfile.open(path, "w") as archive:
        for name, kind, value in entries:
            member = tarfile.TarInfo(name)
            member.type, member.mode = kind, 0o644
            if kind == tarfile.REGTYPE:
                member.size = len(value)
                archive.addfile(member, io.BytesIO(value))
            else:
                if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                    member.linkname = value
                archive.addfile(member)
    return path


def image_at(path, layers, runtime=None, format="docker"):
    config = {"os": "linux", "architecture": "amd64", "config": runtime or {},
              "rootfs": {"type": "layers", "diff_ids": ["sha256:" + sha256_file(p) for p in layers]},
              "history": [{"created_by": "fixture"} for _ in layers]}
    writer = ImageArchiveWriter() if format == "docker" else OCIImageWriter()
    writer.write_new(path, config, layers, "example/app:1")
    return path


class LayerValidationBoundaryTests(unittest.TestCase):
    def conflicting_entries(self, parent_kind=tarfile.REGTYPE, child_kind=tarfile.REGTYPE):
        parent_value = b"parent" if parent_kind == tarfile.REGTYPE else "data"
        child_value = b"child" if child_kind == tarfile.REGTYPE else "data"
        return [("data", tarfile.REGTYPE, b"target"),
                ("parent/child", child_kind, child_value), ("parent", parent_kind, parent_value)]

    def test_index_rejects_conflict_in_both_orders_before_changing_tree(self):
        for parent_kind in (tarfile.REGTYPE, tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE):
            for child_kind in (tarfile.REGTYPE, tarfile.LNKTYPE):
                for reverse in (False, True):
                    with self.subTest(parent=parent_kind, child=child_kind, reverse=reverse), tempfile.TemporaryDirectory() as temp:
                        root = Path(temp)
                        index = RootFSIndex()
                        index.apply_layer(layer_at(root / "old.tar", [("keep", tarfile.REGTYPE, b"keep")]))
                        before = dict(index.entries)
                        entries = self.conflicting_entries(parent_kind, child_kind)
                        if reverse:
                            entries = entries[:1] + list(reversed(entries[1:]))
                        bad = layer_at(root / "bad.tar", entries)
                        with self.assertRaisesRegex(ArchiveError, "non-directory"):
                            index.apply_layer(bad)
                        self.assertEqual(index.entries, before)

    def test_materializer_rejects_conflict_before_creating_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            layer = layer_at(root / "bad.tar", self.conflicting_entries(child_kind=tarfile.LNKTYPE))
            materializer = RootFSMaterializer(root / "rootfs")
            with patch.object(materializer, "_metadata"), self.assertRaisesRegex(ArchiveError, "non-directory"):
                materializer.apply(layer)
            self.assertEqual(list(materializer.root.iterdir()), [])

    def test_independent_reader_rejects_conflicts_in_both_orders(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                entries = self.conflicting_entries()
                if reverse:
                    entries = entries[:1] + list(reversed(entries[1:]))
                layer = layer_at(root / "bad.tar", entries)
                with self.assertRaisesRegex(ArchiveError, "non-directory"):
                    conformance._layer_tree([layer])

    def test_import_export_and_flatten_do_not_publish_conflicting_tree(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bad = layer_at(root / "bad.tar", self.conflicting_entries(child_kind=tarfile.LNKTYPE))
            image = image_at(root / "image.tar", [bad])
            for name, operation in (
                    ("import.tar", lambda p: import_rootfs(bad, "example/imported:1", p)),
                    ("export.tar", lambda p: export_rootfs(image, p)),
                    ("flat.tar", lambda p: flatten_image(image, p))):
                output = root / name
                with self.assertRaisesRegex(ArchiveError, "non-directory"):
                    operation(output)
                self.assertFalse(output.exists())
            self.assertEqual(list(root.glob("pyimagebuilder-*/")), [])

    def test_import_cli_returns_error_without_uncaught_traceback(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = layer_at(root / "bad.tar", self.conflicting_entries(child_kind=tarfile.LNKTYPE))
            output = root / "output.tar"
            process = subprocess.run([sys.executable, str(PROJECT / "main.py"), "import", str(source),
                                      "example/app:1", "-o", str(output), "--quiet"],
                                     cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(process.returncode, 1)
            self.assertNotIn(b"Traceback", process.stderr)
            self.assertIn(b"non-directory", process.stderr)
            self.assertFalse(output.exists())

    def test_directory_header_can_follow_its_child(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            layer = layer_at(root / "layer.tar", [("parent/child", tarfile.REGTYPE, b"data"),
                                                  ("parent", tarfile.DIRTYPE, "")])
            index = RootFSIndex()
            index.apply_layer(layer)
            self.assertEqual(index.read_file("/parent/child"), b"data")
            output = root / "image.tar"
            import_rootfs(layer, "example/app:1", output)
            self.assertIn("parent/child", conformance.snapshot(output, "example/app:1")["tree"])

    def test_cross_layer_directory_replacement_remains_supported(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            lower = layer_at(root / "lower.tar", [("parent/child", tarfile.REGTYPE, b"old")])
            upper = layer_at(root / "upper.tar", [("parent/.wh.child", tarfile.REGTYPE, b""),
                                                   ("parent", tarfile.REGTYPE, b"new")])
            index = RootFSIndex()
            for layer in (lower, upper):
                index.apply_layer(layer)
            self.assertEqual(index.kind("parent"), "file")
            self.assertIsNone(index.kind("parent/child"))
            self.assertEqual(index.read_file("/parent"), b"new")
            materializer = RootFSMaterializer(root / "rootfs")
            with patch.object(materializer, "_metadata"):
                for layer in (lower, upper):
                    materializer.apply(layer)
            self.assertEqual((materializer.root / "parent").read_bytes(), b"new")
            self.assertEqual(conformance._layer_tree([lower, upper])["parent"]["type"], "file")

    def test_runtime_comparison_detects_healthcheck_stop_signal_and_onbuild(self):
        cases = [("Healthcheck", {"Test": ["CMD", "check"], "Interval": 30000000000}, "healthcheck"),
                 ("StopSignal", "SIGQUIT", "stop_signal"),
                 ("OnBuild", ["COPY . /app"], "onbuild")]
        for format in ("docker", "oci"):
            for field, value, reported in cases:
                with self.subTest(format=format, field=field), tempfile.TemporaryDirectory() as temp:
                    root = Path(temp)
                    left = image_at(root / "left.tar", [], {}, format)
                    right = image_at(root / "right.tar", [], {field: value}, format)
                    differences = conformance.compare_snapshots(conformance.snapshot(left, "example/app:1"),
                                                               conformance.snapshot(right, "example/app:1"))
                    self.assertEqual({item["field"] for item in differences}, {"config." + reported})

    def test_optional_runtime_null_matches_absent_and_empty_onbuild(self):
        empty = {"runtime": conformance._runtime({"config": {}}), "tree": {}}
        null = {"runtime": conformance._runtime({"config": {
            "Healthcheck": None, "StopSignal": None, "OnBuild": None}}), "tree": {}}
        self.assertEqual(conformance.compare_snapshots(empty, null), [])
        null["runtime"] = conformance._runtime({"config": {"OnBuild": []}})
        self.assertEqual(conformance.compare_snapshots(empty, null), [])


if __name__ == "__main__":
    unittest.main()
