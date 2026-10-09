"""与控制台表现形式无关的构建事件数据。"""

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class BuildEvent:
    """冻结字段赋值的进度事件，供普通日志、TTY 和 JSON 渲染器共用。

    frozen=True 不会冻结 details 内部字典；发送事件后调用方不应再修改其中内容。
    """
    type: str
    elapsed: float
    message: str = ""
    phase: Optional[str] = None
    step: Optional[int] = None
    total: Optional[int] = None
    instruction: Optional[str] = None
    line: Optional[int] = None
    duration: Optional[float] = None
    current: Optional[int] = None
    amount: Optional[int] = None
    unit: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self):
        """省略未设置的可选字段，让 JSON 消费者只接收当前事件实际提供的数据。"""
        return {key: value for key, value in asdict(self).items()
                if value is not None and value != {} and value != ""}
