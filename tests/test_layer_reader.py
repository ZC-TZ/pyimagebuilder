"""核对成员复用的开销、硬链接内容、稀疏数据和句柄生命周期。"""

import hashlib
import io
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dockerfile_parser import parse
from errors import ArchiveError
from layer import LayerBuilder
from layer_reader import LayerReader
from rootfs import RootFSIndex
from sbom import create_sbom


class LayerReaderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def layer(self, name, files):
        path = self.root / (name + ".tar")
        with tarfile.open(str(path), "w", format=tarfile.PAX_FORMAT) as archive:
            for name, value in files:
                member = tarfile.TarInfo(name)
                member.mode = 0o640
                member.uid, member.gid = 12, 34
                if isinstance(value, bytes):
                    member.size = len(value)
                    archive.addfile(member, io.BytesIO(value))
                else:
                    member.type, member.linkname = tarfile.LNKTYPE, value
                    archive.addfile(member)
        return path

    def index(self, *layers):
        result = RootFSIndex()
        for layer in layers:
            result.apply_layer(layer)
        return result

    def test_bulk_stage_copy_opens_source_once_and_keeps_bytes(self):
        files = [("pkg/file-{}".format(i), str(i).encode()) for i in range(80)]
        source = self.layer("source", files)
        index = self.index(source)
        transfer = parse("FROM scratch\nCOPY --from=source /pkg /out/\n")[1].value
        output = self.root / "copy.tar"
        original = tarfile.open
        with patch("layer_reader.tarfile.open", wraps=original) as opened:
            LayerBuilder(self.root, RootFSIndex()).transfer_from_rootfs(index, transfer, "/", output)
        reads = [call for call in opened.call_args_list if call[0][0] == str(source)
                 or call[0][0] == source]
        self.assertEqual(len(reads), 1)
        result = self.index(output)
        with LayerReader() as reader:
            for name, expected in files:
                self.assertEqual(result.read_file(name.replace("pkg/", "out/"), reader=reader), expected)

    def test_sbom_hashes_and_python_detection_share_one_layer_scan(self):
        files = [("pkg-{}.dist-info/METADATA".format(i),
                  "Name: pkg-{}\nVersion: 1.2.3\n".format(i).encode()) for i in range(50)]
        source = self.layer("packages", files)
        index = self.index(source)
        original = tarfile.open
        with patch("layer_reader.tarfile.open", wraps=original) as opened:
            result = create_sbom(index, {"rootfs": {"diff_ids": []}}, "example/app:1", 0)
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(len(result["packages"]), 51)
        checksums = {item["fileName"][2:]: item["checksums"][1]["checksumValue"]
                     for item in result["files"]}
        self.assertEqual(checksums, {name: hashlib.sha256(value).hexdigest() for name, value in files})

    def test_cached_member_keeps_old_hardlink_after_target_replacement(self):
        lower = self.layer("lower", [("pkg/data", b"old"), ("pkg/alias", "pkg/data")])
        upper = self.layer("upper", [("pkg/data", b"new")])
        index = self.index(lower, upper)
        with LayerReader() as reader:
            self.assertEqual(index.read_file("pkg/data", reader=reader), b"new")
            self.assertEqual(index.read_file("pkg/alias", reader=reader), b"old")
            with self.assertRaisesRegex(ArchiveError, "size limit"):
                index.read_file("pkg/alias", limit=2, reader=reader)

    def test_lru_eviction_reopens_layer_and_closes_handles(self):
        layers = [self.layer(str(i), [("data", bytes([i]))]) for i in range(4)]
        archives = []
        original = tarfile.open

        def track(*args, **kwargs):
            archive = original(*args, **kwargs)
            archives.append(archive)
            self.assertLessEqual(sum(not item.closed for item in archives), 2)
            return archive

        with patch("layer_reader.tarfile.open", side_effect=track):
            with LayerReader(limit=2) as reader:
                for path in layers + layers[:1]:
                    archive, member = reader.member(path, "data")
                    self.assertLessEqual(len(reader.opened), 2)
                    self.assertFalse(archive.closed)
                    with archive.extractfile(member) as stream:
                        self.assertEqual(len(stream.read()), 1)
        self.assertEqual(len(archives), 5)
        self.assertTrue(all(archive.closed for archive in archives))

    def test_reader_cache_does_not_outlive_operation(self):
        source = self.layer("replaced", [("data", b"old")])
        self.assertEqual(self.index(source).read_file("data"), b"old")
        source = self.layer("replaced", [("padding", b"x" * 1024), ("data", b"new")])
        self.assertEqual(self.index(source).read_file("data"), b"new")

    def test_sparse_member_reads_expanded_bytes(self):
        source = self.root / "sparse.tar"
        with tarfile.open(str(source), "w", format=tarfile.PAX_FORMAT) as archive:
            member = tarfile.TarInfo("sparse")
            member.size = 1
            member.pax_headers = {"GNU.sparse.map": "0,1", "GNU.sparse.size": "1024"}
            archive.addfile(member, io.BytesIO(b"a"))
        index = self.index(source)
        self.assertEqual(index.read_file("sparse"), b"a" + b"\0" * 1023)

    def test_parse_failure_closes_archive_and_preserves_error(self):
        archive = Mock()
        archive.getmembers.side_effect = ValueError("bad header")
        archive.close.side_effect = OSError("close failed")
        with patch("layer_reader.tarfile.open", return_value=archive):
            with LayerReader() as reader:
                with self.assertRaisesRegex(ValueError, "bad header"):
                    reader.member("invalid.tar", "data")
        archive.close.assert_called_once_with()

    def test_operation_failure_still_closes_all_layers(self):
        first, second = Mock(), Mock()
        first.getmembers.return_value = []
        second.getmembers.return_value = []
        first.close.side_effect = OSError("close failed")
        with patch("layer_reader.tarfile.open", side_effect=[first, second]):
            with self.assertRaisesRegex(RuntimeError, "copy failed"):
                with LayerReader() as reader:
                    for name in ("a.tar", "b.tar"):
                        with self.assertRaises(KeyError):
                            reader.member(name, "missing")
                    raise RuntimeError("copy failed")
        first.close.assert_called_once_with()
        second.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
