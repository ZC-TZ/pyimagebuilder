"""用真实管道验证 RUN 执行准备失败后的描述符回收，不创建容器或子进程。"""

import errno
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dockerfile_parser import Command
from executor import RunExecutor


class RunPipeCleanupTests(unittest.TestCase):
    def runner(self, logged):
        result = object.__new__(RunExecutor)
        result.network, result.sandbox = "none", "hardened"
        if logged:
            result.log = lambda *_args: None
        return result

    def assert_closed(self, descriptors):
        for fd in descriptors:
            with self.subTest(fd=fd):
                try:
                    os.fstat(fd)
                except OSError as exc:
                    self.assertEqual(exc.errno, errno.EBADF)
                else:
                    self.fail("RUN preparation leaked descriptor {}".format(fd))

    def close_test_descriptors(self, descriptors):
        # 修复前的复现也必须释放真实句柄，不能污染后续测试。
        for fd in descriptors:
            try:
                os.close(fd)
            except OSError:
                pass

    def test_fork_failure_closes_all_allocated_pipes(self):
        for logged in (False, True):
            for error in (OSError(errno.EAGAIN, "fork unavailable"), KeyboardInterrupt()):
                with self.subTest(logged=logged, error=type(error).__name__):
                    descriptors = []
                    original = os.pipe

                    def pipe():
                        pair = original()
                        descriptors.extend(pair)
                        return pair

                    try:
                        with patch("executor.os.pipe", side_effect=pipe), \
                                patch("executor.os.fork", side_effect=error, create=True):
                            with self.assertRaises(type(error)) as raised:
                                self.runner(logged).execute(SimpleNamespace(), Command(True, "true"),
                                                            {}, "/", "", ["/bin/sh", "-c"])
                            self.assertIs(raised.exception, error)
                        self.assertEqual(len(descriptors), 6 if logged else 2)
                        self.assert_closed(descriptors)
                    finally:
                        self.close_test_descriptors(descriptors)

    def test_partial_pipe_allocation_failure_closes_previous_pairs(self):
        for failed_call in (2, 3):
            with self.subTest(failed_call=failed_call):
                descriptors = []
                calls = []
                original = os.pipe
                error = OSError(errno.EMFILE, "pipe unavailable")

                def pipe():
                    calls.append(1)
                    if len(calls) == failed_call:
                        raise error
                    pair = original()
                    descriptors.extend(pair)
                    return pair

                try:
                    with patch("executor.os.pipe", side_effect=pipe), \
                            patch("executor.os.fork", create=True) as fork:
                        with self.assertRaises(OSError) as raised:
                            self.runner(True).execute(SimpleNamespace(), Command(True, "true"),
                                                      {}, "/", "", ["/bin/sh", "-c"])
                        self.assertIs(raised.exception, error)
                        fork.assert_not_called()
                    self.assertEqual(len(descriptors), (failed_call - 1) * 2)
                    self.assert_closed(descriptors)
                finally:
                    self.close_test_descriptors(descriptors)

    def test_partial_pipe_allocation_interrupt_closes_previous_pairs(self):
        descriptors = []
        original = os.pipe

        def pipe():
            if descriptors:
                raise KeyboardInterrupt()
            pair = original()
            descriptors.extend(pair)
            return pair

        try:
            with patch("executor.os.pipe", side_effect=pipe), \
                    patch("executor.os.fork", create=True) as fork:
                with self.assertRaises(KeyboardInterrupt):
                    self.runner(True).execute(SimpleNamespace(), Command(True, "true"),
                                              {}, "/", "", ["/bin/sh", "-c"])
                fork.assert_not_called()
            self.assert_closed(descriptors)
        finally:
            self.close_test_descriptors(descriptors)

    def test_cleanup_attempts_other_fds_and_preserves_fork_error(self):
        descriptors, closed = [], []
        pipe, close = os.pipe, os.close
        error = OSError(errno.EAGAIN, "original fork failure")

        def tracked_pipe():
            pair = pipe()
            descriptors.extend(pair)
            return pair

        def unreliable_close(fd):
            closed.append(fd)
            close(fd)
            if len(closed) == 1:
                raise OSError(errno.EIO, "close reported a failure")

        try:
            with patch("executor.os.pipe", side_effect=tracked_pipe), \
                    patch("executor.os.close", side_effect=unreliable_close), \
                    patch("executor.os.fork", side_effect=error, create=True):
                with self.assertRaises(OSError) as raised:
                    self.runner(True).execute(SimpleNamespace(), Command(True, "true"),
                                              {}, "/", "", ["/bin/sh", "-c"])
                self.assertIs(raised.exception, error)
            self.assertEqual(set(closed), set(descriptors))
            self.assert_closed(descriptors)
        finally:
            self.close_test_descriptors(descriptors)

    def test_successful_parent_path_still_closes_all_pipes(self):
        for logged in (False, True):
            with self.subTest(logged=logged):
                descriptors = []
                original = os.pipe

                def pipe():
                    pair = original()
                    descriptors.extend(pair)
                    return pair

                try:
                    with patch("executor.os.pipe", side_effect=pipe), \
                            patch("executor.os.fork", return_value=123, create=True), \
                            patch("executor.os.waitpid", return_value=(123, 0), create=True), \
                            patch("executor._wait_status", return_value=0):
                        # 无实际子进程，父分支关闭写端后读到 EOF；日志线程使用真实管道。
                        self.runner(logged).execute(SimpleNamespace(), Command(True, "true"),
                                                   {}, "/", "", ["/bin/sh", "-c"])
                    self.assert_closed(descriptors)
                finally:
                    self.close_test_descriptors(descriptors)


if __name__ == "__main__":
    unittest.main()

