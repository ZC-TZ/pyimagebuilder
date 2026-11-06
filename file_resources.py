"""统一接管已取得所有权的文件描述符，避免接管失败时泄漏原始句柄。"""

import os
import stat
from contextlib import contextmanager


@contextmanager
def cleanup_on_exit(cleanup):
    """始终尝试收尾；已有失败时保留原异常，正常退出时报告收尾失败。

    cleanup 只能释放调用方拥有的资源。不能用 sys.exc_info 判断当前操作是否失败，
    因为调用方可能在处理其他异常时正常进入此作用域。
    """
    try:
        yield
    except BaseException:
        try:
            cleanup()
        except BaseException:
            pass  # 校验失败、读写错误和 KeyboardInterrupt 的优先级高于收尾错误。
        raise
    else:
        cleanup()


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


def open_regular_file(path, *, writable=False):
    """按普通文件接管句柄；writable 供独占临时文件在发布前刷盘。"""
    if not stat.S_ISREG(path.lstat().st_mode):
        raise OSError("Artifact is not a regular file")
    flags = os.O_RDWR if writable else os.O_RDONLY
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
    stream = fdopen_owned(os.open(str(path), flags), "r+b" if writable else "rb")
    try:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise OSError("Artifact is not a regular file")
        return stream
    except BaseException:
        stream.close()
        raise
