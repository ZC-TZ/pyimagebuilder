"""发布已验证的交付文件，禁止覆盖同名目标；仅使用 Python 3.7 标准库。"""

import errno
import os
import shutil
import tempfile
from contextlib import contextmanager, ExitStack, nullcontext
from pathlib import Path

from compat import unlink_missing
from path_policy import output_path


BUFFER = 4 * 1024 * 1024
LINK_FALLBACK = {errno.EXDEV, errno.EPERM, errno.EACCES, errno.ENOSYS,
                 errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP}


@contextmanager
def publication_transaction(cas_store=None, reference=None, platform=None, source=None):
    """保护引用与本次创建的交付文件，作用域必须覆盖临时目录退出和收尾操作。

    调用方在排他创建/发布成功后将路径加入返回列表，不能登记尚未取得所有权的路径。
    引用锁从快照持续到退出，防止失败恢复覆盖其他操作；没有 CAS 时只保护交付文件。
    """
    lock = cas_store.ref_lock(reference, platform, source) if cas_store is not None else nullcontext()
    with lock:
        previous = cas_store.snapshot_ref(reference, platform, source) if cas_store is not None else None
        published = []
        try:
            yield published
        except BaseException:
            try:
                # ExitStack 在某个删除失败后仍尝试其余文件；引用恢复也不能被删除异常跳过。
                with ExitStack() as cleanup:
                    for path in published:
                        cleanup.callback(unlink_missing, path)
            finally:
                if cas_store is not None:
                    cas_store.restore_ref(reference, platform, source, previous)
            raise


def _copy_exclusive(source, destination):
    """不支持硬链接时排他复制；关闭句柄后只清理本次创建的失败输出。"""
    created = False
    try:
        with open(source, "rb") as stream, open(destination, "xb") as output:
            created = True
            shutil.copyfileobj(stream, output, BUFFER)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
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
    source, destination = Path(source), output_path(destination)
    try:
        os.link(source, destination)
        return destination
    except OSError as exc:
        if exc.errno not in LINK_FALLBACK:
            raise
    descriptor, pending = tempfile.mkstemp(prefix="publish-", suffix=".part", dir=destination.parent)
    published = False
    try:
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
            published = True
        finally:
            unlink_missing(pending)
    except BaseException:
        # 目标已发布后清理临时副本也可能失败；不能让调用方错过这个已创建的目标。
        if published:
            unlink_missing(destination)
        raise
    return destination
