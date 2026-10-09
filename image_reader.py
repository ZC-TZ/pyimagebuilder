"""读取本地 Docker save 归档，验证配置和未压缩层的 DiffID。"""

import errno
import hashlib
import json
import os
import re
import shutil
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path

from errors import ArchiveError, BaseImageNotFound
from platforms import architecture, normalize_platform


BUFFER = 4 * 1024 * 1024
MAX_JSON = 16 * 1024 * 1024
SHA256 = re.compile(r"^sha256:([0-9a-fA-F]{64})$")
# 无版本的旧条目没有证明跨磁盘副本经过校验，首次复用必须重新验证。
BASE_CACHE_VERSION = 1


def digest_hex(value):
    """验证 sha256 摘要格式，返回不含算法前缀的十六进制值。"""
    match = SHA256.fullmatch(str(value))
    if not match:
        raise ArchiveError("Expected sha256 digest, got {}".format(value))
    return match.group(1).lower()


def sha256_file(path):
    """分块计算文件摘要，避免把大镜像归档整体读入内存。"""
    value = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(BUFFER), b""):
            value.update(block)
    return value.hexdigest()


def outer_name(name):
    """规范化外层 tar 成员名，供重复成员与路径越界检查使用。"""
    while name.startswith("./"):
        name = name[2:]
    if name in ("", "."):
        return ""
    if not name or name.startswith("/") or "\\" in name or ".." in name.split("/"):
        raise ArchiveError("Unsafe outer archive path: " + name)
    return name.rstrip("/")


def archive_members(archive):
    """按规范化名称索引外层 tar，允许 ./ 前缀但拒绝重复及越界路径。"""
    members = {}
    for member in archive:
        name = outer_name(member.name)
        if not name:
            continue
        if name in members:
            raise ArchiveError("Duplicate outer archive member: " + name)
        members[name] = member
    return members


def docker_manifest(value):
    """校验 Docker save 索引结构，标签必须按数组元素精确匹配。

    未标记镜像允许缺省、null 或空 RepoTags；字符串和对象不能当成标签集合。
    路径越界仍由 outer_name 检查，层与配置内容由各读取入口继续校验。
    """
    if not isinstance(value, list) or not value:
        raise ArchiveError("Docker save manifest must contain images")
    for item in value:
        if not isinstance(item, dict):
            raise ArchiveError("Invalid Docker save manifest entry")
        tags = item.get("RepoTags")
        if tags is not None and (not isinstance(tags, list) or
                                  any(not isinstance(tag, str) or not tag for tag in tags)):
            raise ArchiveError("Docker save RepoTags must be an array of nonempty strings")
        if not isinstance(item.get("Config"), str) or not item["Config"]:
            raise ArchiveError("Docker save Config must be a nonempty path")
        layers = item.get("Layers")
        if not isinstance(layers, list) or any(not isinstance(name, str) or not name for name in layers):
            raise ArchiveError("Docker save Layers must be an array of nonempty paths")
    return value


def same_json_value(left, right):
    """比较 JSON 值的类型和内容，避免 Python 把 true、1、1.0 或正负零混同。

    此比较用于检测解析对象是否已被修改，不能用于计算镜像或 manifest 的摘要。
    对象键顺序不影响值的比较；传输身份始终由原始字节决定。
    """
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            same_json_value(left[key], right[key]) for key in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(same_json_value(a, b) for a, b in zip(left, right))
    if isinstance(left, float):
        return repr(left) == repr(right)
    return left == right


def parse_image_json(raw, label="image JSON"):
    """在有大小上限的原始 blob 上解析 JSON 对象，拒绝非 JSON 数值常量。"""
    if not isinstance(raw, bytes):
        raise ArchiveError("Original " + label + " must be bytes")
    if len(raw) > MAX_JSON:
        raise ArchiveError("Oversized " + label)
    try:
        def invalid_constant(token):
            raise ValueError("Non-JSON numeric constant: " + token)
        decoded = json.loads(raw, parse_constant=invalid_constant)
    except (ValueError, UnicodeError) as exc:
        raise ArchiveError("Invalid original " + label) from exc
    if not isinstance(decoded, dict):
        raise ArchiveError(label + " must be a JSON object")
    return decoded


