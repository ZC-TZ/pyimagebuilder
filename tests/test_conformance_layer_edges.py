"""防止独立验收遗漏前向硬链接内容或接受无法物化的跨层路径。"""

import hashlib
import io
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import conformance
from errors import ArchiveError
from rootfs import RootFSIndex


def layer(path, entries):
    """显式安排成员顺序，避免夹具总把被链接的文件放在前面。"""
    with tarfile.open(path, "w") as archive:
        for name, kind, value in entries:
            item = tarfile.TarInfo(name)
            item.type = kind
            if kind == tarfile.REGTYPE:
                item.size = len(value)
                archive.addfile(item, io.BytesIO(value))
            else:
                if kind in (tarfile.LNKTYPE, tarfile.SYMTYPE):
                    item.linkname = value
                archive.addfile(item)
    return path


class IndependentLayerEdges(unittest.TestCase):
    def test_non_directory_parent_in_lower_layer_is_rejected(self):
        for kind in (tarfile.REGTYPE, tarfile.SYMTYPE, tarfile.FIFOTYPE):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                lower = layer(root / "lower.tar", [("parent", kind, b"data" if kind == tarfile.REGTYPE else "data")])
                upper = layer(root / "upper.tar", [("parent/child", tarfile.REGTYPE, b"child")])
                index = RootFSIndex()
                index.apply_layer(lower)
                with self.assertRaisesRegex(ArchiveError, "non-directory"):
                    index.apply_layer(upper)
                with self.assertRaisesRegex(ArchiveError, "non-directory"):
                    conformance._layer_tree([lower, upper])

    def test_forward_chain_has_digest_for_same_or_lower_layer_target(self):
        for lower_target in (False, True):
            with self.subTest(lower=lower_target), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                content = b"payload"
                entries = [("alias", tarfile.LNKTYPE, "middle"), ("middle", tarfile.LNKTYPE, "data")]
                layers = []
                if lower_target:
                    layers.append(layer(root / "lower.tar", [("data", tarfile.REGTYPE, content)]))
                else:
                    entries.append(("data", tarfile.REGTYPE, content))
                layers.append(layer(root / "upper.tar", entries))
                tree = conformance._layer_tree(layers)
                for name in ("alias", "middle"):
                    self.assertEqual(tree[name]["sha256"], hashlib.sha256(content).hexdigest())
                    self.assertEqual(tree[name]["size"], len(content))

    def test_target_replacement_preserves_previous_alias_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lower = layer(root / "lower.tar", [("alias", tarfile.LNKTYPE, "data"),
                                               ("data", tarfile.REGTYPE, b"old")])
            upper = layer(root / "upper.tar", [("data", tarfile.REGTYPE, b"new")])
            tree = conformance._layer_tree([lower, upper])
            self.assertEqual(tree["alias"]["sha256"], hashlib.sha256(b"old").hexdigest())
            self.assertEqual(tree["data"]["sha256"], hashlib.sha256(b"new").hexdigest())

    def test_missing_cyclic_and_non_file_targets_are_rejected(self):
        cases = [
            [("alias", tarfile.LNKTYPE, "missing")],
            [("alias", tarfile.LNKTYPE, "middle"), ("middle", tarfile.LNKTYPE, "alias")],
            [("alias", tarfile.LNKTYPE, "dir"), ("dir", tarfile.DIRTYPE, "")],
        ]
        for entries in cases:
            with self.subTest(entries=entries), tempfile.TemporaryDirectory() as tmp:
                source = layer(Path(tmp) / "layer.tar", entries)
                with self.assertRaises(ArchiveError):
                    conformance._layer_tree([source])


if __name__ == "__main__":
    unittest.main()
