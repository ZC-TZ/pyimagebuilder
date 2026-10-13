#!/usr/bin/env python3
"""Linux RUN 取消验收：确认 namespace 后台进程、日志线程和管道得到回收。"""

import argparse
import os
import platform
import signal
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import executor
from builder import build
from errors import BuildError
from platforms import host_architecture
from progress import BuildReporter
from progress.renderer_plain import PlainRenderer


class BuildCancelled(KeyboardInterrupt):
    """只用于本脚本触发可识别的主线程取消。"""


def descendants(pid):
    """从宿主 /proc 记录 namespace init 及其当前后代的宿主 PID。"""
    found, pending = set(), [pid]
    while pending:
        current = pending.pop()
        if current in found:
            continue
        found.add(current)
        try:
            children = Path("/proc/{0}/task/{0}/children".format(current)).read_text()
        except FileNotFoundError:
            continue
        pending.extend(int(value) for value in children.split())
    return found


def open_descriptors():
    """枚举仍可读取链接的描述符，忽略枚举 /proc/self/fd 自己产生的短期描述符。"""
    found = set()
    for entry in Path("/proc/self/fd").iterdir():
        try:
            found.add((entry.name, os.readlink(str(entry))))
        except FileNotFoundError:
            pass
    return found


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-tar", type=Path, required=True)
    parser.add_argument("--from", dest="reference", required=True)
    parser.add_argument("--workspace", type=Path, default=Path("/tmp"))
    parser.add_argument("--sandbox", choices=("legacy", "hardened", "rootless"), default="hardened")
    parser.add_argument("--cancel-after", type=float, default=4.0)
    parser.add_argument("--detached", action="store_true", help="额外验证 setsid 后台进程；基础镜像必须含 setsid")
    args = parser.parse_args()
    if platform.system() != "Linux" or host_architecture() is None:
        raise BuildError("Lifecycle acceptance requires a supported Linux host")
    if not 0 < args.cancel_after < 30:
        raise BuildError("--cancel-after must be greater than 0 and less than 30 seconds")
    if threading.active_count() != 1:
        raise BuildError("Run this acceptance script in its own single-threaded Python process")
    previous_alarm = signal.getsignal(signal.SIGALRM)
    with tempfile.TemporaryDirectory(prefix="pyimagebuilder-lifecycle-", dir=args.workspace) as temporary:
        root = Path(temporary)
        context = root / "context"
        context.mkdir()
        dockerfile = context / "Dockerfile"
        command = "sleep 60 & wait"
        if args.detached:
            command = "setsid /bin/sh -c 'sleep 60' & wait"
        dockerfile.write_text("FROM {}\nUSER 0:0\nRUN {}\n".format(args.reference, command), encoding="utf-8")
        runner_file, output = root / "runner.pid", root / "cancelled.tar"
        observed = set()
        original_fork, original_execute = executor._fork_namespace_runner, executor.RunExecutor.execute

        def record_runner():
            pid = original_fork()
            if pid:
                # supervisor 尚未 chroot；只在它的分支记录宿主 PID。
                runner_file.write_text(str(pid), encoding="ascii")
            return pid

        def cancel(_number, _frame):
            if runner_file.is_file():
                observed.update(descendants(int(runner_file.read_text(encoding="ascii"))))
            raise BuildCancelled()

        def execute(self, *values, **options):
            # 使用内核计时器，避免测试自己的计时线程触发 fork 多线程预检。
            signal.setitimer(signal.ITIMER_REAL, args.cancel_after)
            try:
                return original_execute(self, *values, **options)
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0)

        before_fds = open_descriptors()
        reporter = BuildReporter(PlainRenderer(), heartbeat_interval=0.1)
        try:
            signal.signal(signal.SIGALRM, cancel)
            reporter.start("pyimagebuilder/lifecycle:cancelled", "linux/" + host_architecture())
            with patch.object(executor, "_fork_namespace_runner", record_runner), \
                    patch.object(executor.RunExecutor, "execute", execute):
                try:
                    build(dockerfile, context, args.base_tar, None,
                          "pyimagebuilder/lifecycle:cancelled", output,
                          enable_run=True, workspace_dir=args.workspace,
                          run_sandbox=args.sandbox, target_platform="linux/" + host_architecture(),
                          reporter=reporter)
                except BuildCancelled:
                    pass
                else:
                    raise BuildError("RUN finished before cancellation; inspect shell/sleep availability")
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_alarm)
            reporter.close()
        if len(observed) < 2:
            raise BuildError("Cancellation did not observe a namespace init and background descendant")
        # 被杀后代可能短暂等待宿主 init 回收；只给予有限的验收等待时间。
        deadline = time.monotonic() + 5
        while any(Path("/proc/" + str(pid)).exists() for pid in observed):
            if time.monotonic() >= deadline:
                raise BuildError("A RUN namespace process remains after cancellation")
            time.sleep(0.05)
        try:
            os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            pass
        else:
            raise BuildError("The acceptance process still owns an unreaped RUN child")
        if output.exists() or threading.active_count() != 1:
            raise BuildError("Cancelled build published an archive or retained a background thread")
        if open_descriptors() - before_fds:
            raise BuildError("Cancelled build retained an open file descriptor")
    print("Linux RUN lifecycle acceptance passed: cancellation, descendants, threads, no output archive")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (BuildError, OSError) as exc:
        print("Linux RUN lifecycle acceptance failed: " + str(exc), file=sys.stderr)
        sys.exit(1)
