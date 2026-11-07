"""每行输出一个 BuildEvent JSON 对象，适合服务集成和日志采集。"""

import json
import sys

from .renderer_base import Renderer


class JsonRenderer(Renderer):
    """将构建事件写为机器可读的 JSON Lines。"""
    def __init__(self, stream=None, quiet=False):
        self.stream = stream or sys.stdout
        self.quiet = quiet

    def render(self, event):
        """输出一行紧凑 JSON；quiet 模式只保留最终结果事件。"""
        if self.quiet and event.type not in ("build_success", "build_failed"):
            return
        # JSON 转义保持字段值可逆，也避免终端编码、孤立代理字符和 Unicode
        # 行分隔符破坏输出；此策略只用于事件，不用于镜像 manifest/config。
        print(json.dumps(event.as_dict(), ensure_ascii=True, separators=(",", ":")),
              file=self.stream, flush=True)
