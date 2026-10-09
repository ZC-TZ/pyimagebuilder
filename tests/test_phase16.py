"""通过独立 Python 进程验证严格输入锁定。"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]


class PhaseSixteenTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.context = self.root / "context"
        self.context.mkdir()
        (self.context / "Dockerfile").write_text(
            "FROM scratch\nCOPY payload.txt /app/payload.txt\nCMD [\"/app/payload.txt\"]\n",
            encoding="utf-8")
        (self.context / "payload.txt").write_text("locked contents\n", encoding="utf-8")
        self.lock = self.root / "inputs.lock.json"
        self.common = ["--dockerfile", str(self.context / "Dockerfile"),
                       "--context", str(self.context), "--tag", "example/hermetic:16",
                       "--source-date-epoch", "42"]

    def invoke(self, script, *arguments, hash_seed="0"):
        environment = os.environ.copy()
        if hash_seed is None:
            environment.pop("PYTHONHASHSEED", None)
        else:
            environment["PYTHONHASHSEED"] = hash_seed
        return subprocess.run([sys.executable, str(PROJECT / script), *arguments],
                              cwd=self.root, env=environment, capture_output=True,
                              text=True)

    def create_lock(self):
        result = self.invoke("hermetic.py", *self.common, "--output", str(self.lock))
        self.assertEqual(result.returncode, 0, result.stderr)

    def build(self, output, *options):
        return self.invoke("main.py", *self.common, "--output", str(output),
                           "--hermetic", "--input-lock", str(self.lock), *options)

    def test_locked_rebuild_is_byte_identical_and_has_a_report(self):
        self.create_lock()
        first = self.root / "first.tar"
        second = self.root / "second.tar"
        for output in (first, second):
            result = self.build(output)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(Path(str(output) + ".hermetic.json").is_file())
        self.assertEqual(first.read_bytes(), second.read_bytes())
        report = json.loads(Path(str(first) + ".hermetic.json").read_text())
        self.assertEqual(report["policy"], "no-run-local-snapshot-v1")
        self.assertEqual(len(report["outputs"]["primarySha256"]), 64)

    def test_changed_input_rejected_without_publishing(self):
        self.create_lock()
        (self.context / "payload.txt").write_text("new contents\n", encoding="utf-8")
        output = self.root / "changed.tar"
        result = self.build(output)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("input lock differs", result.stderr)
        self.assertFalse(output.exists())

    def test_docker_and_oci_outputs_are_recorded(self):
        self.common.extend(["--format", "both"])
        self.create_lock()
        output = self.root / "dual.tar"
        result = self.build(output)
        self.assertEqual(result.returncode, 0, result.stderr)
        oci = self.root / "dual.oci.tar"
        self.assertTrue(oci.is_file())
        report = json.loads(Path(str(output) + ".hermetic.json").read_text())
        self.assertEqual(len(report["outputs"]["primarySha256"]), 64)
        self.assertEqual(len(report["outputs"]["ociSha256"]), 64)

    def test_run_rejected_when_locking(self):
        (self.context / "Dockerfile").write_text("FROM scratch\nRUN echo hi\n", encoding="utf-8")
        result = self.invoke("hermetic.py", *self.common, "--output", str(self.lock))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("rejects all RUN", result.stderr)

    def test_hash_seed_and_host_architecture_arguments_rejected(self):
        result = self.invoke("hermetic.py", *self.common, "--output", str(self.lock),
                             hash_seed=None)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PYTHONHASHSEED=0", result.stderr)
        (self.context / "Dockerfile").write_text(
            "FROM scratch\nARG BUILDARCH\nENV ARCH=$BUILDARCH\n", encoding="utf-8")
        result = self.invoke("hermetic.py", *self.common, "--output", str(self.lock))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("BUILDPLATFORM/BUILDARCH", result.stderr)

    def test_cli_rejects_cache_and_secret_options(self):
        self.create_lock()
        result = self.build(self.root / "rejected.tar", "--cache-dir", str(self.root / "cache"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--hermetic rejects", result.stderr)

    def test_context_symlink_rejected_if_supported(self):
        try:
            (self.context / "link").symlink_to("payload.txt")
        except (OSError, NotImplementedError):
            self.skipTest("Symlink creation unavailable")
        result = self.invoke("hermetic.py", *self.common, "--output", str(self.lock))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("context symlinks", result.stderr)


if __name__ == "__main__":
    unittest.main()
