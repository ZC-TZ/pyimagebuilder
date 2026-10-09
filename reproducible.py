"""规范化时间戳和 tar 头，支持可重复的镜像输出。"""

import datetime
import re
import tarfile
from pathlib import Path

from errors import BuildError


MAX_EPOCH = 253402300799  # 9999-12-31T23:59:59Z


def parse_epoch(value):
    """校验非负 SOURCE_DATE_EPOCH，限制在可表示的 UTC 日期范围内。"""
    raw = str(value)
    if isinstance(value, bool) or re.fullmatch(r"[0-9]+", raw) is None:
        raise BuildError("SOURCE_DATE_EPOCH must be a nonnegative integer")
    if len(raw.lstrip("0")) > 12:
        raise BuildError("SOURCE_DATE_EPOCH exceeds year 9999")
    epoch = int(raw)
    if epoch > MAX_EPOCH:
        raise BuildError("SOURCE_DATE_EPOCH exceeds year 9999")
    return epoch


def timestamp(epoch):
    """把固定 epoch 转为 OCI 兼容的 UTC 时间字符串。"""
    moment = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)
    return (moment + datetime.timedelta(seconds=epoch)).isoformat().replace("+00:00", "Z")


def add_file(archive, name, source, epoch, progress=None):
    """把已有 blob 加入归档，避免引入宿主文件的 inode 元数据。"""
    source = Path(source)
    info = tarfile.TarInfo(name)
    info.mode = 0o644
    info.uid = info.gid = 0
    info.mtime = epoch
    info.size = source.stat().st_size
    with open(source, "rb") as stream:
        if progress is None:
            archive.addfile(info, stream)
        else:
            from progress.stream import CountingReader
            archive.addfile(info, CountingReader(stream, progress, info.size, name))
