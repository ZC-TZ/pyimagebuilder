"""发布已验证的交付文件，禁止覆盖同名目标；仅使用 Python 3.7 标准库。"""

import errno
import os
import shutil
import tempfile
from pathlib import Path

from compat import unlink_missing


BUFFER = 4 * 1024 * 1024
LINK_FALLBACK = {errno.EXDEV, errno.EPERM, errno.EACCES, errno.ENOSYS,
                 errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP}


def _copy_exclusive(source, destination):
    """不支持硬链接时排他复制；关闭句柄后只清理本次创建的失败输出。"""
    created = False
    try:
        with open(source, "rb") as stream, open(destination, "xb") as output:
            created = True
            shutil.copyfileobj(stream, output, BUFFER)
            output.flush()
            os.fsync(output.fileno())
    except Exception:
        if created:
            unlink_missing(destination)
        raise


def publish_new_file(source, destination):
    """把私有工作区中的已验证文件发布到尚不存在的目标，保留源文件。

    同文件系统优先通过硬链接原子创建目标，避免 rename 覆盖竞争写入者。
    跨文件系统先复制到目标磁盘的独占临时文件，再尝试硬链接发布。
    文件系统不支持硬链接时使用 xb 复制，仍禁止覆盖，但复制期间目标可见。
    此函数不提供多文件事务；调用方负责回滚自己已成功发布的其他产物。
    """
    source, destination = Path(source), Path(destination)
    try:
        os.link(source, destination)
        return destination
    except OSError as exc:
        if exc.errno not in LINK_FALLBACK:
            raise
    descriptor, pending = tempfile.mkstemp(prefix="publish-", suffix=".part", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "wb") as output, open(source, "rb") as stream:
            shutil.copyfileobj(stream, output, BUFFER)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(pending, destination)
        except OSError as exc:
            if exc.errno not in LINK_FALLBACK:
                raise
            _copy_exclusive(pending, destination)
    finally:
        unlink_missing(pending)
    return destination
