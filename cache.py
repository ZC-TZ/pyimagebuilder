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
from cache_identity import file_identity
from store_lock import file_lock
from path_policy import cache_entry_key
from image_reader import digest_hex, sha256_file, verify_layer_tar


# 旧 RUN 层可能依赖宿主组、错误的附加组或被 umask 削减的 /tmp 权限。
# 更早版本还遗漏 COPY/ADD 目录元数据和完整 tar 校验，不能复用旧指令层。
# 版本 10 及更早的跨阶段 COPY 忽略硬链接共享权限，并遗漏源 PAX xattr。
# 版本 11 及更早可能根据无效 passwd 链接或带哨兵 UID/GID 的基础层生成错误结果。
# 版本 12 及更早的 RUN 可能通过下层符号链接误应用 whiteout 删除。
# 版本 13 及更早遗漏根目录属性与根 opaque，并可将 RUN 的 .wh.* 实体误作删除。
# 版本 14 及更早的路径规则允许双前导斜杠或改写反斜杠，不能复用旧结果。
# 版本 15 及更早会丢失目录扩展属性，且 RUN 默认 umask/cache 挂载权限受宿主影响。
# 版本 16 及更早可把另一 xattr namespace 的普通属性误当删除，并遗漏目录 xattr 删除。
# 版本 17 及更早在层应用前发布缓存，失败指令也可能留下无效链接或权限层。
# 版本 18 及更早会丢失跨阶段目录的硬链接，并将独立本地源错误地合成硬链接。
# 版本 20 排除源输入在生成 COPY/ADD 层期间变化后，仍按旧指纹发布的条目。
# 版本 21 排除旧 URL 文件名解析与 RUN rootfs 链接跳转语义生成的结果。
# 版本 22 排除 Windows 旧文件身份误命中后，依据错误基础 rootfs 生成的后续层。
# 版本 23 排除多阶段 RUN 共用物化目录后，读取前一阶段残留内容生成的层及后续结果。
CACHE_VERSION = 23


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
        self.locks = self.root / "locks"
        if any(is_linked_directory(path) for path in (self.entries, self.layers, self.locks)):
            raise BuildError("Cache entries/layers directories must not be symlinks")
        for path in (self.root, self.entries, self.layers, self.locks):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _entry_lock(self, key):
        """同一键的验证回写与发布共用锁，避免旧校验结果覆盖新的构建结果。"""
        if is_linked_directory(self.locks):
            raise BuildError("Layer cache locks directory must not be a link")
        return file_lock(self.locks / (key + ".lock"), "Layer cache entry")

    def lookup(self, key):
        """返回有效缓存结果；元数据过期、缺失或内容损坏时返回 None。

        默认模式下，文件身份全部匹配时可跳过完整哈希；Windows 额外检查 ChangeTime。
        文件身份变化或 verify=True 时重新核对 DiffID 与 tar 格式。
        这只适用于可信本地缓存，不能抵御同时修改内容和缓存元数据的攻击者。
        """
        key = cache_entry_key(key)
        with self._entry_lock(key):
            return self._lookup_locked(key)

    def _lookup_locked(self, key):
        """持条目锁完成验证及文件身份刷新；忙时由调用方重试。"""
        entry = self.entries / (key + ".json")
        if entry.is_symlink():
            raise BuildError("Instruction cache entry must not be a symlink")
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
            if path.is_symlink() or not path.is_file() or path.stat().st_size != metadata["size"]:
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
        key = cache_entry_key(key)
        with self._entry_lock(key):
            self._store_locked(key, diff_id, layer_path)

    def _store_locked(self, key, diff_id, layer_path):
        """持条目锁发布完整层和元数据；不同缓存键仍可并发写入。"""
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
            if target.is_symlink():
                raise BuildError("Instruction cache layer must not be a symlink")
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
        return file_identity(path)
