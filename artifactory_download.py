#!/usr/bin/env python3
"""下载 Artifactory Registry V2 制品并转换为 Docker 可加载归档。

支持的页面形式：
  /ui/native/<repo>/<path>/
  /ui/repos/tree/General/<repo>/<path>
  /artifactory/webapp/#/artifacts/browse/tree/General/<repo>/<path>
  <registry-host>/<repo>:<tag>（Docker 镜像引用）

实际下载地址会自动转换为：/artifactory/<repo>/<path>/
下载完成后重构为 docker save 兼容布局，可通过 docker load -i 导入。
"""

from __future__ import annotations

import argparse
import base64
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
import gzip
import hashlib
import html.parser
import json
import os
import re
import shutil
import ssl
import sys
import tarfile
import tempfile
import threading
import time
import zlib
from pathlib import Path, PurePosixPath
from typing import List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit
from urllib.request import (Request, build_opener, HTTPBasicAuthHandler,
                            HTTPPasswordMgrWithDefaultRealm, HTTPRedirectHandler, HTTPSHandler)

from compat import unlink_missing
from file_publish import publish_new_file


class DirectoryLinks(html.parser.HTMLParser):
    """仅提取 Artifactory HTML 目录索引中的 href。"""

    def __init__(self) -> None:
        super().__init__()
        self.links: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        """只收集 a 标签的 href；后续路径校验决定是否允许下载。"""
        if tag.lower() == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)


class AdaptiveLimiter(object):
    """可在下载过程中平滑调整的并发闸门。"""

    def __init__(self, limit: int) -> None:
        self._condition = threading.Condition()
        self._limit = limit
        self._active = 0

    def acquire(self) -> None:
        """等待并发名额，避免目录包含大量层时压垮内网服务。"""
        with self._condition:
            while self._active >= self._limit:
                self._condition.wait()
            self._active += 1

    def release(self) -> None:
        """释放下载名额并唤醒等待的线程。"""
        with self._condition:
            self._active -= 1
            self._condition.notify_all()

    def set_limit(self, limit: int) -> None:
        """根据传输反馈调整后续请求的并发上限。"""
        with self._condition:
            self._limit = limit
            self._condition.notify_all()

    def snapshot(self) -> Tuple[int, int]:
        """在同一把锁下读取活跃数与并发上限。"""
        with self._condition:
            return self._active, self._limit


