"""按读取字节阈值触发进度回调的流包装器。"""


class CountingReader:
    """包装可读流；interval 是字节增量阈值，不是时间间隔。

    total 为已知的正字节数时，到达总量也会回调；总量未知时 EOF 不强制补发进度。
    """
    def __init__(self, stream, callback, total=None, label="", interval=1024 * 1024):
        self.stream = stream
        self.callback = callback
        self.total = total
        self.label = label
        self.interval = interval
        self.current = 0
        self.reported = 0

    def read(self, size=-1):
        """转发读取；新增字节达到 interval，或累计值达到已知总量时回调。"""
        block = self.stream.read(size)
        self.current += len(block)
        if self.current - self.reported >= self.interval or (self.total and self.current >= self.total):
            self.reported = self.current
            self.callback(self.current, self.total, self.label)
        return block
