"""发布已验证的交付文件，禁止覆盖同名目标；仅使用 Python 3.7 标准库。"""

import errno
import os
import shutil
import stat
import tempfile
from contextlib import contextmanager, ExitStack, nullcontext
from contextvars import ContextVar
from pathlib import Path

from compat import unlink_missing
from file_resources import fdopen_owned
from path_policy import output_path


BUFFER = 4 * 1024 * 1024
LINK_FALLBACK = {errno.EXDEV, errno.EPERM, errno.EACCES, errno.ENOSYS,
                 errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP}
_PUBLICATIONS = ContextVar("pyimagebuilder_publications", default=())


def _node_identity(info):
    """标识文件对象；不使用会随写入、硬链接清理而改变的大小、mtime 或 ctime。"""
    if not info.st_ino:
        return None
    return info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)


def _unlink_owned(path, expected):
    """只回收仍匹配登记身份的节点，保留后来替换到同名路径的文件或链接。"""
    path = Path(path)
    try:
        actual = path.lstat()
    except FileNotFoundError:
        return
    if expected is None:
        raise OSError("Cannot safely clean output without a stable file identity: " + str(path))
    if _node_identity(actual) == expected:
        unlink_missing(path)


class _PublishedPaths(list):
    """保留 append/extend 用法，并在登记时保存绝对路径与文件身份。"""
    def __init__(self):
        super().__init__()
        self.identities = {}
        self.observed = {}

    def append(self, path):
        path = Path(os.path.abspath(path))
        if path in self.observed:
            # 发布返回与 append 之间也可能发生替换，不能重新认领同名的新文件。
            identity = self.observed[path]
        else:
            try:
                identity = _node_identity(path.lstat())
            except FileNotFoundError:
                identity = None
        self.identities[path] = identity
        super().append(path)

    def extend(self, paths):
        if paths is self:
            paths = list(paths)
        for path in paths:
            self.append(path)

    def __iadd__(self, paths):
        self.extend(paths)
        return self


def _observe_publication(path, identity):
    """将发布时的实际身份传给同一上下文的内外层事务，不自动扩大回滚范围。"""
    path = Path(os.path.abspath(path))
    for ledger in _PUBLICATIONS.get():
        ledger.observed[path] = identity


@contextmanager
def publication_transaction(cas_store=None, reference=None, platform=None, source=None):
    """保护引用与本次创建的交付文件，作用域必须覆盖临时目录退出和收尾操作。

    调用方在排他创建/发布成功后将路径加入返回列表，不能登记尚未取得所有权的路径。
    引用锁从快照持续到退出，防止失败恢复覆盖其他操作；没有 CAS 时只保护交付文件。
    """
    lock = cas_store.ref_lock(reference, platform, source) if cas_store is not None else nullcontext()
    with lock:
        previous = cas_store.snapshot_ref(reference, platform, source) if cas_store is not None else None
        published = _PublishedPaths()
        token = _PUBLICATIONS.set(_PUBLICATIONS.get() + (published,))
        try:
            try:
                yield published
            except BaseException:
                try:
                    # ExitStack 在某个删除失败后仍尝试其余文件；引用恢复也不能被删除异常跳过。
                    with ExitStack() as cleanup:
                        for path in published:
                            cleanup.callback(_unlink_owned, path, published.identities.get(path))
                finally:
                    if cas_store is not None:
                        cas_store.restore_ref(reference, platform, source, previous)
                raise
        finally:
            _PUBLICATIONS.reset(token)


def _copy_exclusive(source, destination):
    """不支持硬链接时排他复制；关闭句柄后只清理本次创建的失败输出。"""
    created = False
    identity = None
    try:
        with open(source, "rb") as stream, open(destination, "xb") as output:
            created = True
            identity = _node_identity(os.fstat(output.fileno()))
            shutil.copyfileobj(stream, output, BUFFER)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        if created:
            _unlink_owned(destination, identity)
        raise
    return identity


def publish_new_file(source, destination):
    """把私有工作区中的已验证文件发布到尚不存在的目标，保留源文件。

    同文件系统优先通过硬链接原子创建目标，避免 rename 覆盖竞争写入者。
    跨文件系统先复制到目标磁盘的独占临时文件，再尝试硬链接发布。
    文件系统不支持硬链接时使用 xb 复制，仍禁止覆盖，但复制期间目标可见。
    此函数不提供多文件事务；调用方负责回滚自己已成功发布的其他产物。
    """
    source, destination = Path(source), output_path(destination)
    source_identity = _node_identity(source.lstat())
    try:
        os.link(source, destination)
    except OSError as exc:
        if exc.errno not in LINK_FALLBACK:
            raise
    else:
        try:
            _observe_publication(destination, source_identity)
        except BaseException:
            _unlink_owned(destination, source_identity)
            raise
        return destination
    descriptor, pending = tempfile.mkstemp(prefix="publish-", suffix=".part", dir=destination.parent)
    published = False
    pending_identity = identity = None
    try:
        try:
            try:
                pending_identity = _node_identity(os.fstat(descriptor))
            except BaseException:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                raise
            with fdopen_owned(descriptor, "wb") as output, open(source, "rb") as stream:
                shutil.copyfileobj(stream, output, BUFFER)
                output.flush()
                os.fsync(output.fileno())
            try:
                os.link(pending, destination)
                identity = pending_identity
            except OSError as exc:
                if exc.errno not in LINK_FALLBACK:
                    raise
                identity = _copy_exclusive(pending, destination)
            published = True
            _observe_publication(destination, identity)
        finally:
            _unlink_owned(pending, pending_identity)
    except BaseException:
        # 目标已发布后清理临时副本也可能失败；不能让调用方错过这个已创建的目标。
        if published:
            _unlink_owned(destination, identity)
        raise
    return destination