class TransferStats(object):
    """供下载线程更新、主线程读取的传输统计。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._bytes = 0

    def add(self, size: int) -> None:
        """累计已读取字节，供进度线程汇总。"""
        with self._lock:
            self._bytes += size

    def snapshot(self) -> int:
        """读取线程安全的已传输字节总数。"""
        with self._lock:
            return self._bytes


class ProgressDisplay(object):
    """TTY 中单行刷新；重定向到日志时降低打印频率。"""

    def __init__(self) -> None:
        self.is_tty = sys.stdout.isatty()
        self._lock = threading.Lock()
        self._last_width = 0

    def status(self, message: str) -> None:
        """刷新当前状态；终端原位更新，日志环境按行输出。"""
        with self._lock:
            if self.is_tty:
                padding = " " * max(0, self._last_width - len(message))
                sys.stdout.write("\r" + message + padding)
                sys.stdout.flush()
                self._last_width = len(message)
            else:
                print(message, flush=True)

    def log(self, message: str) -> None:
        """输出一条不会与终端状态行重叠的日志。"""
        with self._lock:
            if self.is_tty and self._last_width:
                sys.stdout.write("\r" + " " * self._last_width + "\r")
            print(message, flush=True)
            self._last_width = 0

    def finish_line(self) -> None:
        """结束终端上的临时状态行。"""
        with self._lock:
            if self.is_tty and self._last_width:
                sys.stdout.write("\n")
                sys.stdout.flush()
            self._last_width = 0


class CountingReader(object):
    """包装文件对象，在 tarfile 读取时累计打包进度。"""

    def __init__(self, raw, stats: TransferStats) -> None:
        self.raw = raw
        self.stats = stats

    def read(self, size: int = -1):
        """在调用方读取文件时同步累计打包字节。"""
        data = self.raw.read(size)
        if data:
            self.stats.add(len(data))
        return data


class DigestReader(object):
    """边读取边计算压缩 blob 摘要，避免 gzip 解压后再完整扫描源文件。"""

    def __init__(self, raw):
        self.raw = raw
        self.digest = hashlib.sha256()

    def read(self, size=-1):
        """在 gzip 解压器读取压缩输入时同步计算原始 blob 摘要。"""
        data = self.raw.read(size)
        if data:
            self.digest.update(data)
        return data

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def artifact_directory_url(page_url: str) -> str:
    """把 UI 页面 URL 或已是 artifact URL 的地址规范为制品目录 URL。"""
    raw_input = page_url.strip()
    # Docker 镜像引用通常没有协议，例如 host/repository:tag；仓库截图使用 HTTP。
    if "://" not in raw_input:
        raw_input = "http://" + raw_input
    parsed = urlsplit(raw_input)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("URL 必须以 http:// 或 https:// 开头")

    path = unquote(parsed.path)
    fragment = unquote(parsed.fragment)
    native = re.match(r"^/ui/native/(.+)$", path)
    tree = re.match(r"^/ui/repos/tree/General/(.+)$", path)
    webapp_tree = re.match(r"^/?artifacts/browse/tree/General/(.+)$", fragment)
    image_path = path.lstrip("/")
    # 最后一个冒号之后是 tag；主机端口即使含冒号，也位于 netloc 中，不会混淆。
    image_match = re.match(r"^(.+):([^/:]+)$", image_path)
    if native:
        artifact_path = "/artifactory/" + native.group(1)
    elif tree:
        artifact_path = "/artifactory/" + tree.group(1)
    elif path.rstrip("/") == "/artifactory/webapp" and webapp_tree:
        artifact_path = "/artifactory/" + webapp_tree.group(1)
    elif image_match and not path.startswith("/artifactory/"):
        repository_path, tag = image_match.groups()
        artifact_path = "/artifactory/{}/{}".format(repository_path, tag)
    elif path.startswith("/artifactory/"):
        artifact_path = path
    else:
        raise ValueError("不支持的 URL；应为 /ui/native/、/ui/repos/tree/General/、"
                         "/artifactory/webapp/#/artifacts/browse/tree/General/、"
                         "Docker 镜像引用或 /artifactory/ 地址")

    # 路径中的空格等字符必须编码；末尾斜杠用于请求目录索引。
    artifact_path = quote(artifact_path, safe="/%:@")
    if not artifact_path.endswith("/"):
        artifact_path += "/"
    return urlunsplit((parsed.scheme, parsed.netloc, artifact_path, "", ""))


class SameOriginRedirect(HTTPRedirectHandler):
    """限制鉴权请求的重定向目标，防止凭据随跳转发送到其他来源。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """拒绝跨源重定向，防止 Basic 凭据被带到其他主机。"""
        old = urlsplit(req.full_url)
        new = urlsplit(newurl)
        if (new.scheme.lower(), new.netloc.lower()) != (old.scheme.lower(), old.netloc.lower()):
            raise HTTPError(req.full_url, code, "Cross-origin redirect refused", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def make_opener(base_url: str, username: Optional[str] = None,
                password: Optional[str] = None, ca_file: Optional[Path] = None):
    """创建可选 Basic 鉴权的下载器，并限制凭据只发送到已配置的 Artifactory 源。"""
    if (username is None) != (password is None):
        raise ValueError("Username and password must be provided together")
    handlers = [SameOriginRedirect(),
                HTTPSHandler(context=ssl.create_default_context(cafile=str(ca_file) if ca_file else None))]
    if username is not None:
        manager = HTTPPasswordMgrWithDefaultRealm()
        manager.add_password(None, base_url, username, password)
        handlers.append(HTTPBasicAuthHandler(manager))
    opener = build_opener(*handlers)
    opener.artifactory_origin = (urlsplit(base_url).scheme.lower(), urlsplit(base_url).netloc.lower())
    opener.artifactory_authorization = ("Basic " + base64.b64encode(
        (username + ":" + password).encode("utf-8")).decode("ascii")) if username is not None else None
    return opener


def request(opener, url: str, method: str = "GET", extra_headers=None, timeout: int = 60):
    """向同源 Artifactory 发起请求；保持原始编码以便按服务端摘要校验。"""
    origin = (urlsplit(url).scheme.lower(), urlsplit(url).netloc.lower())
    if origin != opener.artifactory_origin:
        raise ValueError("Request target differs from configured Artifactory origin")
    headers = {
        "User-Agent": "artifactory-tar-downloader/1.1",
        "Accept-Encoding": "identity",
    }
    if opener.artifactory_authorization is not None:
        headers["Authorization"] = opener.artifactory_authorization
    if extra_headers:
        headers.update(extra_headers)
    return opener.open(Request(url, headers=headers, method=method), timeout=timeout)


def safe_relative_name(href: str) -> Optional[str]:
    """拒绝父目录、绝对路径和目录链接，避免写出临时目录。"""
    parsed = urlsplit(href)
    if parsed.scheme or parsed.netloc or href.endswith("/"):
        return None
    name = unquote(parsed.path)
    pure = PurePosixPath(name)
    if (not pure.parts or "\\" in name or ":" in name or
            any(ord(char) < 32 for char in name) or
            pure.is_absolute() or ".." in pure.parts):
        return None
    return str(pure)


def list_files(opener, directory_url: str) -> List[str]:
    """从制品目录列出安全的相对文件名，拒绝越界链接。"""
    with request(opener, directory_url) as response:
        content_type = response.headers.get_content_type()
        body = response.read().decode(response.headers.get_content_charset() or "utf-8", errors="replace")
    if content_type not in {"text/html", "application/xhtml+xml"} and "<a" not in body.lower():
        raise RuntimeError("服务器未返回目录索引；请确认输入的是制品目录页面，而非单个文件")
    parser = DirectoryLinks()
    parser.feed(body)
    files = []
    for href in parser.links:
        name = safe_relative_name(href)
        if name:
            files.append(name)
    files = sorted(set(files))
    if not files:
        raise RuntimeError("目录中没有可下载的文件，或账号没有读取权限")
    return files


def probe_file_size(opener, directory_url: str, name: str) -> Optional[int]:
    """读取单个文件大小；服务器不支持 HEAD 时返回 None。"""
    url = urljoin(directory_url, quote(name, safe="/%:@"))
    try:
        with request(opener, url, method="HEAD", timeout=5) as response:
            value = response.headers.get("Content-Length")
            if value is not None and int(value) >= 0:
                return int(value)
    except (HTTPError, URLError, ValueError, OSError):
        return None
    return None


def probe_file_sizes(opener, directory_url: str, files: List[str]) -> dict:
    """并发探测大小，为最大文件优先调度和精确进度提供数据。"""
    sizes = {}
    workers = min(12, len(files))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_names = {
            executor.submit(probe_file_size, opener, directory_url, name): name
            for name in files
        }
        for future in as_completed(future_names):
            size = future.result()
            if size is not None:
                sizes[future_names[future]] = size
    return sizes


def format_size(size: float) -> str:
    """把字节数格式化为下载进度中的可读大小。"""
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    value = float(max(0, size))
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return "{:.1f} {}".format(value, unit)
        value /= 1024
    return "{:.1f} TiB".format(value)


def format_duration(seconds: float) -> str:
    """把耗时格式化为进度展示用的简短文本。"""
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return "{:d}:{:02d}:{:02d}".format(hours, minutes, seconds)
    return "{:02d}:{:02d}".format(minutes, seconds)


def image_reference_from_directory(directory_url: str) -> str:
    """从 Artifactory 制品目录还原 registry/repository:tag。"""
    parsed = urlsplit(directory_url)
    path = unquote(parsed.path)
    prefix = "/artifactory/"
    if not path.startswith(prefix):
        raise ValueError("无法从制品目录推断 Docker 镜像名称")
    parts = [part for part in path[len(prefix):].strip("/").split("/") if part]
    if len(parts) < 2:
        raise ValueError("制品目录缺少仓库路径或镜像 tag")
    repository = "/".join(parts[:-1])
    tag = parts[-1]
    return "{}/{}:{}".format(parsed.netloc, repository, tag)


def split_image_reference(image_reference: str) -> Tuple[str, str]:
    """拆分镜像名和 tag，同时避免把 registry 端口误认为分隔符。"""
    colon = image_reference.rfind(":")
    slash = image_reference.rfind("/")
    if colon <= slash or colon == len(image_reference) - 1:
        raise ValueError("Docker 镜像引用缺少 tag：{}".format(image_reference))
    return image_reference[:colon], image_reference[colon + 1:]


def parse_sha256_digest(digest: str, field_name: str) -> str:
    """验证 Registry digest，并返回不含算法前缀的十六进制值。"""
    match = re.match(r"^sha256:([0-9a-fA-F]{64})$", str(digest))
    if not match:
        raise RuntimeError("{} 不是受支持的 sha256 digest：{}".format(field_name, digest))
    return match.group(1).lower()


def sha256_file(path: Path) -> str:
    """分块计算下载文件的 SHA-256，避免把未经校验的 blob 写入镜像。"""
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        while True:
            block = source.read(4 * 1024 * 1024)
            if not block:
                break
            hasher.update(block)
    return hasher.hexdigest()


def find_blob(root: Path, digest: str, field_name: str) -> Path:
    """把 Registry digest 映射到 Artifactory 的 sha256__<hex> 文件。"""
    digest_hex = parse_sha256_digest(digest, field_name)
    candidates = [
        "sha256__" + digest_hex,
        "sha256_" + digest_hex,
        digest_hex,
    ]
    by_name = {path.name: path for path in root.rglob("*") if path.is_file()}
    for name in candidates:
        if name in by_name:
            return by_name[name]
    raise RuntimeError("找不到 {} 对应的制品文件（sha256__{}）".format(field_name, digest_hex))


def load_json_object(path: Path, description: str) -> dict:
    """读取 Registry 元数据 JSON，并在结构错误时附上文件用途。"""
    try:
        with path.open("r", encoding="utf-8") as source:
            value = json.load(source)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("{} 不是有效 JSON：{}".format(description, exc))
    if not isinstance(value, dict):
        raise RuntimeError("{} 必须是 JSON 对象".format(description))
    return value


def write_json(path: Path, value) -> None:
    """以稳定、紧凑的 JSON 写出待打包镜像的元数据。"""
    with path.open("w", encoding="utf-8", newline="\n") as target:
        json.dump(value, target, ensure_ascii=False, separators=(",", ":"))
        target.write("\n")


def verify_registry_blob(path: Path, digest: str, description: str) -> None:
    """按 Registry descriptor 声明的摘要核对已下载 blob。"""
    expected = parse_sha256_digest(digest, description)
    actual = sha256_file(path)
    if actual != expected:
        raise RuntimeError("{} 摘要校验失败：期望 {}，实际 {}".format(
            description, expected, actual
        ))


def materialize_layer(blob: Path, target: Path, media_type: str,
                      expected_diff_id: str, index: int, total: int,
                      display: ProgressDisplay, expected_blob_digest=None) -> str:
    """将 Registry 传输层解压为 docker save 所需的 layer.tar。"""
    expected_hex = parse_sha256_digest(expected_diff_id, "rootfs.diff_ids[{}]".format(index - 1))
    with blob.open("rb") as source:
        magic = source.read(4)

    if magic == b"\x28\xb5\x2f\xfd" or media_type.endswith("+zstd"):
        raise RuntimeError("第 {} 层使用 zstd 压缩；Python 3.7 标准库无法解压".format(index))
    compressed = magic.startswith(b"\x1f\x8b") or media_type.endswith("+gzip")

    hasher = hashlib.sha256()
    written = 0
    last_report = time.monotonic()
    try:
        with blob.open("rb") as raw, target.open("wb") as destination:
            checked = DigestReader(raw)
            source_stream = gzip.GzipFile(fileobj=checked, mode="rb") if compressed else checked
            with source_stream:
                while True:
                    block = source_stream.read(4 * 1024 * 1024)
                    if not block:
                        break
                    destination.write(block)
                    hasher.update(block)
                    written += len(block)
                    now = time.monotonic()
                    if now - last_report >= (0.5 if display.is_tty else 5.0):
                        display.status("转换镜像层 {}/{} | 已解包 {}".format(
                            index, total, format_size(written)
                        ))
                        last_report = now
            # 压缩内容末尾的剩余字节也属于传输 blob，必须计入 Registry 摘要。
            if compressed:
                for _ in iter(lambda: checked.read(4 * 1024 * 1024), b""):
                    pass
    except (OSError, EOFError, zlib.error) as exc:
        if target.exists():
            target.unlink()
        raise RuntimeError("第 {} 层解压失败：{}".format(index, exc))

    actual_hex = hasher.hexdigest()
    if expected_blob_digest is not None and checked.digest.hexdigest() != parse_sha256_digest(
            expected_blob_digest, "layers[{}].digest".format(index - 1)):
        target.unlink()
        raise RuntimeError("第 {} 层压缩 blob SHA-256 校验失败".format(index))
    if actual_hex != expected_hex:
        target.unlink()
        raise RuntimeError("第 {} 层 diff_id 校验失败：期望 {}，实际 {}".format(
            index, expected_hex, actual_hex
        ))
    if not tarfile.is_tarfile(str(target)):
        target.unlink()
        raise RuntimeError("第 {} 层解压后不是有效 tar".format(index))
    return "sha256:" + actual_hex


def chain_id(parent_chain_id: Optional[str], diff_id: str) -> str:
    """按 Docker chain ID 规则为同一 diff 层在不同父链下生成稳定 ID。"""
    if parent_chain_id is None:
        return diff_id
    value = "{} {}".format(parent_chain_id, diff_id).encode("utf-8")
    return "sha256:" + hashlib.sha256(value).hexdigest()


def convert_registry_to_docker_archive(raw_root: Path, archive_root: Path,
                                       image_reference: str,
                                       display: ProgressDisplay) -> None:
    """把 Registry V2 文件布局重构为 docker save 兼容目录。"""
    manifest_path = raw_root / "manifest.json"
    if not manifest_path.is_file():
        raise RuntimeError("制品目录缺少 Registry manifest.json")
    registry_manifest = load_json_object(manifest_path, "Registry manifest.json")
    if registry_manifest.get("schemaVersion") != 2:
        raise RuntimeError("仅支持 schemaVersion=2 的 Docker/OCI manifest")
    if "manifests" in registry_manifest:
        raise RuntimeError("当前 tag 指向多平台 manifest list；请先选择具体平台镜像")

    config_entry = registry_manifest.get("config")
    layers = registry_manifest.get("layers")
    if not isinstance(config_entry, dict) or not isinstance(config_entry.get("digest"), str):
        raise RuntimeError("Registry manifest 缺少 config.digest")
    if not isinstance(layers, list) or any(not isinstance(layer, dict) for layer in layers):
        raise RuntimeError("Registry manifest 的 layers 格式无效")

    archive_root.mkdir(parents=True, exist_ok=True)
    config_digest = config_entry["digest"]
    config_hex = parse_sha256_digest(config_digest, "config.digest")
    config_blob = find_blob(raw_root, config_digest, "config.digest")
    verify_registry_blob(config_blob, config_digest, "config blob")
    config = load_json_object(config_blob, "镜像 config blob")
    diff_ids = config.get("rootfs", {}).get("diff_ids") if isinstance(config.get("rootfs"), dict) else None
    if not isinstance(diff_ids, list) or len(diff_ids) != len(layers):
        raise RuntimeError("config.rootfs.diff_ids 数量与 manifest.layers 不一致")

    config_name = config_hex + ".json"
    shutil.copyfile(str(config_blob), str(archive_root / config_name))
    layer_paths = []
    parent_chain = None

    for index, (layer, expected_diff_id) in enumerate(zip(layers, diff_ids), 1):
        digest = layer.get("digest")
        if not isinstance(digest, str):
            raise RuntimeError("manifest.layers[{}] 缺少 digest".format(index - 1))
        blob = find_blob(raw_root, digest, "layers[{}].digest".format(index - 1))

        # 先写临时层，得到 diff_id 后才能计算与父层相关的 chain ID。
        temporary_layer = archive_root / (".layer-{}.tar".format(index))
        diff_id = materialize_layer(
            blob, temporary_layer, str(layer.get("mediaType", "")),
            str(expected_diff_id), index, len(layers), display, digest
        )
        current_chain = chain_id(parent_chain, diff_id)
        current_hex = parse_sha256_digest(current_chain, "chain ID")
        layer_directory = archive_root / current_hex
        layer_directory.mkdir(parents=True, exist_ok=True)
        os.replace(str(temporary_layer), str(layer_directory / "layer.tar"))

        # VERSION/json 是旧版 daemon 读取 docker save 包时使用的兼容元数据。
        (layer_directory / "VERSION").write_text("1.0\n", encoding="ascii")
        legacy_metadata = {"id": current_hex}
        if parent_chain is not None:
            legacy_metadata["parent"] = parse_sha256_digest(parent_chain, "parent chain ID")
        write_json(layer_directory / "json", legacy_metadata)
        layer_paths.append("{}/layer.tar".format(current_hex))
        parent_chain = current_chain
        display.status("转换镜像层 {}/{} | 完成".format(index, len(layers)))

    repository, tag = split_image_reference(image_reference)
    docker_manifest = [{
        "Config": config_name,
        "RepoTags": [image_reference],
        "Layers": layer_paths,
    }]
    write_json(archive_root / "manifest.json", docker_manifest)

    # repositories 属于旧格式索引；保留它可兼容仍依赖该文件的 Docker 版本。
    top_layer_hex = parse_sha256_digest(parent_chain, "top chain ID") if parent_chain else config_hex
    write_json(archive_root / "repositories", {repository: {tag: top_layer_hex}})
    display.finish_line()


def pack_files(root: Path, output: Path, display: ProgressDisplay) -> float:
    """写入独占临时 tar，并显示进度；通过公共发布器交付到新目标。"""
    files = sorted(path for path in root.rglob("*") if path.is_file())
    total_size = sum(path.stat().st_size for path in files)
    part_output = None

    stats = TransferStats()
    stop = threading.Event()
    started = time.monotonic()

    def report() -> None:
        while not stop.wait(0.5 if display.is_tty else 5.0):
            copied = stats.snapshot()
            elapsed = max(time.monotonic() - started, 0.001)
            percent = 100.0 * copied / total_size if total_size else 100.0
            display.status("打包 {:5.1f}% | {} / {} | {:.1f} MiB/s".format(
                min(percent, 100.0), format_size(copied), format_size(total_size),
                copied / elapsed / (1024 * 1024)
            ))

    reporter = threading.Thread(target=report, name="tar-progress")
    reporter.daemon = True
    reporter.start()
    try:
        # 固定 output.part 可能属于另一任务，不能先删除或覆盖它。
        descriptor, pending = tempfile.mkstemp(prefix="artifact-pack-", suffix=".part", dir=output.parent)
        part_output = Path(pending)
        with os.fdopen(descriptor, "wb") as sink, tarfile.open(fileobj=sink, mode="w", bufsize=1024 * 1024) as archive:
            for path in files:
                arcname = path.relative_to(root).as_posix()
                tarinfo = archive.gettarinfo(str(path), arcname=arcname)
                with path.open("rb") as source:
                    archive.addfile(tarinfo, CountingReader(source, stats))
        publish_new_file(part_output, output)
    finally:
        stop.set()
        reporter.join()
        if part_output is not None:
            unlink_missing(part_output)

    elapsed = time.monotonic() - started
    display.status("打包 100.0% | {} | 耗时 {}".format(format_size(total_size), format_duration(elapsed)))
    display.finish_line()
    return elapsed


def download_one(opener, directory_url: str, name: str, root: Path,
                 expected_size: Optional[int], limiter: AdaptiveLimiter,
                 stats: TransferStats, display: ProgressDisplay) -> int:
    """下载单个文件，临时中断时最多尝试三次。

    续传前核对 Content-Range，响应忽略 Range 时覆盖重下；范围或响应长度错误时丢弃前缀。
    这里只验证传输范围和大小，镜像 blob 摘要由后续导入或转换过程核对。
    """
    destination = root / Path(*PurePosixPath(name).parts)
    destination.parent.mkdir(parents=True, exist_ok=True)
    url = urljoin(directory_url, quote(name, safe="/%:@"))
    limiter.acquire()
    try:
        for attempt in range(1, 4):
            offset = destination.stat().st_size if destination.exists() else 0
            if destination.is_file() and expected_size is not None and offset == expected_size:
                return offset
            if expected_size is not None and offset >= expected_size and offset != expected_size:
                stats.add(-offset)
                destination.unlink()
                offset = 0
            headers = {"Range": "bytes={}-".format(offset)} if offset else None
            try:
                with request(opener, url, extra_headers=headers) as response:
                    resumed = offset > 0 and response.getcode() == 206
                    # HTTP 206 不保证响应起点与已有前缀一致；必须核对 Content-Range。
                    # 范围不合法时丢弃前缀，下一次请求完整重下，
                    # 避免把错误响应拼成大小正确但内容错误的文件。
                    range_length = None
                    if response.getcode() == 206:
                        match = re.fullmatch(r"bytes ([0-9]+)-([0-9]+)/([0-9]+|\*)",
                                             response.headers.get("Content-Range", ""))
                        valid = match is not None
                        if valid:
                            start, end = int(match[1]), int(match[2])
                            total = None if match[3] == "*" else int(match[3])
                            valid = (start == offset and end >= start and
                                     (total is None or end < total) and
                                     (expected_size is None or total == expected_size))
                        if not valid:
                            stats.add(-offset)
                            if destination.exists():
                                destination.unlink()
                            raise IOError("Invalid Artifactory Content-Range")
                        range_length = end - start + 1
                        if expected_size is None and total is not None:
                            expected_size = total
                    if offset and not resumed:
                        # 服务端忽略 Range，只能覆盖重下；修正已传输统计。
                        stats.add(-offset)
                        offset = 0
                    mode = "ab" if resumed else "wb"
                    with destination.open(mode) as local:
                        received = 0
                        while True:
                            block = response.read(4 * 1024 * 1024)
                            if not block:
                                break
                            local.write(block)
                            stats.add(len(block))
                            received += len(block)
                    length = response.headers.get("Content-Length", "")
                    if ((range_length is not None and received != range_length) or
                            (length.isdecimal() and received != int(length))):
                        stats.add(-destination.stat().st_size)
                        destination.unlink()
                        raise IOError("Artifactory response body length mismatch")
                actual_size = destination.stat().st_size
                if expected_size is not None and actual_size != expected_size:
                    raise IOError("文件大小不匹配：期望 {}，实际 {}".format(expected_size, actual_size))
                return actual_size
            except Exception as exc:
                if attempt >= 3:
                    partial_size = destination.stat().st_size if destination.exists() else 0
                    if partial_size:
                        stats.add(-partial_size)
                        destination.unlink()
                    raise
                display.log("文件 {} 下载中断，2 秒后第 {}/3 次重试：{}".format(
                    name, attempt + 1, exc
                ))
                time.sleep(2)
    finally:
        limiter.release()


def download_and_pack(opener, directory_url: str, files: List[str], output: Path,
                      workers: int, image_reference: str, reporter=None,
                      cas_store=None, target_platform="linux/amd64", cas_only=False) -> None:
    """并发下载 Artifactory 制品，并按所选模式导出归档或导入 CAS。

    cas_only=True 时仅校验并保存传输 blob，不生成 tar，也不在此处解压核对 DiffID；
    首次构建由 CAS open_base 完成解压校验。普通归档路径先转换并验证各层再打包。
    """
    if cas_only and cas_store is None:
        raise RuntimeError("CAS-only download requires a CAS store")
    output.parent.mkdir(parents=True, exist_ok=True)
    if reporter is None:
        display = ProgressDisplay()
    else:
        class EventDisplay:
            is_tty = False
            def log(self, message):
                reporter.log(message)
            def status(self, message):
                reporter.log(message)
            def finish_line(self):
                pass
        display = EventDisplay()
    display.log("正在并发读取文件大小…")
    probe_started = time.monotonic()
    sizes = probe_file_sizes(opener, directory_url, files)
    probe_elapsed = time.monotonic() - probe_started
    if reporter is not None:
        reporter.record_timing("Probe Artifactory files", probe_elapsed)
    exact_total = sum(sizes.values()) if len(sizes) == len(files) else None

    # LPT（最大处理时间优先）可显著减少只剩一个大镜像层的尾部等待。
    ordered_files = sorted(files, key=lambda name: sizes.get(name, -1), reverse=True)
    if exact_total is not None:
        display.log("发现 {} 个文件，共 {}；大小探测耗时 {:.1f} 秒，按大文件优先下载。".format(
            len(files), format_size(exact_total), probe_elapsed
        ))
    else:
        display.log("发现 {} 个文件，其中 {} 个已知大小；按已知大小优先下载。".format(
            len(files), len(sizes)
        ))

    with tempfile.TemporaryDirectory(prefix="artifactory_download_") as temp:
        workspace = Path(temp)
        raw_root = workspace / "registry-v2"
        archive_root = workspace / "docker-archive"
        raw_root.mkdir()
        if workers == 0:
            max_workers = min(12, len(files))
            selected_workers = min(6, len(files))
            adaptive = True
            display.log("开始下载：初始并发 {}，将根据增益自动试探，最大 {}。".format(
                selected_workers, max_workers
            ))
        else:
            max_workers = min(workers, len(files))
            selected_workers = max_workers
            adaptive = False
            display.log("开始下载：固定并发 {}。".format(selected_workers))

        limiter = AdaptiveLimiter(selected_workers)
        stats = TransferStats()
        report_started = time.monotonic()
        last_report = report_started
        last_report_bytes = 0
        smoothed_speed = None
        last_adjust = report_started
        last_adjust_bytes = 0
        probe_previous_limit = None
        probe_baseline_speed = 0.0
        cooldown_until = report_started + 8
        completed = 0
        report_interval = 0.5 if display.is_tty else 5.0

        try:
            # 线程池按最大值创建，真正的网络并发由可动态调整的 limiter 控制。
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                pending = set()
                for name in ordered_files:
                    pending.add(executor.submit(
                        download_one, opener, directory_url, name, raw_root, sizes.get(name),
                        limiter, stats, display
                    ))

                while pending:
                    done, pending = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)
                    for future in done:
                        future.result()
                        completed += 1

                    now = time.monotonic()
                    transferred = stats.snapshot()
                    if now - last_report >= report_interval or not pending:
                        interval = max(now - last_report, 0.001)
                        instant_speed = max(0, transferred - last_report_bytes) / interval
                        if smoothed_speed is None:
                            smoothed_speed = instant_speed
                        else:
                            smoothed_speed = 0.25 * instant_speed + 0.75 * smoothed_speed
                        active, limit = limiter.snapshot()
                        if exact_total is not None:
                            percent = min(100.0, 100.0 * transferred / max(exact_total, 1))
                            remaining_bytes = max(0, exact_total - transferred)
                            eta = format_duration(remaining_bytes / smoothed_speed) if smoothed_speed > 0 else "--:--"
                            status = "下载 {:5.1f}% | {}/{} 文件 | {} / {} | {:.1f} MiB/s | ETA {} | 并发 {}/{}".format(
                                percent, completed, len(files), format_size(transferred),
                                format_size(exact_total), smoothed_speed / (1024 * 1024),
                                eta, active, limit
                            )
                        else:
                            status = "下载中 | {}/{} 文件 | {} | {:.1f} MiB/s | 并发 {}/{}".format(
                                completed, len(files), format_size(transferred),
                                smoothed_speed / (1024 * 1024), active, limit
                            )
                        display.status(status)
                        if reporter is not None:
                            reporter.progress(transferred, exact_total, "Downloading base image")
                        last_report = now
                        last_report_bytes = transferred

                    # 每 8 秒评估一次；只有新增线程带来至少 8% 增益才保留。
                    if adaptive and pending and now - last_adjust >= 8:
                        adjust_interval = max(now - last_adjust, 0.001)
                        window_speed = max(0, transferred - last_adjust_bytes) / adjust_interval
                        active, limit = limiter.snapshot()
                        enough_demand = active >= limit and len(pending) > limit

                        if probe_previous_limit is not None:
                            gain = (window_speed / probe_baseline_speed - 1.0) if probe_baseline_speed > 0 else 0.0
                            if gain >= 0.08:
                                display.log("并发 {} 的测速增益为 {:+.0f}%，保留该设置。".format(
                                    limit, gain * 100
                                ))
                                cooldown_until = now + 8
                            else:
                                limiter.set_limit(probe_previous_limit)
                                display.log("并发 {} 未带来有效增益（{:+.0f}%），回退到 {}。".format(
                                    limit, gain * 100, probe_previous_limit
                                ))
                                cooldown_until = now + 30
                            probe_previous_limit = None
                        elif (now >= cooldown_until and enough_demand and limit < max_workers
                              and window_speed >= 512 * 1024):
                            new_limit = min(max_workers, limit + 2)
                            probe_previous_limit = limit
                            probe_baseline_speed = window_speed
                            limiter.set_limit(new_limit)
                            display.log("试探并发 {} → {}（基准 {:.1f} MiB/s）…".format(
                                limit, new_limit, window_speed / (1024 * 1024)
                            ))

                        last_adjust = now
                        last_adjust_bytes = transferred
        finally:
            display.finish_line()

        download_elapsed = time.monotonic() - report_started
        if reporter is not None:
            reporter.record_timing("Download Artifactory files", download_elapsed)
        display.log("下载完成：{}，耗时 {}；开始{}…".format(
            format_size(stats.snapshot()), format_duration(download_elapsed),
            "校验并写入 CAS" if cas_only else "转换 Registry V2 格式"))
        if cas_store is not None:
            manifest_raw = (raw_root / "manifest.json").read_bytes()
            manifest = json.loads(manifest_raw)
            config_path = find_blob(raw_root, manifest["config"]["digest"], "config.digest")
            layer_paths = [find_blob(raw_root, item["digest"], "layer.digest")
                           for item in manifest["layers"]]
            cas_store.import_registry(manifest_raw, config_path, layer_paths,
                                      image_reference, target_platform, replace=True)
        if cas_only:
            cas_store.open_base(image_reference, target_platform, "artifactory")
            return
        conversion_started = time.monotonic()
        convert_registry_to_docker_archive(raw_root, archive_root, image_reference, display)
        conversion_elapsed = time.monotonic() - conversion_started
        if reporter is not None:
            reporter.record_timing("Convert Artifactory layers", conversion_elapsed)
        display.log("格式转换完成，耗时 {}；开始生成 Docker tar…".format(
            format_duration(conversion_elapsed)
        ))
        pack_elapsed = pack_files(archive_root, output, display)
        if reporter is not None:
            reporter.record_timing("Pack Artifactory base archive", pack_elapsed)


