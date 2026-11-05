"""为可信本地缓存统一记录文件身份，补齐 Windows 创建时间与变更时间的差异。"""

import hashlib
import os
from functools import lru_cache
from pathlib import Path


def stat_identity(data):
    """保留原有 stat 字段顺序，句柄与路径的绑定检查只比较前四项。"""
    return [data.st_dev, data.st_ino, data.st_size, data.st_mtime_ns, data.st_ctime_ns]


@lru_cache(maxsize=1)
def _windows_basic_info_api():
    """延迟加载标准库 ctypes 的 Windows API，不给 Linux 或交付包增加依赖。"""
    import ctypes
    from ctypes import wintypes

    class FileBasicInfo(ctypes.Structure):
        _fields_ = [(name, ctypes.c_longlong) for name in (
            "CreationTime", "LastAccessTime", "LastWriteTime", "ChangeTime")]
        _fields_.append(("FileAttributes", wintypes.DWORD))

    function = ctypes.WinDLL("kernel32", use_last_error=True).GetFileInformationByHandleEx
    function.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    function.restype = wintypes.BOOL
    return function, FileBasicInfo


def _windows_change_time(stream):
    """获取文件实际 ChangeTime；驱动或 API 不支持时交给调用方做内容校验。"""
    try:
        import ctypes
        import msvcrt
        function, info_type = _windows_basic_info_api()
        info = info_type()
        if function(msvcrt.get_osfhandle(stream.fileno()), 0,
                    ctypes.byref(info), ctypes.sizeof(info)) and info.ChangeTime > 0:
            return info.ChangeTime
    except (AttributeError, ImportError, OSError):
        pass
    return None


def file_identity(path, *, content=True):
    """构造缓存身份；Windows 不能仅凭创建时间和可恢复的 mtime 判定未变化。

    Windows 同一时间粒度内的写入可能得到相同 ChangeTime。持久身份使用内容摘要；
    已在读取前后核对内容的调用方可用 ChangeTime 做过程中的轻量检查。
    """
    path = Path(path)
    identity = stat_identity(path.stat())
    if os.name != "nt":
        return identity
    with open(path, "rb") as stream:
        if stat_identity(os.fstat(stream.fileno()))[:4] != identity[:4]:
            raise OSError("File changed while inspecting cache identity: " + str(path))
        changed = _windows_change_time(stream)
        if content or changed is None:
            digest = hashlib.sha256()
            for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                digest.update(block)
            token = "sha256:" + digest.hexdigest()
        else:
            token = "windows-change:" + str(changed)
        if (stat_identity(os.fstat(stream.fileno()))[:4] != identity[:4] or
                _windows_change_time(stream) != changed):
            raise OSError("File changed while inspecting cache identity: " + str(path))
    if stat_identity(path.stat())[:4] != identity[:4]:
        raise OSError("File changed while inspecting cache identity: " + str(path))
    return identity + [token]
