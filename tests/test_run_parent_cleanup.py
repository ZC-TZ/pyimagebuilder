"""父分支用真实管道和日志线程，系统进程及信号用替身，核对中断后的所有权。"""

import errno
import os
import signal
import sys
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import executor
from dockerfile_parser import Command
from errors import UnsupportedInstruction


class Exited(BaseException):
    pass


class RunParentCleanupTests(unittest.TestCase):
    def run_failure(self, mode, logged=True, fail_thread=0):
        descriptors, pairs, duplicates, threads, aborted = [], [], [], [], []
        pipe, thread_type, fdopen = os.pipe, threading.Thread, os.fdopen
        error = KeyboardInterrupt() if mode == "interrupt" else OSError("parent operation failed")
        runner = object.__new__(executor.RunExecutor)
        runner.network, runner.sandbox = "none", "legacy"
        if logged:
            runner.log = lambda *_args: None

        def allocate():
            pair = pipe()
            pairs.append(pair)
            descriptors.extend(pair)
            return pair

        def fork():
            # 保留子进程写端，使已启动的日志线程真实阻塞；abort 后才出现 EOF。
            duplicates.extend(os.dup(pair[1]) for pair in pairs)
            return 123456

        def abort(pid):
            aborted.append(pid)
            for fd in duplicates:
                os.close(fd)
            duplicates.clear()

        class FailedThread(thread_type):
            def start(self):
                raise error

        def factory(**kwargs):
            index = len(threads)
            thread = FailedThread(**kwargs) if mode == "thread" and index == fail_thread else thread_type(**kwargs)
            threads.append(thread)
            return thread

        def adopt(fd, *args, **kwargs):
            if mode == "adopt" and fd == pairs[0][0]:
                raise error
            return fdopen(fd, *args, **kwargs)

        def diagnostic(stream):
            if mode in ("interrupt", "read"):
                raise error
            return ""

        try:
            with patch.object(executor.os, "pipe", side_effect=allocate), \
                    patch.object(executor.os, "fork", side_effect=fork, create=True), \
                    patch.object(executor.os, "waitpid", side_effect=error, create=True), \
                    patch.object(executor.os, "fdopen", side_effect=adopt), \
                    patch.object(executor, "_abort_run", side_effect=abort, create=True), \
                    patch.object(executor, "_read_run_diagnostics", side_effect=diagnostic), \
                    patch.object(threading, "Thread", side_effect=factory):
                with self.assertRaises(type(error)) as raised:
                    runner.execute(SimpleNamespace(), Command(True, "true"), {}, "/", "", ["/bin/sh", "-c"])
            self.assertIs(raised.exception, error)
            self.assertEqual(aborted, [123456])
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            for fd in descriptors:
                with self.assertRaises(OSError) as closed:
                    os.fstat(fd)
                self.assertEqual(closed.exception.errno, errno.EBADF)
        finally:
            # 旧提交缺失收尾也不能让测试本身残留阻塞的读线程。
            for fd in duplicates + descriptors:
                try:
                    os.close(fd)
                except OSError:
                    pass
            for thread in threads:
                if thread.ident is not None:
                    thread.join(5)

    def test_diagnostic_read_interrupt_reaps_child_and_closes_pipes(self):
        for logged in (False, True):
            with self.subTest(logged=logged):
                self.run_failure("interrupt", logged)

    def test_diagnostic_stream_adoption_failure_reaps_child(self):
        self.run_failure("adopt")

    def test_partial_log_thread_start_failure_reaps_child(self):
        for index in (0, 1):
            with self.subTest(index=index):
                self.run_failure("thread", fail_thread=index)

    def test_waitpid_failure_does_not_skip_pipe_cleanup(self):
        self.run_failure("wait")

    def test_known_background_thread_is_rejected_before_pipe_creation(self):
        stop = threading.Event()
        worker = threading.Thread(target=stop.wait)
        worker.start()
        try:
            with patch.object(executor.os, "pipe") as pipe, \
                    patch.object(executor.os, "fork", side_effect=AssertionError("unexpected fork"), create=True):
                try:
                    executor._fork_with_pipes(True)
                except BaseException as exc:
                    error = exc
                else:
                    error = None
            self.assertIsInstance(error, UnsupportedInstruction)
            pipe.assert_not_called()
        finally:
            stop.set()
            worker.join(5)

    def test_abort_does_not_signal_an_already_reaped_pid(self):
        with patch.object(executor.os, "waitpid", side_effect=ChildProcessError(), create=True), \
                patch.object(executor.os, "kill") as kill:
            executor._abort_run(123456)
        kill.assert_not_called()

    def test_abort_reaps_exited_child_without_signalling(self):
        with patch.object(executor.os, "waitpid", return_value=(123456, 0), create=True), \
                patch.object(executor.os, "kill") as kill:
            executor._abort_run(123456)
        kill.assert_not_called()

    def test_abort_requests_supervisor_termination_then_waits(self):
        calls = []
        with patch.object(executor.os, "waitpid", side_effect=lambda pid, flags:
                          (calls.append(("wait", pid, flags)) or ((0, 0) if flags else (pid, 0))), create=True), \
                patch.object(executor.os, "kill", side_effect=lambda pid, sig: calls.append(("kill", pid, sig))):
            executor._abort_run(123456)
        self.assertEqual(calls, [("wait", 123456, getattr(os, "WNOHANG", 1)),
                                 ("kill", 123456, signal.SIGTERM), ("wait", 123456, 0)])

    def test_runner_creation_blocks_termination_until_handler_knows_pid(self):
        events, handlers = [], {}

        def handler(number, callback):
            events.append("handler")
            handlers[number] = callback

        with ExitStack() as stack:
            stack.enter_context(patch.object(executor.signal, "SIG_BLOCK", 0, create=True))
            stack.enter_context(patch.object(executor.signal, "SIG_SETMASK", 2, create=True))
            stack.enter_context(patch.object(executor.signal, "SIGKILL", 9, create=True))
            stack.enter_context(patch.object(executor.signal, "pthread_sigmask",
                                side_effect=lambda operation, values: (events.append((operation, values)) or set()),
                                create=True))
            stack.enter_context(patch.object(executor.signal, "signal", side_effect=handler))
            stack.enter_context(patch.object(executor.os, "fork", side_effect=lambda:
                                            (events.append("fork") or 123456), create=True))
            kill = stack.enter_context(patch.object(executor.os, "kill"))
            wait = stack.enter_context(patch.object(executor, "_wait_child",
                                       side_effect=lambda pid, flags=0: (0, 0) if flags else (pid, 0)))
            leave = stack.enter_context(patch.object(executor.os, "_exit", side_effect=Exited))
            self.assertEqual(executor._fork_namespace_runner(), 123456)
            terminate = handlers[signal.SIGTERM]
            self.assertEqual(events[:4], [(0, {signal.SIGTERM}), "fork", "handler", (2, set())])
            with self.assertRaises(Exited):
                terminate(signal.SIGTERM, None)
            kill.assert_called_once_with(123456, 9)
            self.assertEqual(wait.call_args_list, [unittest.mock.call(123456, getattr(os, "WNOHANG", 1)),
                                                   unittest.mock.call(123456)])
            leave.assert_called_once_with(128 + signal.SIGTERM)

    def test_late_supervisor_termination_never_signals_reaped_runner(self):
        for result in ((123456, 0), ChildProcessError()):
            with self.subTest(result=type(result).__name__), ExitStack() as stack:
                handlers = {}
                stack.enter_context(patch.object(executor.signal, "SIG_BLOCK", 0, create=True))
                stack.enter_context(patch.object(executor.signal, "SIG_SETMASK", 2, create=True))
                stack.enter_context(patch.object(executor.signal, "pthread_sigmask", return_value=set(), create=True))
                stack.enter_context(patch.object(executor.signal, "signal",
                                    side_effect=lambda number, handler: handlers.update({number: handler})))
                stack.enter_context(patch.object(executor.os, "fork", return_value=123456, create=True))
                kill = stack.enter_context(patch.object(executor.os, "kill"))
                wait = stack.enter_context(patch.object(executor, "_wait_child",
                                    **({"side_effect": result} if isinstance(result, BaseException)
                                       else {"return_value": result})))
                leave = stack.enter_context(patch.object(executor.os, "_exit", side_effect=Exited))
                executor._fork_namespace_runner()
                with self.assertRaises(Exited):
                    handlers[signal.SIGTERM](signal.SIGTERM, None)
                kill.assert_not_called()
                wait.assert_called_once_with(123456, getattr(os, "WNOHANG", 1))
                leave.assert_called_once_with(128 + signal.SIGTERM)

    def test_diagnostic_write_failure_still_exits_child(self):
        with patch.object(executor.os, "write", side_effect=BrokenPipeError()), \
                patch.object(executor.os, "_exit", side_effect=Exited) as leave:
            try:
                raise ValueError("setup failed")
            except ValueError:
                with self.assertRaises(Exited):
                    executor._exit_with_diagnostics(123)
        leave.assert_called_once_with(127)


if __name__ == "__main__":
    unittest.main()
