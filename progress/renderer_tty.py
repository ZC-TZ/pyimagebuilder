"""交互式终端渲染器：可选颜色和单行传输进度刷新。"""

import os

from .progress_bar import progress_text
from .renderer_plain import PlainRenderer


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
            self.stream.write("\r" + " " * self.width + "\r")
            self.stream.flush()
            self.width = 0

    def render(self, event):
        """刷新活动状态行，同时保留已完成事件的日志。"""
        if event.type in ("step_progress", "heartbeat") and not self.quiet:
            message = progress_text(event, bar=True) if event.type == "step_progress" else (
                "... {} ({:.1f}s)".format(event.message, event.duration or 0))
            value = "    " + message
            self.stream.write("\r" + value + " " * max(0, self.width - len(value)))
            self.stream.flush()
            self.width = len(value)
            return
        super().render(event)

    def close(self):
        """结束临时状态行，使后续 shell 提示符从新行开始。"""
        if self.width:
            self.stream.write("\n")
            self.stream.flush()
            self.width = 0
