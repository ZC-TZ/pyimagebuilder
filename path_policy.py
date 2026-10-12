"""区分镜像、归档与宿主路径；先规范化，再检查实际消费端的边界。"""

import os
import posixpath
import re
from pathlib import Path

from errors import ArchiveError, BuildError


def container_path(value, workdir="/", non_root=False):
    """解析 Linux 镜像路径；双前导斜杠没有独立命名空间，统一为单斜杠。

    容器路径允许 .. 的正常语义；敏感挂载应在规范化后要求非根路径。
    当前层生成器不支持反斜杠文件名，必须报错，不能将其改写为目录分隔符。
    """
    if (not isinstance(value, str) or not value or "\0" in value or "\\" in value or
            not isinstance(workdir, str) or not workdir.startswith("/") or
            "\0" in workdir or "\\" in workdir):
        raise BuildError("Invalid container path or WORKDIR")
    joined = value if value.startswith("/") else posixpath.join(workdir, value)
    result = "/" + posixpath.normpath(joined).lstrip("/")
    if non_root and result == "/":
        raise BuildError("Container path must not resolve to root")
    return result


def archive_path(name, allow_root=True):
    """规范化 tar/ZIP 的相对成员名，拒绝父跳转而不是把它折叠掉。

    a//b 与 a/./b 必须索引到同一名字，供调用方检测重复。Linux 合法的冒号
    在逻辑镜像索引中保留；真正写入 Windows 时另做宿主组件校验。
    """
    if (not isinstance(name, str) or "\0" in name or "\\" in name or
            name.startswith("/") or ".." in name.split("/")):
        raise ArchiveError("Unsafe archive path: " + repr(name))
    result = "/".join(part for part in name.split("/") if part not in ("", "."))
    if not result and not allow_root:
        raise ArchiveError("Empty archive path")
    return result


def host_components(parts, windows=None, wildcards=False):
    """在拼接宿主路径前拒绝 Windows 盘符重置、ADS、设备名与文件名别名。

    仅用于相对组件；不把 Linux 镜像的合法名称误当 Windows 文件名。
    wildcards 只为构建上下文的 glob 模式开放 * 和 ?，其匹配结果仍需检查。
    """
    if windows is None:
        windows = os.name == "nt"
    for part in parts:
        if part in ("", ".", "..") or "/" in part or "\\" in part or "\0" in part:
            raise BuildError("Unsafe host path component: " + repr(part))
        if not windows:
            continue
        illegal = '<>:"|' + ("" if wildcards else "?*")
        stem = part.split(".", 1)[0].rstrip(" ").upper()
        reserved = stem in ("CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$") or bool(
            re.fullmatch(r"(?:COM|LPT)[1-9¹²³]", stem))
        if reserved or part.endswith((".", " ")) or any(
                char in illegal or ord(char) < 32 for char in part):
            raise BuildError("Unsupported Windows path component: " + repr(part))


def output_path(value):
    """解析交付输出，但拒绝最终节点的链接，包括指向不存在文件的链接。

    输入路径可以显式指向链接目标；新输出不能先 resolve 掉链接再检查是否存在。
    最终发布仍需排他创建；此处的预检不能替代发布时的竞争保护。
    """
    raw = Path(value)
    if "\0" in str(raw) or raw.is_symlink():
        raise BuildError("Output path must not contain NUL or be a symlink")
    host_components([part for part in raw.parts if part not in (raw.anchor, ".", "..")])
    return raw.resolve()


def cache_entry_key(key):
    """缓存键只能是文件名原子，不能让外部 API 的键改写 entries 边界。"""
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", key):
        raise BuildError("Invalid instruction cache key")
    return key


def host_path_aliases(names, windows=None):
    """预检整批相对名字，包括隐式父目录，拒绝 Windows 大小写别名。"""
    if windows is None:
        windows = os.name == "nt"
    spellings = {}
    for name in names:
        parts = tuple(name.split("/")) if name else ()
        host_components(parts, windows=windows)
        if windows:
            for end in range(1, len(parts) + 1):
                prefix = parts[:end]
                key = tuple(part.casefold() for part in prefix)
                if key in spellings and spellings[key] != prefix:
                    raise BuildError("Windows path alias: " + name)
                spellings[key] = prefix


def overlay_path(value):
    """OverlayFS 路径被嵌入 mount 选项，不允许路径字符变成选项/多层分隔符。"""
    if any(char in str(value) for char in (",", ":", "\\", "\0", "\n", "\r")):
        raise BuildError("Unsupported separator in OverlayFS path: " + str(value))
    return value
