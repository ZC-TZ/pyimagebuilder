"""统一接管已取得所有权的文件描述符，避免接管失败时泄漏原始句柄。"""

import os
import stat


def fdopen_owned(descriptor, *args, **kwargs):
    """将自有描述符交给文件流；失败时关闭描述符并保留原始接管异常。

    仅用于调用方负责关闭的描述符，不能传入借用的 stdin/stdout/stderr。
    成功返回后由调用方关闭文件流（通常使用 with），不能再单独关闭描述符。
    """
    try:
        return os.fdopen(descriptor, *args, **kwargs)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def open_regular_file(path):
    if not stat.S_ISREG(path.lstat().st_mode):
        raise OSError("Artifact is not a regular file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
    stream = fdopen_owned(os.open(str(path), flags), "rb")
    try:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise OSError("Artifact is not a regular file")
        return stream
    except BaseException:
        stream.close()
        raise
