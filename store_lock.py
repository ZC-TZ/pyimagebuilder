"""使用 Python 3.7 标准库保护镜像库更新，兼顾线程和独立进程。"""

import errno
import os
import threading
from contextlib import contextmanager
from pathlib import Path

from errors import BuildError


_guard = threading.Lock()
_locks = {}
_active = threading.local()


def _lock_file(stream):
    """尝试立即取得内核文件锁；仅将锁冲突转为可重试的构建错误。"""
    stream.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EAGAIN):
            raise BuildError("Image reference is busy; retry after the other operation completes") from exc
        raise


def _unlock_file(stream):
    stream.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


@contextmanager
def file_lock(path):
    """对同一锁路径互斥更新；忙时立即报错，同线程的嵌套调用可重入。

    线程锁弥补 POSIX 文件锁的进程内语义差异；内核锁保护独立 CLI 进程。
    锁文件永久保留，不能在释放时删除，否则新旧 inode 上可能出现两把有效锁。
    进程退出时内核自动释放锁，文件仍存在不代表引用处于忙状态。
    """
    path = Path(path)
    key = os.path.normcase(str(path.absolute()))
    with _guard:
        local_lock = _locks.setdefault(key, threading.RLock())
    if not local_lock.acquire(blocking=False):
        raise BuildError("Image reference is busy; retry after the other operation completes")
    active = getattr(_active, "paths", None)
    if active is None:
        active = _active.paths = set()
    if key in active:
        try:
            yield
        finally:
            local_lock.release()
        return
    stream = None
    locked = False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            raise BuildError("Image store lock path must not be a symlink")
        stream = open(path, "a+b")
        if os.fstat(stream.fileno()).st_size == 0:
            stream.write(b"\0")
            stream.flush()
        _lock_file(stream)
        locked = True
        active.add(key)
        yield
    finally:
        active.discard(key)
        try:
            if locked:
                _unlock_file(stream)
        finally:
            try:
                if stream is not None:
                    stream.close()
            finally:
                local_lock.release()
