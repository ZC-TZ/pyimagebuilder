"""交互式终端渲染器：可选颜色和单行传输进度刷新。"""

import os
import shutil

from .progress_bar import progress_text
from .renderer_plain import PlainRenderer
from .layers import format_layer_event
from .pull import format_pull_status
from .presentation import phase_name, stream_text, terminal_width


def fit_status_line(value, columns):
    """临时状态不占满终端最后一列，避免自动换行后旧行无法用回车擦除。"""
    limit = max(1, columns - 1)
    if terminal_width(value) <= limit:
        return value
    suffix = "." * min(3, limit)
    available = limit - len(suffix)
    prefix, width = [], 0
    for char in value:
        count = terminal_width(char)
        if width + count > available:
            break
        prefix.append(char)
        width += count
    return "".join(prefix) + suffix


class TTYRenderer(PlainRenderer):
    """以终端状态刷新和可选颜色呈现构建事件。"""
    def __init__(self, stream=None, quiet=False, verbose=False, color=True):
        super().__init__(stream, quiet, verbose)
        self.color = color and "NO_COLOR" not in os.environ
        self.width = 0

    def _line(self, value):
        self._clear()
        if self.color:
            if value.startswith(("Build succeeded", "    OK")):
                value = "\033[32m" + value + "\033[0m"
            elif value.startswith(("Build failed", "    FAILED")):
                value = "\033[31m" + value + "\033[0m"
            elif value.startswith("[+") or value.startswith("["):
                value = "\033[36m" + value + "\033[0m"
            elif "CACHED" in value:
                value = "\033[33m" + value + "\033[0m"
        super()._line(value)

    def _clear(self):
        if self.width:
            columns = shutil.get_terminal_size((120, 24)).columns
            self.stream.write("\r" + " " * min(self.width, max(1, columns - 1)) + "\r")
            self.stream.flush()
            self.width = 0

    def _error_line(self, value):
        self._clear()
        if self.color:
            value = "\033[31m" + value + "\033[0m"
        super()._error_line(value)

    def render(self, event):
        """刷新活动状态行，同时保留已完成事件的日志。"""
        transient_pull = event.type == "pull_status" and not event.details.get("milestone")
        if (event.type in ("step_progress", "heartbeat", "layer_progress") or transient_pull) and not self.quiet:
            pull = event.details if transient_pull else event.details.get("pull")
            message = (format_pull_status(pull) if pull else
                       format_layer_event(event).strip() if event.type == "layer_progress" else
                       progress_text(event, bar=True) if event.type == "step_progress" else (
                "... {} ({:.1f}s)".format(phase_name(event.message), event.duration or 0)))
            columns = shutil.get_terminal_size((120, 24)).columns
            # 转义会增加显示列数，必须在裁剪与擦除宽度计算之前完成。
            value = fit_status_line(stream_text("    " + message, self.stream), columns)
            width = terminal_width(value)
            previous_width = min(self.width, max(1, columns - 1))
            self.stream.write("\r" + value + " " * max(0, previous_width - width))
            self.stream.flush()
            self.width = width
            return
        super().render(event)

    def close(self):
        """结束临时状态行，使后续 shell 提示符从新行开始。"""
        if self.width:
            self.stream.write("\n")
            self.stream.flush()
            self.width = 0