def image_json_bytes(value, raw=None, label="image JSON"):
    """新对象使用稳定编码；已有 JSON blob 核对类型和值后保留原始字节。

    此处统一防止 config/manifest 因忘记 raw 或宽松值比较产生隐蔽身份变化。
    新生成 JSON 必须符合 JSON 数值范围，不能输出 NaN/Infinity。
    """
    if raw is None:
        try:
            raw = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                             separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError, UnicodeError) as exc:
            raise ArchiveError("Cannot encode " + label) from exc
    decoded = parse_image_json(raw, label)
    if not same_json_value(decoded, value):
        raise ArchiveError("Original " + label + " does not match parsed object")
    return raw


def image_config_bytes(config, raw=None):
    """保留转存的原始 config；ImageID 始终取该原始字节串的 SHA-256。"""
    return image_json_bytes(config, raw, "image config")


def image_history(config, layer_count):
    """校验可选 history；缺省、null 和空数组表示没有历史记录。

    history 缺失不代表文件层缺失，不能据此判定镜像损坏或猜测原始构建命令。
    有记录时严格核对文件层对应关系，防止把字符串/数字当成 empty_layer 布尔值。
    只返回读取视图，不补写配置，以免转存时改变原始 ImageID。
    """
    history = config.get("history")
    if history is None:
        return []
    if not isinstance(history, list):
        raise ArchiveError("Image history must be an array or null")
    for item in history:
        if not isinstance(item, dict):
            raise ArchiveError("Image history entries must be objects")
        empty = item.get("empty_layer")
        if empty is not None and type(empty) is not bool:
            raise ArchiveError("Image history empty_layer must be a boolean or null")
    if history and sum(not item.get("empty_layer", False) for item in history) != layer_count:
        raise ArchiveError("Image history does not match rootfs.diff_ids")
    return history


@dataclass
class BaseImage:
    """封装已验证的基础配置、有序层路径和来源信息，供 builder 读取。"""
    config: dict
    layers: list
    repo_tags: list
    source_manifest: dict
    config_digest: str = None
    config_raw: bytes = None
    source_index: dict = None
    manifest_raw: bytes = None
    manifest_digest: str = None

    def original_config_bytes(self):
        """转存必须携带原始配置，且解析对象和声明摘要仍与该字节串一致。

        不允许在丢失原始字节后回退到 json.dumps；需要修改配置的派生构建应走新写入。
        """
        if self.config_raw is None:
            raise ArchiveError("Image transfer requires original config bytes")
        raw = image_config_bytes(self.config, self.config_raw)
        actual = "sha256:" + hashlib.sha256(raw).hexdigest()
        if self.config_digest is not None and self.config_digest != actual:
            raise ArchiveError("Original config digest does not match image identity")
        return raw

    def original_manifest_bytes(self):
        """存在原始 manifest 时核对其摘要及 config 绑定，防止混合两个来源的 blob。"""
        if self.manifest_raw is None:
            return None
        manifest = parse_image_json(self.manifest_raw, "image manifest")
        actual = "sha256:" + hashlib.sha256(self.manifest_raw).hexdigest()
        if self.manifest_digest is not None and self.manifest_digest != actual:
            raise ArchiveError("Original manifest digest does not match image identity")
        config = manifest.get("config")
        config_raw = self.original_config_bytes()
        if (not isinstance(config, dict) or
                config.get("digest") != "sha256:" + hashlib.sha256(config_raw).hexdigest() or
                (config.get("size") is not None and config["size"] != len(config_raw))):
            raise ArchiveError("Original manifest/config identity mismatch")
        return self.manifest_raw


