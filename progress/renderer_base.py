"""构建事件渲染器的最小接口。"""


class Renderer:
    """呈现事件的输出接口；渲染器不参与构建决策。"""
    def render(self, event):
        """消费报告器发送的一条事件。"""
        raise NotImplementedError

    def close(self):
        """成功或失败后结束尚未完成的输出行。"""
        pass
