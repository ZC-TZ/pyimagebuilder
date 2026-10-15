"""线程安全的构建事件发布器，统一进度、日志和阶段耗时。"""

import os
import sys
import threading
import time
from contextlib import contextmanager

from .events import BuildEvent
from .layers import LAYER_EVENTS
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
        self._closed = False
        self.timings = {}
        self.stop = threading.Event()
        self.thread = None
        self._started = False
        self._paused = 0
        self._render_error = None

    def _emit(self, kind, message="", **fields):
        with self.lock:
            if self.finished:
                return
            if self._render_error is not None:
                raise self._render_error
            event = BuildEvent(kind, round(time.monotonic() - self.started, 3),
                               message=message, phase=self.active,
                               step=self.step, total=self.total,
                               instruction=self.instruction, line=self.line, **fields)
            try:
                self.renderer.render(event)
            except BaseException as exc:
                self._render_error = exc
                self.stop.set()
                raise

    def start(self, image, platform, mode="build"):
        """启动事件流和心跳线程，供长时间操作持续报告状态。"""
        initialized = False
        try:
            with self.lock:
                if self._started or self.finished:
                    raise RuntimeError("Build reporter can only be started once")
                self._started = initialized = True
                self._emit("build_start", details={"image": image, "platform": platform, "mode": mode})
                if self._paused:
                    return
                self._start_heartbeat()
        except BaseException:
            if initialized:
                self.close()
            raise

    def _start_heartbeat(self):
        """在事件锁内创建一代心跳；旧代的停止信号不能被恢复操作清除。"""
        self.stop = threading.Event()
        stopped = self.stop
        self.thread = threading.Thread(target=lambda: self._heartbeat(stopped), daemon=True)
        self.thread.start()

    def _heartbeat(self, stopped):
        try:
            while not stopped.wait(self.heartbeat_interval):
                with self.lock:
                    if self.active and self.active_since is not None and not self.finished:
                        duration = time.monotonic() - self.active_since
                        self._emit("heartbeat", self.active, duration=round(duration, 3))
        except BaseException as exc:
            # _emit 已登记渲染异常，下一次主线程输出会报告；后台不能静默成功。
            with self.lock:
                if self._render_error is None and not self.finished:
                    self._render_error = exc
                stopped.set()

    def close(self):
        """幂等结束输出并回收心跳线程；不能持有事件锁等待工作线程。"""
        # finally/except 中的输出收尾不能覆盖 RUN、渲染或用户取消的原始异常。
        # 单独调用 close 时仍报告关闭失败，避免把正常路径的输出故障当成成功。
        unwinding = sys.exc_info()[0] is not None
        try:
            try:
                with self.lock:
                    self.stop.set()
                    self.finished = True
                    if not self._closed:
                        self._closed = True
                        self.renderer.close()
            finally:
                try:
                    thread = self.thread
                    if (thread is not None and thread.ident is not None and
                            thread is not threading.current_thread()):
                        thread.join()
                finally:
                    # 关闭后不再持有异常 traceback；它可能保留整个失败构建的输入对象。
                    with self.lock:
                        self._render_error = None
        except BaseException:
            if not unwinding:
                raise

    @contextmanager
    def pause_heartbeat(self):
        """暂停并回收自己的心跳线程，使 RUN 能在单线程进程中 fork。

        只控制本发布器的线程；执行器仍需检查调用方是否有其他活跃线程。
        日志事件仍可正常输出，退出后恢复心跳；嵌套暂停只在最外层恢复。
        """
        with self.lock:
            self._paused += 1
            self.stop.set()
            thread = self.thread
        failed = False
        try:
            if thread is not None and thread.ident is not None:
                if thread is threading.current_thread():
                    raise RuntimeError("Cannot pause heartbeat from its own thread")
                thread.join()
            yield
        except BaseException:
            failed = True
            raise
        finally:
            with self.lock:
                self._paused -= 1
                if not self._paused and not self.finished and self._render_error is None:
                    if self._started:
                        try:
                            self._start_heartbeat()
                        except BaseException:
                            self.stop.set()
                            # 仅恢复失败时报告它；RUN 已失败时保留原始异常身份。
                            if not failed:
                                raise

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

    def layer_event(self, kind, message="", **fields):
        """发布结构化层任务事件；Dockerfile step 字段仍由当前构建上下文维护。"""
        if kind not in LAYER_EVENTS:
            raise ValueError("Unknown layer event: " + kind)
        self._emit(kind, message, **fields)

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
        try:
            with self.lock:
                if self.finished:
                    return
                try:
                    self.active = self.active_since = None
                    if self.timings:
                        summary["timings"] = {key: round(value, 3)
                                              for key, value in self.timings.items()}
                    self._emit("build_success", duration=round(time.monotonic() - self.started, 3), details=summary)
                finally:
                    self.finished = True
                    self.stop.set()
        finally:
            self.close()

    def fail(self, error):
        """发布包含当前步骤上下文的失败事件，停止心跳并结束输出。"""
        try:
            with self.lock:
                if self.finished:
                    return
                try:
                    # 已失效的输出端不能再报错一次，CLI 仍能正常返回失败状态。
                    if self._render_error is not None:
                        return
                    reason = str(error)
                    if not self.debug and "Traceback (most recent call last)" in reason:
                        reason = reason.strip().splitlines()[-1]
                    if self.active_since is not None and self.step is not None:
                        self._emit("step_failed", reason, duration=round(time.monotonic() - self.active_since, 3))
                    self._emit("build_failed", "{}: {}".format(type(error).__name__, reason),
                               duration=round(time.monotonic() - self.started, 3))
                finally:
                    self.finished = True
                    self.stop.set()
        finally:
            self.close()


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
