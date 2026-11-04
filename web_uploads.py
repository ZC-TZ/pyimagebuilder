"""为工作台暂存浏览器选中的 Fast 应用包。"""

import secrets
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from errors import BuildError


MAX_UPLOAD = 2 * 1024 ** 3
MAX_TOTAL = 4 * 1024 ** 3
MAX_FILES = 24
IDLE_SECONDS = 30 * 60


@dataclass
class Upload:
    directory: tempfile.TemporaryDirectory
    path: Path
    size: int
    expires: float = 0


class FastUploads:
    def __init__(self):
        self.lock = threading.Lock()
        self.entries = {}
        self.closed = False

    def _remove(self, token):
        entry = self.entries[token]
        entry.directory.cleanup()
        del self.entries[token]

    def _ready(self, token):
        entry = self.entries.get(token)
        if self.closed or entry is None or not entry.expires or entry.expires <= time.monotonic():
            raise BuildError("暂存文件已过期或已提交，请重新选择应用包")
        return entry

    def receive(self, source, size, filename):
        if not 0 < size <= MAX_UPLOAD:
            raise BuildError("应用包大小必须大于 0，且不超过 2 GiB")
        if (not filename or len(filename) > 255 or "/" in filename or "\\" in filename or
                any(ord(char) < 32 or ord(char) == 127 for char in filename)):
            raise BuildError("无效的应用包文件名")
        name = filename.lower()
        if name != "dist.zip" and not name.endswith(".war"):
            raise BuildError("请选择 WAR 或名为 dist.zip 的文件")
        with self.lock:
            if self.closed:
                raise BuildError("工作台正在关闭")
            now = time.monotonic()
            for token, entry in list(self.entries.items()):
                if entry.expires and entry.expires <= now:
                    self._remove(token)
            if len(self.entries) >= MAX_FILES or size + sum(e.size for e in self.entries.values()) > MAX_TOTAL:
                raise BuildError("应用包暂存空间已满，请等待现有任务结束，或直接填写文件路径")
            token = secrets.token_urlsafe(24)
            directory = tempfile.TemporaryDirectory(prefix="pyib-fast-upload-")
            path = Path(directory.name) / ("dist.zip" if name == "dist.zip" else "package.war")
            self.entries[token] = Upload(directory, path, size)
        try:
            with path.open("xb") as output:
                remaining = size
                while remaining:
                    block = source.read1(min(remaining, 1024 * 1024))
                    if not block or self.closed:
                        raise BuildError("应用包暂存中断")
                    output.write(block)
                    remaining -= len(block)
            with self.lock:
                if self.closed:
                    raise BuildError("工作台正在关闭")
                self.entries[token].expires = time.monotonic() + IDLE_SECONDS
        except BaseException:
            self.finish(token)
            raise
        return token

    def path(self, token, claim=False):
        with self.lock:
            entry = self._ready(token)
            if claim:
                entry.expires = 0
            return entry.path

    def unclaim(self, token):
        with self.lock:
            if self.closed:
                self._remove(token)
            else:
                self.entries[token].expires = time.monotonic() + IDLE_SECONDS

    def finish(self, token):
        with self.lock:
            self._remove(token)

    def discard(self, token):
        with self.lock:
            entry = self.entries.get(token)
            if entry is not None and entry.expires:
                self._remove(token)

    def close(self):
        with self.lock:
            self.closed = True
            for token, entry in list(self.entries.items()):
                if entry.expires:
                    self._remove(token)
