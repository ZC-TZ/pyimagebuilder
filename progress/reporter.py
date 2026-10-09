"""线程安全的构建事件发布器，统一进度、日志和阶段耗时。"""

import os
import sys
import threading
import time
from contextlib import contextmanager

from .events import BuildEvent
from .renderer_json import JsonRenderer
from .renderer_plain import PlainRenderer
from .renderer_tty import TTYRenderer


class BuildReporter:
    """发布与输出形式无关的构建事件，并汇总阶段耗时。"""
    def __init__(self, renderer, *, heartbeat_interval=1.0, progress_interval=0.25,
                 debug=False, verbose=False):
        self.renderer = renderer
        self.debug = debug
        self.verbose = verbose or debug
        self.heartbeat_interval = heartbeat_interval
        self.progress_interval = progress_interval
        self.lock = threading.RLock()
        self.started = time.monotonic()
        self.active = None
        self.active_since = None
        self.step = None
        self.total = None
        self.instruction = None
        self.line = None
        self.last_progress = 0.0
        self.finished = False
        self.timings = {}
        self.stop = threading.Event()
        self.thread = None

    def _emit(self, kind, message="", **fields):
        with self.lock:
            if self.finished:
                return
            event = BuildEvent(kind, round(time.monotonic() - self.started, 3),
                               message=message, phase=self.active,
                               step=self.step, total=self.total,
                               instruction=self.instruction, line=self.line, **fields)
            self.renderer.render(event)

    def start(self, image, platform, mode="build"):
        """启动事件流和心跳线程，供长时间操作持续报告状态。"""
        self._emit("build_start", details={"image": image, "platform": platform, "mode": mode})
        self.thread = threading.Thread(target=self._heartbeat, daemon=True)
        self.thread.start()

    def _heartbeat(self):
        while not self.stop.wait(self.heartbeat_interval):
            with self.lock:
                if self.active and self.active_since is not None and not self.finished:
                    duration = time.monotonic() - self.active_since
                    self._emit("heartbeat", self.active, duration=round(duration, 3))

    def phase_start(self, message):
        """标记 Dockerfile 指令以外的阶段，如下载或导出。"""
        with self.lock:
            self.active = message
            self.active_since = time.monotonic()
            self.step = self.instruction = self.line = None
            self._emit("phase_start", message)

    def phase_success(self, message=None):
        """完成当前阶段并报告实际耗时。"""
        with self.lock:
            duration = time.monotonic() - self.active_since if self.active_since else 0
            self._emit("phase_success", message or self.active or "done", duration=round(duration, 3))
            self.active = self.active_since = None

    def step_start(self, index, total, instruction, line):
        """把后续日志和进度关联到当前 Dockerfile 指令及源行号。"""
        with self.lock:
            self.step, self.total, self.instruction, self.line = index, total, instruction, line
            self.active = instruction
            self.active_since = time.monotonic()
            self._emit("step_start")

    def step_success(self, message="done"):
        """报告当前 Dockerfile 指令完成。"""
        with self.lock:
            duration = time.monotonic() - self.active_since if self.active_since else 0
            self._emit("step_success", message, duration=round(duration, 3))
            self.active = self.active_since = None

    def cache_hit(self, message="layer"):
        """报告指令复用了缓存层。"""
        self._emit("step_cache_hit", message)

    def cache_miss(self):
        """报告指令需要重新生成层。"""
        self._emit("step_cache_miss")

    @contextmanager
    def timed(self, label):
        """累计指定操作的耗时；即使操作失败也会在 finally 中记录。"""
        started = time.monotonic()
        try:
            yield
        finally:
            with self.lock:
                self.timings[label] = self.timings.get(label, 0.0) + time.monotonic() - started

    def record_timing(self, label, seconds):
        """把调用方测得的耗时加入指定阶段。"""
        with self.lock:
            self.timings[label] = self.timings.get(label, 0.0) + seconds

    def log(self, message, stream="stdout"):
        """逐行发送日志，并标明 stdout 或 stderr 来源。"""
        for line in str(message).splitlines():
            self._emit("step_log", line, details={"stream": stream})

    def progress(self, current, total=None, message="", unit="bytes", force=False):
        """限制进度事件频率，同时确保最终进度不被节流丢弃。"""
        now = time.monotonic()
        with self.lock:
            if not force and now - self.last_progress < self.progress_interval and current != total:
                return
            self.last_progress = now
            self._emit("step_progress", message, current=current, amount=total, unit=unit)

    def success(self, **summary):
        """发布成功摘要，停止心跳并结束渲染输出。"""
        with self.lock:
            self.active = self.active_since = None
            if self.timings:
                summary["timings"] = {key: round(value, 3)
                                      for key, value in self.timings.items()}
            self._emit("build_success", duration=round(time.monotonic() - self.started, 3), details=summary)
            self.finished = True
            self.stop.set()
            self.renderer.close()

    def fail(self, error):
        """发布包含当前步骤上下文的失败事件，停止心跳并结束输出。"""
        with self.lock:
            reason = str(error)
            if not self.debug and "Traceback (most recent call last)" in reason:
                reason = reason.strip().splitlines()[-1]
            if self.active_since is not None and self.step is not None:
                self._emit("step_failed", reason, duration=round(time.monotonic() - self.active_since, 3))
            self._emit("build_failed", "{}: {}".format(type(error).__name__, reason),
                       duration=round(time.monotonic() - self.started, 3))
            self.finished = True
            self.stop.set()
            self.renderer.close()


def make_reporter(progress="auto", quiet=False, verbose=False, debug=False,
                  no_color=False, stream=None):
    """根据 CLI 选项与输出流能力选择 JSON、普通日志或 TTY 渲染器。"""
    stream = stream if stream is not None else sys.stdout
    if progress == "json":
        renderer = JsonRenderer(stream, quiet=quiet)
    elif progress == "plain" or os.environ.get("CI") or not getattr(stream, "isatty", lambda: False)():
        renderer = PlainRenderer(stream, quiet, verbose or debug)
    else:
        renderer = TTYRenderer(stream, quiet, verbose or debug, color=not no_color)
    return BuildReporter(renderer, debug=debug, verbose=verbose)