class ImageArchiveReader:
    """验证 Docker save 归档后，向构建器提供配置和层 tar 文件。"""
    def __init__(self, path, workspace, trusted_layer_store=None):
        self.path = Path(path)
        self.workspace = Path(workspace)
        self.trusted_layer_store = Path(trusted_layer_store) if trusted_layer_store is not None else None

    @staticmethod
    def _identity(path):
        data = path.stat()
        return [data.st_dev, data.st_ino, data.st_size, data.st_mtime_ns, data.st_ctime_ns]

    def _cached_layer(self, name, expected):
        if self.trusted_layer_store is None:
            return None, None
        key = hashlib.sha256((str(self.path.resolve()) + "\0" + name + "\0" +
                              expected).encode("utf-8")).hexdigest()
        directory = self.trusted_layer_store.resolve()
        layer = directory / "layers" / (digest_hex(expected) + ".tar")
        metadata = directory / "entries" / (key + ".json")
        try:
            entry = json.loads(metadata.read_text(encoding="utf-8"))
            if (entry.get("version") == BASE_CACHE_VERSION and
                    entry.get("source") == self._identity(self.path) and
                    entry.get("layer") == self._identity(layer) and
                    entry.get("diff_id") == expected):
                return layer, (layer, metadata)
        except (OSError, ValueError, AttributeError):
            pass
        return None, (layer, metadata)

    def _publish_layer(self, target, location, expected):
        if location is None:
            return target
        layer, metadata = location
        layer.parent.mkdir(parents=True, exist_ok=True)
        metadata.parent.mkdir(parents=True, exist_ok=True)
        if layer.is_file() and sha256_file(layer) == digest_hex(expected):
            target.unlink()
        else:
            try:
                os.replace(target, layer)
            except OSError as exc:
                if exc.errno != errno.EXDEV:
                    raise
                # workspace 与缓存可位于不同磁盘；先复制到缓存磁盘，
                # 再在同一文件系统内原子发布，不能让读者看到半份层。
                descriptor, pending = tempfile.mkstemp(prefix="base-layer-", suffix=".part", dir=layer.parent)
                try:
                    with os.fdopen(descriptor, "wb") as output, open(target, "rb") as stream:
                        shutil.copyfileobj(stream, output, BUFFER)
                        output.flush()
                        os.fsync(output.fileno())
                    # 读归档时验证的是 workspace 层；副本仍需在发布前独立核对。
                    # 失败保留 workspace 文件，不发布错误层或可信身份条目。
                    if sha256_file(pending) != digest_hex(expected):
                        raise ArchiveError("Base layer DiffID changed during cross-device cache copy")
                    os.replace(pending, layer)
                    target.unlink()
                finally:
                    if os.path.exists(pending):
                        os.unlink(pending)
        entry = {"version": BASE_CACHE_VERSION,
                 "source": self._identity(self.path), "layer": self._identity(layer),
                 "diff_id": expected}
        descriptor, pending = tempfile.mkstemp(prefix="base-entry-", suffix=".part", dir=metadata.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(entry, stream, separators=(",", ":"))
            os.replace(pending, metadata)
        finally:
            if os.path.exists(pending):
                os.unlink(pending)
        return layer

    def read(self, from_reference, expected_platform=None, progress=None):
        """选取指定引用及平台，验证 config/layer 摘要并提取层。

        显式本地映射在归档仅含一个镜像时允许引用别名；多镜像归档要求准确匹配。
        配置可信层缓存时可复用已验证的层，否则写入调用方提供的私有 workspace。
        """
        if expected_platform is not None:
            normalize_platform(expected_platform)
        if not self.path.is_file():
            raise BaseImageNotFound("Local base image archive not found: " + str(self.path))
        try:
            with tarfile.open(self.path, "r:*") as archive:
                members = archive_members(archive)

                def json_member(name):
                    member = members.get(name)
                    if member is None or not member.isfile():
                        raise ArchiveError("Missing archive JSON: " + name)
                    if member.size > MAX_JSON:
                        raise ArchiveError("Oversized archive JSON: " + name)
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise ArchiveError("Unreadable archive member: " + name)
                    with stream:
                        return json.load(stream)

                manifest = docker_manifest(json_member("manifest.json"))
                matched = [item for item in manifest if from_reference in (item.get("RepoTags") or [])]
                if expected_platform is not None and matched:
                    selected_by_platform = []
                    for item in matched:
                        candidate = json_member(outer_name(item.get("Config", "")))
                        if (isinstance(candidate, dict) and candidate.get("os") == "linux" and
                                candidate.get("architecture") == architecture(expected_platform)):
                            selected_by_platform.append(item)
                    if len(selected_by_platform) != 1:
                        raise BaseImageNotFound("FROM {} has no unique {} entry".format(
                            from_reference, expected_platform))
                    selected = selected_by_platform[0]
                elif matched:
                    if len(matched) != 1:
                        raise BaseImageNotFound("FROM {} is ambiguous across platforms".format(from_reference))
                    selected = matched[0]
                elif len(manifest) == 1:
                    # 显式 --base-tar/--base-map 允许单镜像归档使用映射别名。
                    selected = manifest[0]
                else:
                    raise BaseImageNotFound("FROM {} not present in multi-image archive".format(from_reference))
                config_name = outer_name(selected.get("Config", ""))
                config_member = members.get(config_name)
                if config_member is None or not config_member.isfile():
                    raise ArchiveError("Missing image config: " + config_name)
                if config_member.size > MAX_JSON:
                    raise ArchiveError("Oversized image config: " + config_name)
                config_stream = archive.extractfile(config_member)
                if config_stream is None:
                    raise ArchiveError("Unreadable image config")
                with config_stream:
                    config_raw = config_stream.read()
                if re.fullmatch(r"[0-9a-fA-F]{64}\.json", config_name):
                    if hashlib.sha256(config_raw).hexdigest() + ".json" != config_name.lower():
                        raise ArchiveError("Base image config SHA-256 mismatch")
                config = json.loads(config_raw)
                if not isinstance(config, dict):
                    raise ArchiveError("Image config is not an object")
                if (config.get("os") != "linux" or
                        config.get("architecture") not in ("amd64", "arm64")):
                    raise ArchiveError("Base image must be linux/amd64 or linux/arm64")
                if (expected_platform is not None and
                        config["architecture"] != architecture(expected_platform)):
                    raise ArchiveError("Base image platform does not match " + expected_platform)
                rootfs = config.get("rootfs")
                if not isinstance(rootfs, dict) or rootfs.get("type") != "layers":
                    raise ArchiveError("Config rootfs.type must be layers")
                diff_ids = rootfs.get("diff_ids")
                names = selected.get("Layers")
                if not isinstance(diff_ids, list) or not isinstance(names, list) or len(diff_ids) != len(names):
                    raise ArchiveError("Layers and rootfs.diff_ids have different counts")
                image_history(config, len(diff_ids))
                self.workspace.mkdir(parents=True, exist_ok=True)
                paths = []
                for index, (name, expected) in enumerate(zip(names, diff_ids), 1):
                    normalized = outer_name(name)
                    member = members.get(normalized)
                    if member is None or not member.isfile():
                        raise ArchiveError("Missing layer: " + normalized)
                    digest_hex(expected)
                    cached, location = self._cached_layer(normalized, expected)
                    if cached is not None:
                        paths.append(cached)
                        continue
                    target = self.workspace / ("base-layer-{}.tar".format(index))
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise ArchiveError("Unreadable layer: " + normalized)
                    hasher = hashlib.sha256()
                    with stream, open(target, "wb") as output:
                        copied = 0
                        while True:
                            block = stream.read(BUFFER)
                            if not block:
                                break
                            output.write(block)
                            hasher.update(block)
                            copied += len(block)
                            if progress is not None:
                                progress(copied, member.size, "Extracting base layer {}/{}".format(index, len(names)))
                    if hasher.hexdigest() != digest_hex(expected):
                        raise ArchiveError("Layer {} DiffID mismatch".format(index))
                    if not tarfile.is_tarfile(target):
                        raise ArchiveError("Layer {} is not a tar archive".format(index))
                    paths.append(self._publish_layer(target, location, expected))
                return BaseImage(config, paths, selected.get("RepoTags") or [], selected,
                                 "sha256:" + hashlib.sha256(config_raw).hexdigest(), config_raw)
        except (tarfile.TarError, json.JSONDecodeError, UnicodeDecodeError, TypeError, KeyError) as exc:
            raise ArchiveError("Invalid Docker save archive: {}".format(exc)) from exc
