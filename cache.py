"""持久化指令层缓存：指令输入键与按 DiffID 去重的层文件分开存储。"""

import hashlib
import json
import os
import shutil
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path

from errors import ArchiveError, BuildError
from compat import is_linked_directory
from image_reader import digest_hex, sha256_file, verify_layer_tar


# 旧条目可能包含忽略 COPY/ADD 目录属主、权限的层，必须重新执行指令。
# 更早版本还缺少完整 tar 结构校验，因此不能沿用旧条目的可信身份。
CACHE_VERSION = 9


def cache_key(*values):
    """将影响指令结果的输入稳定序列化，生成缓存键；调用方负责提供完整输入。"""
    raw = json.dumps(values, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class CacheHit:
    """表示缓存结果；无文件系统变化的命中项，其 diff_id 和 path 均为 None。"""
    diff_id: str
    path: Path


class LayerCache:
    """维护 entries 中的指令元数据和 layers 中按内容摘要命名的 tar 文件。"""
    def __init__(self, directory, verify=False):
        self.root = Path(directory).resolve()
        self.verify = verify
        self.entries = self.root / "entries"
        self.layers = self.root / "layers"
        if is_linked_directory(self.entries) or is_linked_directory(self.layers):
            raise BuildError("Cache entries/layers directories must not be symlinks")
        for path in (self.root, self.entries, self.layers):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)

    def lookup(self, key):
        """返回有效缓存结果；元数据过期、缺失或内容损坏时返回 None。

        默认模式下，设备号、inode、大小、mtime 和 ctime 全部匹配时可跳过完整哈希。
        文件身份变化或 verify=True 时重新核对 DiffID 与 tar 格式。
        这只适用于可信本地缓存，不能抵御同时修改内容和缓存元数据的攻击者。
        """
        entry = self.entries / (key + ".json")
        try:
            metadata = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(metadata, dict) or metadata.get("version") != CACHE_VERSION or metadata.get("key") != key:
            return None
        if metadata.get("empty") is True and metadata.get("diff_id") is None:
            return CacheHit(None, None)
        try:
            digest = digest_hex(metadata["diff_id"])
            path = self.layers / (digest + ".tar")
            if not path.is_file() or path.stat().st_size != metadata["size"]:
                return None
            signature = self._identity(path)
            if self.verify or metadata.get("file_identity") != signature:
                if sha256_file(path) != digest:
                    return None
                with open(path, "rb") as checked_layer:
                    verify_layer_tar(checked_layer, "instruction cache layer")
                if not self.verify:
                    metadata["file_identity"] = signature
                    descriptor, pending = tempfile.mkstemp(prefix="entry-", suffix=".part", dir=self.entries)
                    try:
                        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                            json.dump(metadata, output, separators=(",", ":"))
                        os.replace(pending, entry)
                    finally:
                        if os.path.exists(pending):
                            os.unlink(pending)
            return CacheHit(metadata["diff_id"], path)
        except (ArchiveError, KeyError, TypeError, ValueError, OSError, tarfile.TarError):
            return None

    def store(self, key, diff_id, layer_path):
        """先验证并发布层文件，再原子替换指令条目，避免条目引用尚未完成的层。

        同一 DiffID 的层只保存一份；diff_id=None 时记录无文件变化的缓存结果。
        新副本关闭并刷新后再次核对摘要，不能把复制前的源校验当成副本校验。
        """
        if diff_id is None:
            metadata = {"version": CACHE_VERSION, "key": key, "empty": True, "diff_id": None}
        else:
            digest = digest_hex(diff_id)
            source = Path(layer_path)
            if sha256_file(source) != digest:
                raise BuildError("Generated layer DiffID changed before cache publication")
            with open(source, "rb") as checked_layer:
                verify_layer_tar(checked_layer, "generated instruction layer")
            target = self.layers / (digest + ".tar")
            if not target.is_file() or target.stat().st_size != source.stat().st_size or sha256_file(target) != digest:
                descriptor, temporary = tempfile.mkstemp(prefix="layer-", suffix=".part", dir=self.layers)
                try:
                    with os.fdopen(descriptor, "wb") as output, open(source, "rb") as stream:
                        shutil.copyfileobj(stream, output, 4 * 1024 * 1024)
                        output.flush()
                        os.fsync(output.fileno())
                    if sha256_file(temporary) != digest:
                        raise BuildError("Generated layer DiffID changed during cache copy")
                    os.replace(temporary, target)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
            metadata = {"version": CACHE_VERSION, "key": key, "empty": False,
                        "diff_id": diff_id, "size": target.stat().st_size,
                        "file_identity": self._identity(target)}
        descriptor, temporary = tempfile.mkstemp(prefix="entry-", suffix=".part", dir=self.entries)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(metadata, output, separators=(",", ":"))
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.entries / (key + ".json"))
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @staticmethod
    def _identity(path):
        """记录文件身份与时间信息，供可信缓存快速检查；这些字段本身不是内容证明。"""
        data = path.stat()
        return [data.st_dev, data.st_ino, data.st_size, data.st_mtime_ns, data.st_ctime_ns]
