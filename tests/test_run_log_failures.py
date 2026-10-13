"""用真实管道模拟 RUN 输出，验证日志回调失败不会静默成功或提前关闭读端。"""

import errno
import os
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dockerfile_parser import Command
from errors import BuildError
from executor import RunExecutor


class RunLogFailureTests(unittest.TestCase):
    def execute_with_output(self, callback, outputs, diagnostics=b"", exit_code=0,
                            stream_fault=None):
        """模拟子进程写端，仍使用执行器的真实读端及日志线程。"""
        descriptors, pairs, writers, log_threads = [], [], [], []
        writer_errors, thread_errors, written = [], [], {}
        pipe, thread_type = os.pipe, threading.Thread
        fdopen = os.fdopen
        runner = object.__new__(RunExecutor)
        runner.network, runner.sandbox, runner.log = "none", "hardened", callback

        def allocate_pipe():
            pair = pipe()
            pairs.append(pair)
            descriptors.extend(pair)
            return pair

        def write_output(fd, source, payload):
            try:
                offset = 0
                while offset < len(payload):
                    offset += os.write(fd, payload[offset:offset + 4096])
                written[source] = offset
            except BaseException as exc:
                writer_errors.append((source, exc))
            finally:
                os.close(fd)

        def fork():
            # dup 模拟子进程持有的写端；执行器仍自行关闭父进程写端。
            for pair, source, payload in zip(pairs, ("diagnostics", "stdout", "stderr"),
                                             (diagnostics, outputs[0], outputs[1])):
                fd = os.dup(pair[1])
                writer = thread_type(target=write_output, args=(fd, source, payload), daemon=True)
                writers.append(writer)
                writer.start()
            return 123

        def log_thread(target, args, daemon):
            # 旧代码的线程异常也要记录，避免仅靠控制台 traceback 判断测试结果。
            def guarded():
                try:
                    target(*args)
                except BaseException as exc:
                    thread_errors.append(exc)
            result = thread_type(target=guarded, daemon=daemon)
            log_threads.append(result)
            return result

        def waitpid(pid, options):
            self.assertEqual((pid, options), (123, 0))
            for writer in writers:
                writer.join(5)
            self.assertFalse(any(writer.is_alive() for writer in writers), "RUN output drain stalled")
            return pid, 0

        def open_stream(fd, mode):
            if stream_fault is None or fd != pairs[1][0]:
                return fdopen(fd, mode)
            kind, error = stream_fault
            if kind == "open":
                raise error
            stream = fdopen(fd, mode)

            class BrokenReader:
                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    stream.close()

                def __iter__(self):
                    raise error

            return BrokenReader()

        try:
            with patch("executor.os.pipe", side_effect=allocate_pipe), \
                    patch("executor.os.fork", side_effect=fork, create=True), \
                    patch("executor.os.waitpid", side_effect=waitpid, create=True), \
                    patch("executor._wait_status", return_value=exit_code), \
                    patch("executor.os.fdopen", side_effect=open_stream), \
                    patch("threading.Thread", side_effect=log_thread):
                try:
                    runner.execute(SimpleNamespace(), Command(True, "true"),
                                   {}, "/", "", ["/bin/sh", "-c"])
                except BaseException as exc:
                    result = exc
                else:
                    result = None
            for thread in log_threads:
                thread.join(5)
            self.assertFalse(any(thread.is_alive() for thread in log_threads))
            self.assertEqual(thread_errors, [], "Log exception escaped the forwarding thread")
            self.assertEqual(writer_errors, [], "Log reader closed before RUN finished writing")
            self.assertEqual(written, {"diagnostics": len(diagnostics),
                                       "stdout": len(outputs[0]), "stderr": len(outputs[1])})
            for fd in descriptors:
                with self.assertRaises(OSError) as closed:
                    os.fstat(fd)
                self.assertEqual(closed.exception.errno, errno.EBADF)
            return result
        finally:
            for fd in descriptors:
                try:
                    os.close(fd)
                except OSError:
                    pass
            for writer in writers:
                writer.join(5)
            for thread in log_threads:
                thread.join(5)

    def test_callback_failure_drains_large_output_and_fails_run(self):
        payload = (b"x" * 1023 + b"\n") * 256
        for source_index in (0, 1):
            for error in (BrokenPipeError("console disconnected"), ValueError("renderer failed"),
                          KeyboardInterrupt()):
                with self.subTest(source_index=source_index, error=type(error).__name__):
                    calls = []

                    def callback(line, source):
                        calls.append(source)
                        raise error

                    outputs = [b"", b""]
                    outputs[source_index] = payload
                    result = self.execute_with_output(callback, outputs)
                    self.assertIsInstance(result, BuildError)
                    self.assertIn("RUN log forwarding failed", str(result))
                    self.assertIs(result.__cause__, error)
                    self.assertEqual(calls, ["stdout" if source_index == 0 else "stderr"])

    def test_both_streams_are_drained_after_callback_failure(self):
        def callback(line, source):
            raise OSError("output unavailable")

        payload = b"more output\n" * 32768
        result = self.execute_with_output(callback, [payload, payload])
        self.assertIsInstance(result, BuildError)
        self.assertIn("RUN log forwarding failed", str(result))

    def test_run_failure_remains_primary_when_logging_also_fails(self):
        def callback(line, source):
            raise ValueError("renderer failed")

        result = self.execute_with_output(callback, [b"before failure\n" * 8192, b""],
                                          b"rootfs setup failed", exit_code=127)
        self.assertIsInstance(result, BuildError)
        self.assertEqual(str(result), "RUN failed: rootfs setup failed")

    def test_successful_logs_preserve_decoding_and_stream_labels(self):
        lines = []
        result = self.execute_with_output(lambda line, source: lines.append((line, source)),
                                          [b"first\r\n\xff\nlast", b"warning\n"])
        self.assertIsNone(result)
        self.assertEqual([line for line, source in lines if source == "stdout"],
                         ["first", "\ufffd", "last"])
        self.assertEqual([line for line, source in lines if source == "stderr"], ["warning"])

    def test_log_stream_open_failure_reports_error_and_closes_descriptor(self):
        error = OSError(errno.ENOMEM, "cannot create log stream")
        result = self.execute_with_output(lambda *_args: None, [b"", b""],
                                          stream_fault=("open", error))
        self.assertIsInstance(result, BuildError)
        self.assertIn("RUN log forwarding failed for stdout", str(result))
        self.assertIs(result.__cause__, error)

    def test_log_stream_read_failure_is_reported_by_main_thread(self):
        error = OSError(errno.EIO, "cannot read log stream")
        result = self.execute_with_output(lambda *_args: None, [b"", b""],
                                          stream_fault=("read", error))
        self.assertIsInstance(result, BuildError)
        self.assertIn("RUN log forwarding failed for stdout", str(result))
        self.assertIs(result.__cause__, error)


if __name__ == "__main__":
    unittest.main()
