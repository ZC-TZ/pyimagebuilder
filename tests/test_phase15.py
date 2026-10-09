"""验证独立兼容性读取器及离线语义样例。"""

import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import conformance
from errors import BuildError


class PhaseFifteenTests(unittest.TestCase):
    def test_offline_suite_checks_seven_semantics(self):
        result = conformance.suite()
        self.assertTrue(result["passed"], result["cases"])
        self.assertFalse(result["docker_reference_executed"])
        self.assertEqual({item["case"] for item in result["cases"]},
                         {"copy_metadata", "add_multistage", "overwrite", "config_volume",
                          "whiteout", "links", "cache_invalidation"})

    def test_snapshot_comparison_detects_runtime_and_file_changes(self):
        expected = {"runtime": {"env": {"MODE": "prod"}, "cmd": ["start"]},
                    "tree": {"app/file": {"type": "file", "sha256": "a",
                                             "mode": 0o644, "uid": 0, "gid": 0}}}
        actual = copy.deepcopy(expected)
        actual["runtime"]["cmd"] = ["wrong"]
        actual["tree"]["app/file"]["sha256"] = "b"
        fields = {item["field"] for item in conformance.compare_snapshots(actual, expected)}
        self.assertEqual(fields, {"config.cmd", "rootfs/app/file"})

    def test_reference_mode_never_silently_skips_missing_docker(self):
        with patch.object(conformance.shutil, "which", return_value=None):
            with self.assertRaises(BuildError):
                conformance.suite(reference=True)

    def test_linux_mode_requires_linux(self):
        with patch.object(conformance.platform, "system", return_value="Windows"):
            with self.assertRaises(BuildError):
                conformance.linux_suite("base.tar", "example/base:1")


if __name__ == "__main__":
    unittest.main()
