"""为工作台暂存浏览器选中的构建文件。"""

import os
import secrets
import stat
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from errors import BuildError
from path_policy import resolve_host_path


MAX_UPLOAD = 2 * 1024 ** 3
MAX_TOTAL = 4 * 1024 ** 3
MAX_FILES = 24
IDLE_SECONDS = 30 * 60
CLEANUP_RETRY_SECONDS = 5


@dataclass
class Upload:
    directory: Path
    path: Path
    size: int
    kind: str
    expires: float = 0
    discarded: bool = False
    identity: tuple = None
    file_identity: tuple = None


def _identity(info):
    """只比较节点身份，写入或 chmod 不能让本次创建的节点变成外来节点。"""
    return (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)) if info.st_ino else None


def _owned_directory(entry):
    """拒绝同名替换、链接和无法确认身份的节点；真正缺失可释放登记。"""
    try:
        info = entry.directory.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(info.st_mode) or entry.identity is None or _identity(info) != entry.identity:
        raise OSError("应用包暂存目录已被替换或无法确认身份")
    try:
        resolved = entry.directory.resolve()
    except RuntimeError as exc:
        raise OSError("应用包暂存目录无法解析") from exc
    if resolved != entry.directory:
        raise OSError("应用包暂存目录已被替换")
    return True


class FastUploads:
    def __init__(self):
        self.lock = threading.Lock()
        self.entries = {}
        self.closed = False

    def _remove(self, token):
        entry = self.entries.get(token)
        if entry is None:
            return
        entry.discarded = True
        entry.expires = time.monotonic() + CLEANUP_RETRY_SECONDS
        if not _owned_directory(entry):
            del self.entries[token]
            return
        try:
            info = entry.path.lstat()
        except FileNotFoundError:
            pass
        else:
            if entry.file_identity is None or _identity(info) != entry.file_identity:
                raise OSError("应用包暂存文件已被替换或无法确认身份")
            entry.path.unlink()
        # 此目录只创建一个上传文件。只删自有节点，再用 rmdir 核实为空；
        # 不递归删除后来放入的文件，也不留下可绕过身份检查的自动 finalizer。
        entry.directory.rmdir()
        del self.entries[token]

    def _ready(self, token, kind):
        entry = self.entries.get(token)
        if (self.closed or entry is None or entry.discarded or
                not entry.expires or entry.expires <= time.monotonic()):
            raise BuildError("暂存文件已过期或已提交，请重新选择应用包")
        if entry.kind != kind:
            raise BuildError("暂存文件类型与当前输入不匹配")
        try:
            if not _owned_directory(entry) or _identity(entry.path.lstat()) != entry.file_identity:
                raise OSError("暂存文件已变更")
        except OSError as exc:
            raise BuildError("暂存文件已移走或替换，请重新选择应用包") from exc
        return entry

    def _reap(self, force=False):
        now = time.monotonic()
        for token, entry in list(self.entries.items()):
            if entry.expires and (force or entry.expires <= now):
                try:
                    self._remove(token)
                except OSError:
                    pass  # 保留配额和清理期限，下轮重试。

    def reap(self):
        with self.lock:
            self._reap()

    def receive(self, source, size, filename, kind="package"):
        if not 0 < size <= MAX_UPLOAD:
            raise BuildError("文件大小必须大于 0，且不超过 2 GiB")
        if (not filename or len(filename) > 255 or "/" in filename or "\\" in filename or
                any(ord(char) < 32 or ord(char) == 127 for char in filename)):
            raise BuildError("无效的应用包文件名")
        name = filename.lower()
        if kind == "package" and (name == "dist.zip" or name.endswith(".war")):
            stored_name = "dist.zip" if name == "dist.zip" else "package.war"
        elif kind == "archive" and name.endswith(".tar"):
            stored_name = "image.tar"
        elif kind == "rootfs" and name.endswith((".tar", ".tar.gz", ".tar.bz2", ".tar.xz", ".tgz")):
            stored_name = "rootfs.tar"
        elif kind == "dockerfile" and size <= 1024 ** 2:
            stored_name = "Dockerfile"
        else:
            raise BuildError("请选择符合要求的文件（WAR、dist.zip、tar 或不超过 1 MiB 的 Dockerfile）")
        with self.lock:
            if self.closed:
                raise BuildError("工作台正在关闭")
            self._reap()
            if len(self.entries) >= MAX_FILES or size + sum(e.size for e in self.entries.values()) > MAX_TOTAL:
                raise BuildError("应用包暂存空间已满，请等待现有任务结束，或直接填写文件路径")
            token = secrets.token_urlsafe(24)
            root = resolve_host_path(tempfile.gettempdir())
            directory = Path(tempfile.mkdtemp(prefix="pyib-fast-upload-", dir=str(root)))
            path = directory / stored_name
            entry = Upload(directory, path, size, kind)
            self.entries[token] = entry
        try:
            entry.identity = _identity(directory.lstat())
            if entry.identity is None:
                raise OSError("无法确认应用包暂存目录身份")
            with path.open("xb") as output:
                entry.file_identity = _identity(os.fstat(output.fileno()))
                if entry.file_identity is None:
                    raise OSError("无法确认应用包暂存文件身份")
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
            try:
                self.finish(token)
            except OSError:
                pass  # 保留原始上传错误，由工作台继续重试清理。
            raise
        return token

    def path(self, token, claim=False, kind="package"):
        with self.lock:
            entry = self._ready(token, kind)
            if claim:
                entry.expires = 0
            return entry.path

    def claim(self, requested):
        with self.lock:
            if len({token for token, _ in requested.values()}) != len(requested):
                raise BuildError("同一个暂存文件不能填入多个位置")
            entries = {field: self._ready(token, kind) for field, (token, kind) in requested.items()}
            for entry in entries.values():
                entry.expires = 0
            return {field: entry.path for field, entry in entries.items()}

    def release(self, tokens, retry=False):
        failure = None
        for token in tokens:
            try:
                if retry:
                    self.unclaim(token)
                else:
                    self.finish(token)
            except OSError as exc:
                failure = exc
        if failure is not None and not retry:
            raise failure

    def unclaim(self, token):
        with self.lock:
            entry = self.entries.get(token)
            if entry is None:
                return
            if self.closed:
                self._remove(token)
            elif not entry.discarded:
                entry.expires = time.monotonic() + IDLE_SECONDS

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
            self._reap(force=True)
