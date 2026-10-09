"""集中提供 Python 3.7 标准库兼容函数。

将版本差异收敛到此处，避免归档校验和清理逻辑散布版本判断；
辅助函数尽量保留新版 API 的可观察行为。
"""

from pathlib import Path


def remove_suffix(value, suffix):
    """仅移除完全匹配的末尾后缀，兼容 Python 3.9 的 str.removesuffix。"""
    if suffix and value.endswith(suffix):
        return value[:-len(suffix)]
    return value


def is_relative_to(path, parent):
    """检查路径的词法包含关系，兼容 Python 3.9 的 Path.is_relative_to。

    涉及目录边界的调用方应先 resolve 两个路径，避免符号链接绕过检查。
    """
    try:
        Path(path).relative_to(parent)
    except ValueError:
        return False
    return True


def unlink_missing(path):
    """删除路径时仅忽略不存在的情况，兼容 Python 3.8 的 missing_ok=True。"""
    try:
        Path(path).unlink()
    except FileNotFoundError:
        pass


def is_linked_directory(path):
    """在 Python 3.7 及以上识别缓存目录符号链接和 Windows junction。"""
    path = Path(path)
    return path.is_symlink() or (path.exists() and path.resolve() != path.absolute())
