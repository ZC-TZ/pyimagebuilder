"""防止大镜像索引逐文件扫描全树，同时核对快速删除路径的层语义。"""

import io
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rootfs import RootFSIndex


class CountedEntries(dict):
    """统计全树遍历的工作量，避免用易受机器负载影响的耗时断言。"""
    visited = 0

    def __iter__(self):
        self.visited += len(self)
        return super().__iter__()


class RootFSIndexScalingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.index = RootFSIndex()

    def apply(self, name, entries):
        path = self.root / (name + ".tar")
        with tarfile.open(str(path), "w") as archive:
            for name, kind, value in entries:
                member = tarfile.TarInfo(name)
                member.mode = 0o755 if kind == "dir" else 0o644
                if kind == "dir":
                    member.type = tarfile.DIRTYPE
                elif kind in ("hardlink", "symlink"):
                    member.type = tarfile.LNKTYPE if kind == "hardlink" else tarfile.SYMTYPE
                    member.linkname = value
                elif kind == "fifo":
                    member.type = tarfile.FIFOTYPE
                else:
                    member.size = len(value)
                archive.addfile(member, io.BytesIO(value) if kind == "file" else None)
        self.index.apply_layer(path)

    def test_many_small_files_and_replacements_do_not_walk_tree_per_file(self):
        self.index.entries = CountedEntries()
        entries = [("app/group-{}/file-{}".format(i % 20, i), "file", b"a")
                   for i in range(500)]
        self.apply("first", entries)
        self.apply("overwrite", entries)
        # 给线性遍历留余量；旧实现两次应用会枚举数十万个无关路径。
        self.assertLessEqual(self.index.entries.visited, len(entries) * 10)
        self.assertEqual(len(self.index.entries), 521)
        self.assertEqual(self.index.read_file("/app/group-1/file-1"), b"a")

    def test_absent_whiteouts_and_leaf_whiteouts_do_not_scan_tree(self):
        self.apply("base", [("app/file-{}".format(i), "file", b"a") for i in range(200)])
        self.index.entries = CountedEntries(self.index.entries)
        self.apply("whiteouts", [("app/.wh.file-{}".format(i), "file", b"") for i in range(400)])
        self.assertLessEqual(self.index.entries.visited, 4000)
        self.assertEqual(set(self.index.entries), {"app"})

    def test_leaf_replacement_preserves_previous_hardlink_inode(self):
        self.apply("base", [("app/data", "file", b"old"),
                            ("app/alias", "hardlink", "app/data")])
        original = self.index.entries["app/alias"].inode
        self.apply("replace", [("app/data", "file", b"new")])
        self.assertEqual(self.index.read_file("app/data"), b"new")
        self.assertEqual(self.index.read_file("app/alias"), b"old")
        self.assertIs(self.index.entries["app/alias"].inode, original)
        self.assertIsNot(self.index.entries["app/data"].inode, original)

    def test_replacing_directory_removes_only_its_descendants(self):
        self.apply("base", [("app/tree/nested/file", "file", b"old"),
                            ("app/tree-other/file", "file", b"keep")])
        self.apply("replace", [("app/tree", "file", b"new")])
        self.assertEqual(set(self.index.entries),
                         {"app", "app/tree", "app/tree-other", "app/tree-other/file"})
        self.assertEqual(self.index.read_file("app/tree"), b"new")
        self.assertEqual(self.index.read_file("app/tree-other/file"), b"keep")

    def test_opaque_implicit_directory_keeps_same_layer_files(self):
        self.apply("base", [("app/tree/old", "file", b"old"),
                            ("app/other", "file", b"keep")])
        parent = self.index.entries["app/tree"]
        self.apply("opaque", [("app/tree/new", "file", b"new"),
                              ("app/tree/.wh..wh..opq", "file", b"")])
        self.assertIs(self.index.entries["app/tree"], parent)
        self.assertEqual(set(self.index.entries),
                         {"app", "app/tree", "app/tree/new", "app/other"})
        self.assertEqual(self.index.read_file("app/tree/new"), b"new")

    def test_root_opaque_clears_tree_before_same_layer_entries(self):
        self.apply("base", [("old/child", "file", b"old")])
        self.apply("opaque", [("new/child", "file", b"new"),
                              (".wh..wh..opq", "file", b"")])
        self.assertEqual(set(self.index.entries), {"new", "new/child"})

    def test_leaf_types_can_be_replaced_with_implicit_directory(self):
        for kind, value in (("file", b"old"), ("symlink", "/elsewhere"), ("fifo", None)):
            with self.subTest(kind=kind):
                self.index = RootFSIndex()
                self.apply("base", [("app", kind, value)])
                self.apply("replace", [(".wh.app", "file", b""),
                                       ("app/child", "file", b"new")])
                self.assertEqual(self.index.kind("/app"), "dir")
                self.assertEqual(self.index.read_file("app/child"), b"new")


if __name__ == "__main__":
    unittest.main()
