"""验证 Python 3.7 兼容函数保留对应标准库 API 的行为。"""

import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from compat import is_relative_to, remove_suffix, unlink_missing


class CompatibilityTests(unittest.TestCase):
    def test_suffix_is_exact_and_empty_suffix_is_noop(self):
        self.assertEqual(remove_suffix("layer.tar", ".tar"), "layer")
        self.assertEqual(remove_suffix("layer.tar", ".json"), "layer.tar")
        self.assertEqual(remove_suffix("layer.tar", ""), "layer.tar")
        self.assertEqual(remove_suffix("a.tar.tar", ".tar"), "a.tar")

    def test_relative_path_boundary(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertTrue(is_relative_to(root / "dir" / "file", root))
            self.assertTrue(is_relative_to(root, root))
            self.assertFalse(is_relative_to(root.parent / "other", root))

    def test_unlink_ignores_only_missing_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "file"
            unlink_missing(target)
            target.write_text("x")
            unlink_missing(target)
            self.assertFalse(target.exists())
            with self.assertRaises(OSError):
                unlink_missing(root)


if __name__ == "__main__":
    unittest.main()
