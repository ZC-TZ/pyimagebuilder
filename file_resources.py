"""统一接管已取得所有权的文件描述符，避免接管失败时泄漏原始句柄。"""

import os


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
