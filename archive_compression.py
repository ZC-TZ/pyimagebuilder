"""复用 gzip 流式写入与回读校验；不重写 tar、配置 JSON 或镜像层。"""

import gzip
import hashlib
import os

from errors import BuildError

BUFFER = 4 * 1024 * 1024


class _DigestWriter:
    """对实际写出的压缩字节求摘要，避免为最终 SHA-256 再读一遍大文件。"""
    def __init__(self, raw):
        self.raw = raw
        self.digest = hashlib.sha256()

    def write(self, data):
        written = self.raw.write(data)
        if written != len(data):
            raise OSError("Short write while compressing archive")
        self.digest.update(data)
        return written

    def flush(self):
        self.raw.flush()


def gzip_stream(stream, output, size, level, reporter=None):
    """压缩指定长度的输入，并从 gzip 重读至 EOF 核对 CRC、长度和原始摘要。

    调用方负责输入身份检查、隔离输出与发布事务。固定头中的时间和文件名，
    同一输入、级别和压缩实现得到稳定结果；不承诺跨 zlib 版本字节一致。
    """
    original = hashlib.sha256()
    remaining = size
    with output.open("xb") as raw:
        target = _DigestWriter(raw)
        with gzip.GzipFile(filename="", mode="wb", compresslevel=level, fileobj=target, mtime=0) as encoded:
            while remaining:
                block = stream.read(min(BUFFER, remaining))
                if not block:
                    raise BuildError("Compression input was truncated")
                original.update(block)
                encoded.write(block)
                remaining -= len(block)
                if reporter is not None:
                    reporter.progress(size - remaining, size, "Compressing image archive")
            if stream.read(1):
                raise BuildError("Compression input grew during read")
        raw.flush()
        os.fsync(raw.fileno())
        compressed_bytes = raw.tell()
        compressed_digest = target.digest.hexdigest()
    if reporter is not None:
        reporter.progress(0, size, "Verifying gzip archive", force=True)
    decoded, count = hashlib.sha256(), 0
    with gzip.open(output, "rb") as verified:
        for block in iter(lambda: verified.read(BUFFER), b""):
            count += len(block)
            if count > size:
                raise BuildError("Compressed image verification failed")
            decoded.update(block)
            if reporter is not None:
                reporter.progress(count, size, "Verifying gzip archive")
    if count != size or decoded.digest() != original.digest():
        raise BuildError("Compressed image verification failed")
    return {"source_sha256": "sha256:" + original.hexdigest(),
            "uncompressed_archive_bytes": size, "after_bytes": compressed_bytes,
            "sha256": "sha256:" + compressed_digest}
