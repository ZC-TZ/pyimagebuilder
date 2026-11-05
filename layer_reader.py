"""在一次操作内复用层句柄和成员索引，避免逐文件重扫大 tar。"""

import tarfile
from collections import OrderedDict
from contextlib import ExitStack, contextmanager

from errors import ArchiveError


class LayerReader:
    """限量打开未压缩层；缓存仅属于当前 COPY/SBOM，不跨构建持久化。

    内容成员由 RootFSIndex 固定，不能用硬链接当前目标名或 inode 的最终
    元数据头代替。按原始成员名建立字典，保留 TarFile.getmember 的最后同名项语义。
    """
    def __init__(self, limit=8):
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValueError("Layer reader limit must be a positive integer")
        self.limit = limit
        self.opened = OrderedDict()

    def __enter__(self):
        return self

    def __exit__(self, kind, value, traceback):
        try:
            self.close()
        except BaseException:
            if kind is None:
                raise
            # 已有 COPY/读取错误或取消时，句柄关闭失败不能覆盖原始原因。
        return False

    def member(self, path, name):
        """同一驻留层只解析一次头；返回原始 TarInfo，供按偏移读取内容。"""
        key = str(path)
        item = self.opened.pop(key, None)
        if item is None:
            if len(self.opened) >= self.limit:
                _, (previous, _) = self.opened.popitem(last=False)
                previous.close()
            archive = tarfile.open(path, "r:")
            try:
                members = {member.name: member for member in archive.getmembers()}
            except BaseException:
                try:
                    archive.close()
                except BaseException:
                    pass
                raise
            item = archive, members
        self.opened[key] = item
        archive, members = item
        return archive, members[name]

    @contextmanager
    def open_file(self, entry):
        """读取固定 backing member；上下文关闭内容流，层句柄留给同次操作复用。"""
        archive, member = self.member(entry.layer, entry.member)
        if not member.isfile():
            raise ArchiveError("Backing layer member is not a regular file: " + entry.member)
        stream = archive.extractfile(member)
        if stream is None:
            raise ArchiveError("Unreadable layer file: " + entry.member)
        with stream:
            yield member, stream

    def read_file(self, entry, limit):
        """有界读取配置或包清单，大小取实际内容头，不能取 size=0 的硬链接头。"""
        with self.open_file(entry) as (member, stream):
            if member.size > limit:
                raise ArchiveError("Rootfs config file exceeds size limit: " + entry.member)
            return stream.read(limit + 1)

    def close(self):
        """任何一个关闭失败仍尝试关闭其余层，不把层句柄留到下一次构建。"""
        with ExitStack() as closing:
            for archive, _ in self.opened.values():
                closing.callback(archive.close)
            self.opened.clear()
