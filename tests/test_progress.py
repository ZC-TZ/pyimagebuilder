"""验证 CI/JSON 进度事件及失败报告的端到端契约。"""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
from test_phase1 import make_base


class ProgressTests(unittest.TestCase):
    def test_plain_cache_and_summary(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = make_base(root)
            context = root / "context"
            context.mkdir()
            (context / "payload").write_bytes(b"hello")
            (context / "Dockerfile").write_text(
                "FROM example/base:1\nCOPY payload /app/payload\nENV A=B\n")
            cache = root / "cache"
            outputs = []
            for index in (1, 2):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = main.main(["build", str(context), "--base-tar", str(base),
                                      "--cache-dir", str(cache), "-t", "demo:1",
                                      "-o", str(root / ("out%d.tar" % index)),
                                      "--progress=plain", "--verbose"])
                self.assertEqual(code, 0)
                outputs.append(output.getvalue())
            self.assertIn("[1/3] FROM", outputs[0])
            self.assertIn("[2/3] COPY", outputs[0])
            self.assertIn("CACHE MISS", outputs[0])
            self.assertIn("CACHED sha256:", outputs[1])
            self.assertIn("Digest:", outputs[1])
            self.assertIn("Layers:", outputs[1])
            self.assertNotIn("\x1b", outputs[1])
            self.assertNotIn("\r", outputs[1])

    def test_json_stream_and_failure_step(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = root / "context"
            context.mkdir()
            (context / "Dockerfile").write_text("FROM scratch\nCOPY missing /app/missing\n")
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = main.main([str(context), "--offline", "-t", "demo:1",
                                  "-o", str(root / "out.tar"), "--progress=json"])
            self.assertEqual(code, 1)
            self.assertEqual(stderr.getvalue(), "")
            events = [json.loads(line) for line in stdout.getvalue().splitlines()]
            self.assertEqual(events[-1]["type"], "build_failed")
            self.assertEqual(events[-1]["step"], 2)
            self.assertEqual(events[-1]["line"], 2)
            self.assertEqual(events[-2]["type"], "step_failed")

    def test_large_copy_and_export_emit_byte_progress(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = root / "context"
            context.mkdir()
            (context / "large.bin").write_bytes(b"x" * (9 * 1024 * 1024))
            (context / "Dockerfile").write_text("FROM scratch\nCOPY large.bin /app/large.bin\n")
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = main.main([str(context), "--offline", "--no-cache", "-t", "demo:1",
                                  "-o", str(root / "large.tar"), "--progress=json"])
            self.assertEqual(code, 0)
            events = [json.loads(line) for line in stdout.getvalue().splitlines()]
            progress = [item for item in events if item["type"] == "step_progress"]
            self.assertTrue(any(item.get("message", "").startswith("Copying") for item in progress))
            self.assertTrue(any(item.get("message") == "Writing Docker layer" for item in progress))
            self.assertEqual(events[-1]["type"], "build_success")
            self.assertEqual(events[-1]["details"]["layers"], 1)


if __name__ == "__main__":
    unittest.main()