def main(argv=None) -> int:
    """运行独立的 Artifactory 下载与镜像归档转换命令。"""
    from errors import BuildError
    from settings import load_settings, repository_settings
    parser = argparse.ArgumentParser(description="下载 Artifactory Registry 制品并生成 docker load 可用的 tar")
    parser.add_argument("url", help="Artifactory 浏览页面或 /artifactory 制品目录 URL")
    parser.add_argument("--config", type=Path, help="Shared config.json path")
    parser.add_argument("-o", "--output", type=Path, help="输出 tar 文件路径")
    parser.add_argument("--workers", type=int,
                        help="并发下载数；默认 0，自动测速后动态选择")
    parser.add_argument("--username", help="Artifactory username")
    parser.add_argument("--password-env",
                        help="Environment variable holding the password")
    parser.add_argument("--ca-file", type=Path, help="Custom HTTPS CA bundle")
    args = parser.parse_args(argv)
    started = time.monotonic()

    try:
        settings, _ = load_settings(args.config)
        directory_url = artifact_directory_url(args.url)
        connection = repository_settings(settings, directory_url)
        workers = args.workers if args.workers is not None else connection.get("workers", 0)
        if workers < 0:
            raise ValueError("--workers 必须大于等于 0")
        password_env = args.password_env or connection.get(
            "passwordEnv", "PYIMAGEBUILDER_ARTIFACTORY_PASSWORD")
        password = os.environ.get(password_env)
        opener = make_opener(directory_url, args.username or connection.get("username"),
                             password, args.ca_file or connection.get("caFile"))
        print(f"制品目录：{directory_url}")
        image_reference = image_reference_from_directory(directory_url)
        print(f"镜像名称：{image_reference}")
        files = list_files(opener, directory_url)
        default_name = PurePosixPath(unquote(urlsplit(directory_url).path).rstrip("/")).name + ".tar"
        output = args.output or Path.cwd() / default_name
        if output.suffix.lower() != ".tar":
            output = output.with_suffix(".tar")
        if output.exists():
            raise RuntimeError("输出文件已存在：{}".format(output))
        download_and_pack(opener, directory_url, files, output, workers, image_reference)
        elapsed = time.monotonic() - started
        hours, remainder = divmod(int(elapsed), 3600)
        minutes, seconds = divmod(remainder, 60)
        print(f"完成：{output.resolve()}（共 {len(files)} 个文件）")
        print("总耗时：{:02d}:{:02d}:{:02d}（{:.1f} 秒）".format(
            hours, minutes, seconds, elapsed
        ))
        return 0
    except (BuildError, ValueError, RuntimeError, HTTPError, URLError, OSError) as exc:
        print(f"失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
