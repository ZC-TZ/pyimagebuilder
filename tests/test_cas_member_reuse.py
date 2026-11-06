"""校验 CAS 建立索引只解析一次层头，且摘要和文件系统语义门禁不被绕过。"""

import io
import json
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cas_store
from builder import build
from cas_store import CASStore
from errors import ArchiveError
from image_reader import read_layer_members, verify_layer_tar
from image_writer import ImageArchiveWriter
from progress import make_reporter
from rootfs import RootFSIndex
from test_phase1 import make_base
from test_ingestion_validation import transport, truncated_layer


class CASMemberReuseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = CASStore(self.root / "store")

    def import_layer(self, raw):
        _, config_raw, manifest_raw, blobs = transport(raw, compressed=True)
        manifest = json.loads(manifest_raw)
        config_path = self.root / "config.json"
        config_path.write_bytes(config_raw)
        blob = self.root / "layer.gz"
        blob.write_bytes(blobs[manifest["layers"][0]["digest"]])
        return self.store.import_registry(manifest_raw, config_path, [blob],
                                          "example/base:1", "linux/amd64", replace=True)

    def mapping(self, source="local"):
        return {"type": "cas", "store": str(self.store.root.parent), "reference": "example/base:1",
                "platform": "linux/amd64", "source": source}

    def test_cas_build_reuses_parsed_members_and_matches_archive_build_bytes(self):
        source = make_base(self.root)
        self.store.import_docker(source, "example/base:1", "linux/amd64")
        context = self.root / "context"
        context.mkdir()
        recipe = context / "Dockerfile"
        recipe.write_text("FROM example/base:1\nENV MEMBER_REUSE=1\n", encoding="utf-8")
        expected, actual = self.root / "archive.tar", self.root / "cas.tar"
        build(recipe, context, source, None, "example/result:1", expected)
        with patch.object(RootFSIndex, "apply_layer", side_effect=AssertionError("layer reparsed")), \
                patch.object(cas_store, "read_layer_members", wraps=read_layer_members) as reading:
            build(recipe, context, self.mapping(), None, "example/result:1", actual)
        self.assertEqual(reading.call_count, 2)
        self.assertEqual(actual.read_bytes(), expected.read_bytes())

    def test_consumer_sees_manifest_order_and_whiteouts(self):
        source = make_base(self.root)
        self.store.import_docker(source, "example/base:1", "linux/amd64")
        rootfs, indices = RootFSIndex(), []

        def consume(path, members, index, total, diff_id):
            indices.append((index, total, diff_id))
            rootfs.apply_members(path, members)

        image = self.store.open_base("example/base:1", layer_consumer=consume)
        self.assertEqual([item[:2] for item in indices], [(1, 2), (2, 2)])
        self.assertEqual([item[2] for item in indices], image.config["rootfs"]["diff_ids"])
        self.assertIsNone(rootfs.kind("/etc/old.conf"))
        self.assertEqual(rootfs.read_file("/etc/new.conf"), b"new")

    def test_truncated_layer_never_reaches_consumer(self):
        self.import_layer(truncated_layer())
        consumer = Mock()
        with self.assertRaises(ArchiveError):
            self.store.open_base("example/base:1", source="artifactory", layer_consumer=consumer)
        consumer.assert_not_called()

    def test_bad_terminal_header_never_returns_partial_member_list(self):
        member = tarfile.TarInfo("directory")
        member.type = tarfile.DIRTYPE
        raw = member.tobuf() + b"invalid" + b"\0" * (512 - 7)
        with self.assertRaises(ArchiveError):
            read_layer_members(io.BytesIO(raw), "bad terminal")
        with self.assertRaises(ArchiveError):
            verify_layer_tar(io.BytesIO(raw), "bad terminal")

    def test_checksum_checks_still_precede_consumer_for_both_blob_forms(self):
        for representation in ("digest", "diff_id"):
            with self.subTest(representation=representation):
                # 每次使用独立引用，避免上一轮损坏影响本轮测试。
                root = self.root / representation
                root.mkdir()
                make_base(root)
                store = self.store = CASStore(root / "store")
                self.import_layer((root / "first.tar").read_bytes())
                store.open_base("example/base:1", source="artifactory")
                value = store.inspect_ref("example/base:1", source="artifactory")
                path = store._blob(value["layers"][0][representation])
                raw = path.read_bytes()
                path.write_bytes(raw[:-1] + bytes([raw[-1] ^ 1]))
                consumer = Mock()
                with self.assertRaises(ArchiveError):
                    store.open_base("example/base:1", source="artifactory", layer_consumer=consumer)
                consumer.assert_not_called()

    def test_reused_headers_still_reject_duplicate_and_escaping_paths(self):
        for names in (("../escape",), ("duplicate", "duplicate"), ("/root", "root")):
            with self.subTest(names=names):
                stream = io.BytesIO()
                with tarfile.open(fileobj=stream, mode="w") as archive:
                    for name in names:
                        member = tarfile.TarInfo(name)
                        member.size = 1
                        archive.addfile(member, io.BytesIO(b"x"))
                raw = stream.getvalue()
                self.import_layer(raw)
                rootfs = RootFSIndex()
                with self.assertRaises(ArchiveError):
                    self.store.open_base("example/base:1", source="artifactory",
                        layer_consumer=lambda path, members, *_: rootfs.apply_members(path, members))

    def test_root_metadata_and_hardlinks_are_preserved(self):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w", format=tarfile.PAX_FORMAT) as archive:
            root = tarfile.TarInfo(".")
            root.type, root.mode, root.uid, root.gid = tarfile.DIRTYPE, 0o40750, 123, 456
            root.pax_headers = {"SCHILY.xattr.user.test": "root"}
            archive.addfile(root)
            member = tarfile.TarInfo("file")
            member.size, member.mode, member.uid, member.gid = 4, 0o640, 123, 456
            archive.addfile(member, io.BytesIO(b"data"))
            link = tarfile.TarInfo("alias")
            link.type, link.linkname = tarfile.LNKTYPE, "file"
            link.mode, link.uid, link.gid = 0o600, 321, 654
            archive.addfile(link)
        self.import_layer(stream.getvalue())
        rootfs = RootFSIndex()
        self.store.open_base("example/base:1", source="artifactory",
            layer_consumer=lambda path, members, *_: rootfs.apply_members(path, members))
        self.assertEqual((rootfs.root_metadata.mode, rootfs.root_metadata.uid, rootfs.root_metadata.gid),
                         (0o750, 123, 456))
        self.assertEqual(rootfs.root_metadata.pax_headers["SCHILY.xattr.user.test"], "root")
        self.assertEqual(rootfs.read_file("/alias"), b"data")
        self.assertIs(rootfs.entries["alias"].inode, rootfs.entries["file"].inode)
        self.assertEqual(rootfs.entries["file"].metadata.mode, 0o600)

    def test_zero_layer_build_has_both_completion_groups(self):
        source = self.root / "empty.tar"
        ImageArchiveWriter().write_new(source, {"os": "linux", "architecture": "amd64", "config": {},
            "rootfs": {"type": "layers", "diff_ids": []}}, [], "example/base:1")
        self.store.import_docker(source, "example/base:1", "linux/amd64")
        recipe = self.root / "Dockerfile"
        recipe.write_text("FROM example/base:1\n", encoding="utf-8")
        stream = io.StringIO()
        reporter = make_reporter(progress="json", stream=stream)
        self.addCleanup(reporter.close)
        build(recipe, self.root, self.mapping(), None, "example/result:1", self.root / "result.tar",
              reporter=reporter)
        events = [json.loads(line) for line in stream.getvalue().splitlines()]
        self.assertEqual([item["details"]["operation"] for item in events if item["type"] == "layers_complete"],
                         ["verify", "index"])


if __name__ == "__main__":
    unittest.main()
