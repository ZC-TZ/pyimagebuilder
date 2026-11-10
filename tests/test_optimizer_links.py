"""验证整层优化对硬链接、设备/FIFO、有效权限和共享 inode 的等价判断。"""

import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import optimizer
from errors import ArchiveError
from image_reader import BaseImage, sha256_file
from image_writer import ImageArchiveWriter
from oci_writer import OCIImageWriter
from layer_reader import LayerReader
from permissions import normalized_metadata
from test_slim import fixture
from web_delivery_worker import _optimize

TAG = "example/optimize-links:1"


def layer(path, rows):
    with tarfile.open(path, "w", format=tarfile.PAX_FORMAT) as archive:
        for row in rows:
            name, kind, value = row[:3]
            member = tarfile.TarInfo(name);member.type = kind;member.mtime = 0
            member.mode = 0o755 if kind == tarfile.DIRTYPE else 0o644
            if kind == tarfile.REGTYPE:
                member.size = len(value)
            elif kind in (tarfile.LNKTYPE, tarfile.SYMTYPE):
                member.linkname = value
            for key, item in (row[3] if len(row) > 3 else {}).items():
                setattr(member, key, item)
            archive.addfile(member, io.BytesIO(value) if kind == tarfile.REGTYPE else None)
    return path


def image(root, rows, kind="docker"):
    root.mkdir(parents=True, exist_ok=True)
    layers = [layer(root / ("layer%d.tar" % number), entries) for number, entries in enumerate(rows)]
    config = {"architecture": "amd64", "os": "linux", "config": {"User": "1000:1000"},
              "rootfs": {"type": "layers", "diff_ids": ["sha256:" + sha256_file(p) for p in layers]},
              "history": [{"created_by": "fixture%d" % i} for i in range(len(layers))],
              "x-fixture": {"preserve": True}}
    raw = (json.dumps(config, indent=4) + "\n").encode()
    target = root / "source.tar"
    writer = ImageArchiveWriter() if kind == "docker" else OCIImageWriter()
    writer.write_image(target, BaseImage(config, layers, [TAG], {}, config_raw=raw), TAG)
    return target


def tree(layers):
    index = optimizer._index(layers)
    rows, aliases = {}, {}
    with LayerReader() as reader:
        for name, entry in index.entries.items():
            if not entry.layer:
                rows[name] = ("implicit-directory",)
                continue
            _, raw = reader.member(entry.layer, entry.member)
            metadata = entry.metadata or normalized_metadata(raw)
            regular = entry.kind in ("file", "hardlink")
            rows[name] = ("file" if regular else raw.type,
                          index.read_file("/" + name, reader=reader) if regular else None,
                          metadata.mode, metadata.uid, metadata.gid, metadata.mtime,
                          metadata.devmajor, metadata.devminor,
                          entry.linkname if entry.kind == "symlink" else "",
                          metadata.pax_headers)
            if regular:
                aliases.setdefault(id(entry.inode), set()).add(name)
    return rows, {frozenset(names) for names in aliases.values()}, index.root_metadata


class OptimizerLinksTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="optimizer-links-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def check(self, source, output):
        before = optimizer.load_image(source, self.root / ("read-before-" + output.stem), TAG, with_image=True)[2]
        after = optimizer.load_image(output, self.root / ("read-after-" + output.stem), TAG, with_image=True)[2]
        left, right = tree(before.layers), tree(after.layers)
        self.assertEqual(left[:2], right[:2])
        self.assertEqual(before.config["config"], after.config["config"])
        self.assertEqual(before.config["x-fixture"], after.config["x-fixture"])
        return before, after

    def test_existing_complex_image_can_be_kept_unchanged_in_both_formats(self):
        for kind in ("docker", "oci"):
            root = self.root / kind;root.mkdir()
            source, _ = fixture(root, kind)
            output = root / "kept.tar"
            result = optimizer.optimize_image(source, output)
            self.assertEqual(result["removed_layer_numbers"], [])
            before = optimizer.load_image(source, root / "before", with_image=True)[2]
            after = optimizer.load_image(output, root / "after", with_image=True)[2]
            self.assertEqual(before.config_raw, after.config_raw)
            self.assertEqual(before.config_digest, after.config_digest)
            self.assertEqual(tree(before.layers)[:2], tree(after.layers)[:2])

    def test_redundant_layer_with_hardlinks_and_special_nodes_can_be_removed(self):
        for kind in ("docker", "oci"):
            def rows(value):
                return [("a", tarfile.REGTYPE, value), ("alias", tarfile.LNKTYPE, "a"),
                        ("dev/pipe", tarfile.FIFOTYPE, ""),
                        ("dev/char", tarfile.CHRTYPE, "", {"devmajor": 1, "devminor": 3}),
                        ("dev/block", tarfile.BLKTYPE, "", {"devmajor": 8, "devminor": 0})]
            root = self.root / kind
            source = image(root, [rows(b"old"), rows(b"new")], kind)
            output = root / "optimized.tar"
            report = optimizer.optimize_image(source, output, TAG)
            self.assertEqual(report["removed_layer_numbers"], [1])
            before, after = self.check(source, output)
            self.assertEqual(len(after.layers), 1)
            self.assertNotEqual(before.config_digest, after.config_digest)

    def test_old_inode_payload_survives_later_target_replacement_or_deletion(self):
        first = [("a", tarfile.REGTYPE, b"old"), ("alias", tarfile.LNKTYPE, "a")]
        for name, second in (("replace", [("a", tarfile.REGTYPE, b"new")]),
                             ("delete", [(".wh.a", tarfile.REGTYPE, b"")])):
            root = self.root / name;source = image(root, [first, second])
            output = root / "optimized.tar"
            self.assertEqual(optimizer.optimize_image(source, output, TAG)["removed_layer_numbers"], [])
            self.check(source, output)

    def test_identical_bytes_do_not_hide_changed_inode_sharing(self):
        cases = (
            ([("a", tarfile.REGTYPE, b"same"), ("b", tarfile.REGTYPE, b"same")],
             [("b", tarfile.LNKTYPE, "a")]),
            ([("a", tarfile.REGTYPE, b"same"), ("b", tarfile.LNKTYPE, "a")],
             [("b", tarfile.REGTYPE, b"same")]),
        )
        for number, (first, second) in enumerate(cases):
            root = self.root / str(number);source = image(root, [first, second])
            output = root / "optimized.tar"
            self.assertEqual(optimizer.optimize_image(source, output, TAG)["removed_layer_numbers"], [])
            self.check(source, output)

    def test_link_header_permission_owner_and_xattr_changes_are_not_discarded(self):
        source = image(self.root / "attributes", [
            [("a", tarfile.REGTYPE, b"data"), ("b", tarfile.LNKTYPE, "a")],
            [("b", tarfile.LNKTYPE, "a", {"mode": 0o600, "uid": 101, "gid": 202,
                                        "pax_headers": {"SCHILY.xattr.user.test": "kept"}})]])
        output = source.parent / "optimized.tar"
        self.assertEqual(optimizer.optimize_image(source, output, TAG)["removed_layer_numbers"], [])
        _, after = self.check(source, output)
        index = optimizer._index(after.layers)
        self.assertIs(index.entries["a"].inode, index.entries["b"].inode)
        self.assertEqual(index.entries["a"].metadata.uid, 101)
        self.assertEqual(index.entries["a"].metadata.mode, 0o600)

    def test_device_type_number_fifo_permissions_and_xattrs_remain_significant(self):
        cases = (
            (tarfile.CHRTYPE, {"devmajor": 1, "devminor": 3}, tarfile.CHRTYPE, {"devmajor": 1, "devminor": 5}),
            (tarfile.CHRTYPE, {"devmajor": 1, "devminor": 3}, tarfile.BLKTYPE, {"devmajor": 1, "devminor": 3}),
            (tarfile.FIFOTYPE, {"mode": 0o644}, tarfile.FIFOTYPE,
             {"mode": 0o620, "uid": 10, "pax_headers": {"SCHILY.xattr.user.test": "fifo"}}),
        )
        for number, (first_kind, first_attrs, second_kind, second_attrs) in enumerate(cases):
            root = self.root / str(number)
            source = image(root, [[("anchor", tarfile.REGTYPE, b"keep"),
                                   ("dev/node", first_kind, "", first_attrs)],
                                  [("dev/node", second_kind, "", second_attrs)]])
            output = root / "optimized.tar"
            self.assertEqual(optimizer.optimize_image(source, output, TAG)["removed_layer_numbers"], [])
            self.check(source, output)

    def test_canonical_hardlink_order_does_not_prevent_proven_redundancy(self):
        source = image(self.root / "canonical", [
            [("a", tarfile.REGTYPE, b"data"), ("b", tarfile.LNKTYPE, "a")],
            [("b", tarfile.REGTYPE, b"data"), ("a", tarfile.LNKTYPE, "b")]])
        output = source.parent / "optimized.tar"
        self.assertEqual(optimizer.optimize_image(source, output, TAG)["removed_layer_numbers"], [1])
        self.check(source, output)

    def test_signature_reads_each_backing_layer_once_and_reuses_hashes(self):
        root = self.root / "scan";root.mkdir()
        first = layer(root / "first.tar", [("a%d" % i, tarfile.REGTYPE, b"data") for i in range(100)])
        second = layer(root / "second.tar", [("b%d" % i, tarfile.REGTYPE, b"data") for i in range(100)])
        index, memo = optimizer._index([first, second]), {}
        original = tarfile.open
        with patch.object(optimizer.tarfile, "open", wraps=original) as opened:
            before = optimizer._signature(index, memo)
            self.assertEqual(opened.call_count, 2)
            self.assertEqual(optimizer._signature(index, memo), before)
            self.assertEqual(opened.call_count, 2)

    def test_unknown_entry_type_is_not_silently_declared_equivalent(self):
        source = image(self.root / "unknown", [[("unknown", b"Z", "")]])
        output = source.parent / "optimized.tar"
        with self.assertRaisesRegex(ArchiveError, "Unsupported rootfs entry"):
            optimizer.optimize_image(source, output, TAG)
        self.assertFalse(output.exists())

    def test_web_final_progress_failure_rolls_back_published_optimization(self):
        source = image(self.root / "progress", [[("a", tarfile.REGTYPE, b"data"), ("b", tarfile.LNKTYPE, "a")]])
        output = source.parent / "optimized.tar"
        reporter = Mock();reporter.success.side_effect = BrokenPipeError("progress closed")
        with patch("progress.make_reporter", return_value=reporter):
            with self.assertRaises(BrokenPipeError):
                _optimize({"tag": TAG, "platform": "linux/amd64", "output": str(output)}, source)
        self.assertFalse(output.exists())

    def test_cli_entry_optimizes_special_nodes_without_runtime_or_privileges(self):
        source = image(self.root / "cli", [[("a", tarfile.REGTYPE, b"data"),
                                            ("b", tarfile.LNKTYPE, "a"), ("pipe", tarfile.FIFOTYPE, "")]])
        output = source.parent / "optimized.tar"
        project = Path(__file__).resolve().parents[1]
        run = subprocess.run([sys.executable, str(project/"main.py"), "optimize", "--archive", str(source),
                              "--output", str(output)], cwd=str(project), stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, env=dict(os.environ, PYTHONIOENCODING="utf-8"), timeout=30)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout)["removed_layer_numbers"], [])
        self.check(source, output)
