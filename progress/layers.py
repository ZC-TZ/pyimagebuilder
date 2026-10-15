"""跨下载、缓存读取和构建复用的层任务生命周期，不依赖控制台文字解析。"""

import threading
import time
import uuid
from contextlib import contextmanager
from concurrent.futures import CancelledError

from .events import BuildEvent
from .progress_bar import size


LAYER_EVENTS = frozenset(("layer_start", "layer_progress", "layer_complete",
                          "layer_failed", "layers_complete"))
OPERATIONS = {"download": "下载镜像层", "prepare": "准备镜像层", "verify": "校验基础层",
              "read": "读取基础层", "index": "应用基础层", "store": "写入镜像层"}


def format_layer_event(event):
    """普通控制台与独立下载脚本共用格式；层序号和实际完成数量分别显示。"""
    data = event.details
    label = OPERATIONS.get(data["operation"], data["operation"])
    total, completed = data["layers_total"], data["layers_completed"]
    if event.type == "layers_complete":
        return "    OK {}完成 | {}/{} 层".format(label, completed, total)
    prefix = "{} {}/{}".format(label, data["layer_index"], total)
    if event.type == "layer_start":
        return "    > {} | 开始".format(prefix)
    if event.type == "layer_progress":
        amount = " / " + size(event.amount) if event.amount is not None else ""
        return "    {} | 已处理 {}{}".format(prefix, size(event.current or 0), amount)
    if event.type == "layer_failed":
        return "    FAILED {} | {}: {}".format(
            prefix, "已取消" if data.get("cancelled") else "失败", event.message)
    cached = "（复用缓存）" if data.get("cached") else ""
    return "    OK {} | 完成{} | {} | {:.2f}s | 已完成 {}/{}".format(
        prefix, cached, size(event.current or 0), event.duration or 0, completed, total)


class LayerTask:
    """单层任务的字节状态；节流只影响中间事件，不影响生命周期事件。"""
    def __init__(self, group, index, digest):
        self.group, self.index, self.digest = group, index, digest
        self.started = self.last_report = time.monotonic()
        self.current = 0
        self.amount = None
        self.cached = False

    def update(self, current, amount=None):
        """报告本层已处理字节；各层独立节流，避免并发大层遮蔽小层状态。"""
        with self.group.lock:
            self.current, self.amount = current, amount
            now = time.monotonic()
            if now - self.last_report >= self.group.interval:
                self.group._send("layer_progress", self)
                self.last_report = now


class LayerProgress:
    """管理一组有序层的生命周期，允许工作线程按任意顺序完成。

    每层必须经过一次上下文；异常时发失败事件并保留原始异常，只有全部成功
    后才能 finish。完成数量按层索引计数，不能用最后完成的层序号代替。
    reporter 用于统一事件流；display 仅供独立脚本通过同一格式器输出。
    """
    def __init__(self, operation, total, reporter=None, display=None, source="", image="", interval=1.0):
        if type(total) is not int or total < 0:
            raise ValueError("Layer total must be a nonnegative integer")
        self.operation, self.total = operation, total
        self.reporter, self.display = reporter, display
        self.source, self.image, self.interval = source, image, interval
        self.group_id = uuid.uuid4().hex
        self.lock = threading.RLock()
        self.started, self.completed, self.failed = set(), set(), set()
        self.finished = False

    def _send(self, kind, task=None, error=None):
        details = {"group_id": self.group_id, "operation": self.operation,
                   "layers_total": self.total, "layers_completed": len(self.completed),
                   "source": self.source, "image": self.image}
        fields = {"details": details}
        if task is not None:
            details.update(layer_index=task.index, digest=task.digest, cached=task.cached)
            fields.update(current=task.current, amount=task.amount,
                          duration=round(time.monotonic() - task.started, 3), unit="bytes")
        if error is not None:
            details["cancelled"] = isinstance(error, (KeyboardInterrupt, CancelledError))
        message = "{}: {}".format(type(error).__name__, str(error).splitlines()[0] if str(error) else "") if error else ""
        if self.reporter is not None:
            self.reporter.layer_event(kind, message, **fields)
        elif self.display is not None:
            event = BuildEvent(kind, 0, message=message, **fields)
            output = self.display.status if kind == "layer_progress" else self.display.log
            output(format_layer_event(event))

    @contextmanager
    def layer(self, index, digest=""):
        """成功退出才登记完成；错误输出失败时也不能替换正在传播的处理错误。"""
        with self.lock:
            if self.finished or type(index) is not int or not 1 <= index <= self.total or index in self.started:
                raise ValueError("Invalid or duplicate layer lifecycle: " + repr(index))
            self.started.add(index)
            task = LayerTask(self, index, digest)
            self._send("layer_start", task)
        try:
            yield task
        except BaseException as error:
            with self.lock:
                self.failed.add(index)
                try:
                    self._send("layer_failed", task, error)
                except BaseException:
                    pass  # 输出端已失效时，仍向调用者传播原始处理/取消异常。
            raise
        else:
            with self.lock:
                self.completed.add(index)
                self._send("layer_complete", task)

    def finish(self):
        """只发布一次全组完成；失败、漏处理或仍在运行的组不能伪报成功。"""
        with self.lock:
            if self.finished or self.failed or len(self.completed) != self.total:
                raise RuntimeError("Layer group is incomplete, failed or already finished")
            self._send("layers_complete")
            self.finished = True
