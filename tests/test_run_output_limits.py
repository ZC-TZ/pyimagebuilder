"""验证 RUN 超长输出分段转发、诊断保留上限及管道持续排空。"""

import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from errors import BuildError
from tests import test_run_log_failures as log_tests


class RunOutputLimitTests(unittest.TestCase):
    def execute(self, callback, outputs, **kwargs):
        return log_tests.RunLogFailureTests().execute_with_output(callback, outputs, **kwargs)

    def test_no_newline_output_has_bounded_fragments(self):
        lines = []
        payload = b"x" * (512 * 1024 + 13)
        result = self.execute(lambda line, source: lines.append((line, source)), [payload, b""])
        self.assertIsNone(result)
        self.assertTrue(lines)
        self.assertTrue(all(source == "stdout" for _, source in lines))
        self.assertTrue(all(len(line) <= 64 * 1024 for line, _ in lines))
        self.assertEqual("".join(line for line, _ in lines).encode("utf-8"), payload)

    def test_fragment_arrives_before_producer_finishes(self):
        arrived = threading.Event()
        lines = []

        def callback(line, source):
            lines.append(line)
            arrived.set()

        payload = b"a" * (128 * 1024)
        result = self.execute(callback, [payload, b""], write_gate=arrived)
        self.assertIsNone(result)
        self.assertEqual("".join(lines).encode("utf-8"), payload)

    def test_utf8_characters_survive_fragment_boundaries(self):
        for suffix in ("", "\n"):
            with self.subTest(suffix=suffix):
                text = "a" * (64 * 1024 - 1) + "中文🙂" * 32768
                lines = []
                result = self.execute(lambda line, source: lines.append(line),
                                      [(text + suffix).encode("utf-8"), b""])
                self.assertIsNone(result)
                self.assertTrue(all(len(line) <= 64 * 1024 for line in lines))
                self.assertEqual("".join(lines), text)

    def test_crlf_across_fragment_boundary_does_not_add_carriage_return(self):
        lines = []
        text = "a" * (64 * 1024 - 1)
        result = self.execute(lambda line, source: lines.append(line),
                              [(text + "\r\nnext\r\n").encode("ascii"), b""])
        self.assertIsNone(result)
        self.assertEqual("".join(lines), text + "next")

    def test_incomplete_utf8_at_eof_is_replaced_once(self):
        lines = []
        payload = b"a" * (64 * 1024 - 1) + b"\xe4"
        result = self.execute(lambda line, source: lines.append(line), [payload, b""])
        self.assertIsNone(result)
        self.assertEqual("".join(lines), payload.decode("utf-8", "replace"))

    def test_short_lines_preserve_blank_lines_and_crlf(self):
        lines = []
        result = self.execute(lambda line, source: lines.append(line),
                              [b"\nline\r\n\nlast\r\n", b""])
        self.assertIsNone(result)
        self.assertEqual(lines, ["", "line", "", "last"])

    def test_huge_diagnostics_keep_traceback_and_final_reason(self):
        head = b"Traceback (most recent call last):\n"
        tail = b"\nBuildError: final failure reason\n"
        diagnostics = head + b"details\n" * (256 * 1024) + tail
        result = self.execute(lambda *_args: None, [b"", b""],
                              diagnostics=diagnostics, exit_code=127)
        self.assertIsInstance(result, BuildError)
        message = str(result)
        self.assertLessEqual(len(message.encode("utf-8")), 256 * 1024 + 256)
        self.assertIn(head.decode("ascii").strip(), message)
        self.assertTrue(message.endswith(tail.decode("ascii").strip()))
        self.assertIn("RUN diagnostics truncated", message)

    def test_small_diagnostics_keep_existing_error_text(self):
        result = self.execute(lambda *_args: None, [b"", b""],
                              diagnostics=b"setup failed\n", exit_code=127)
        self.assertIsInstance(result, BuildError)
        self.assertEqual(str(result), "RUN failed: setup failed")

    def test_diagnostics_at_retention_limit_are_not_truncated(self):
        diagnostics = b"start\n" + b"x" * (256 * 1024 - 12) + b"\nError"
        self.assertEqual(len(diagnostics), 256 * 1024)
        result = self.execute(lambda *_args: None, [b"", b""],
                              diagnostics=diagnostics, exit_code=127)
        self.assertIsInstance(result, BuildError)
        self.assertEqual(str(result), "RUN failed: " + diagnostics.decode("ascii"))

    def test_callback_failure_still_drains_unterminated_output(self):
        error = OSError("renderer unavailable")

        def callback(line, source):
            raise error

        result = self.execute(callback, [b"x" * (512 * 1024), b"y" * (512 * 1024)])
        self.assertIsInstance(result, BuildError)
        self.assertIn("RUN log forwarding failed", str(result))
        self.assertIs(result.__cause__, error)


if __name__ == "__main__":
    unittest.main()
