"""直接获取远端 ADD 源，不调用 curl、Git 或 Docker。"""

import hashlib
import http.client
import posixpath
from urllib.parse import unquote, urlsplit
from urllib.request import urlopen

from errors import ArchiveError, BuildError, UnsupportedInstruction
from path_policy import archive_path


MAX_BYTES = 4 * 1024 * 1024 * 1024


def fetch(url, destination, checksum=None, reporter=None):
    """下载 HTTP(S) ADD 源，限制为 4 GiB，并可核对原始字节的 SHA-256。

    返回 (URL 中的文件名, 实际摘要)，内容保存到 destination；目标必须尚不存在。
    响应提供 Content-Length 时核对实际长度；未提供时只能依靠大小上限和可选 checksum。
    失败可能留下已写入文件，由调用方的临时工作区负责清理。
    """
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise UnsupportedInstruction("Remote ADD requires an HTTP(S) URL without embedded credentials")
    # URL 总是 POSIX 路径；先选末尾组件再解码，不能把编码后的分隔符改成目录跳转。
    try:
        filename = unquote(posixpath.basename(parsed.path), errors="strict")
        if "/" in filename or archive_path(filename, allow_root=False) != filename:
            raise BuildError("Remote ADD URL needs one safe filename in its path")
    except (UnicodeError, BuildError, ArchiveError) as exc:
        raise BuildError("Invalid Remote ADD URL filename") from exc
    digest = hashlib.sha256()
    count = 0
    try:
        with urlopen(url, timeout=60) as response, open(destination, "xb") as output:
            final = urlsplit(response.geturl())
            if final.scheme not in ("http", "https") or final.username or final.password:
                raise BuildError("Remote ADD redirected outside HTTP(S)")
            length = response.headers.get("Content-Length", "")
            total = int(length) if length.isdecimal() else None
            if total is not None and total > MAX_BYTES:
                raise BuildError("Remote ADD exceeds the 4 GiB limit")
            while True:
                block = response.read(4 * 1024 * 1024)
                if not block:
                    break
                count += len(block)
                if count > MAX_BYTES:
                    raise BuildError("Remote ADD exceeds the 4 GiB limit")
                digest.update(block)
                output.write(block)
                if reporter is not None:
                    reporter.progress(count, total, "Downloading ADD source")
            # 分块读取到 EOF 不一定抛出 IncompleteRead，必须另外核对声明长度，
            # 否则没有 --checksum 的 ADD 会把截断下载当作合法源文件。
            if total is not None and count != total:
                raise BuildError("Remote ADD response length mismatch: expected {}, got {}".format(total, count))
    except (OSError, http.client.HTTPException) as exc:
        raise BuildError("Remote ADD failed: " + str(exc)) from exc
    actual = "sha256:" + digest.hexdigest()
    if checksum is not None and actual.lower() != checksum.lower():
        raise BuildError("Remote ADD checksum mismatch: expected {}, got {}".format(checksum, actual))
    return filename, actual
