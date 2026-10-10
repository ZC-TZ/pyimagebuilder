"""写出 Docker save 兼容镜像归档，并核对其内部引用和摘要。"""

import hashlib
import io
import json
import tarfile

from errors import ArchiveError
from image_reader import (MAX_JSON, archive_members, digest_hex, docker_manifest,
                          image_config_bytes, image_history, outer_name, parse_image_json,
                          verify_layer_tar)
from reproducible import add_file


def _add_bytes(archive, name, raw, epoch=0):
    info = tarfile.TarInfo(name)
    info.mode = 0o644
    info.size = len(raw)
    info.mtime = epoch
    archive.addfile(info, io.BytesIO(raw))


def _chain_ids(diff_ids):
    parent = None
    for diff_id in diff_ids:
        digest_hex(diff_id)
        if parent is None:
            parent = diff_id
        else:
            parent = "sha256:" + hashlib.sha256((parent + " " + diff_id).encode("ascii")).hexdigest()
        yield digest_hex(parent)


class ImageArchiveWriter:
    """根据配置与有序未压缩层生成并验证 Docker save 归档。"""
    def __init__(self, source_date_epoch=0):
        self.source_date_epoch = source_date_epoch

    def write_new(self, output, config, layers, tag, progress=None, fileobj=None):
        """明确创建派生镜像，以稳定 JSON 编码生成新的配置身份。"""
        return self._write(output, config, layers, tag, progress=progress, fileobj=fileobj)

    def write_image(self, output, image, tag, progress=None, fileobj=None):
        """转存已读取的镜像；原始配置缺失或已修改时，在创建输出前拒绝。

        write_new 用于生成派生配置，write_image 用于保留现有 ImageID；调用方不能混用。
        Docker save 的外层 manifest/repositories 是归档索引，可因标签变化重新生成。
        """
        raw = image.original_config_bytes()
        return self._write(output, image.config, image.layers, tag, progress=progress,
                          fileobj=fileobj, config_raw=raw)

    def _write(self, output, config, layers, tag, progress=None, fileobj=None, config_raw=None):
        """以稳定名称和 manifest 引用直接写入指定输出 tar。

        本方法不检查输出是否已存在，也不完整核对输入层的内容摘要。
        构建入口负责选择临时输出、调用 verify，再发布到最终交付路径。
        fileobj 指定时写入调用方已打开的二进制流，其关闭及输出所有权由调用方管理。
        config_raw 用于转存现有镜像，保留原始配置字节与 ImageID；新构建省略此参数。
        内部共用实现；调用方只能通过 write_new 或 write_image 表达写入意图。
        """
        diff_ids = config.get("rootfs", {}).get("diff_ids")
        if not isinstance(diff_ids, list) or len(diff_ids) != len(layers):
            raise ArchiveError("Image config/layer count mismatch")
        raw_config = image_config_bytes(config, config_raw)
        config_name = hashlib.sha256(raw_config).hexdigest() + ".json"
        identifiers = list(_chain_ids(diff_ids))
        names = [item + "/layer.tar" for item in identifiers]
        manifest = [{"Config": config_name, "RepoTags": [tag], "Layers": names}]
        manifest_raw = json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        last_slash = tag.rfind("/")
        colon = tag.rfind(":")
        repository, version = (tag[:colon], tag[colon + 1:]) if colon > last_slash else (tag, "latest")
        top = identifiers[-1] if identifiers else config_name[:-5]
        repositories_raw = json.dumps({repository: {version: top}}, separators=(",", ":")).encode("utf-8")
        total_bytes = sum(path.stat().st_size for path in layers) if progress is not None else 0
        written_bytes = 0
        target = {"fileobj": fileobj} if fileobj is not None else {"name": output}
        with tarfile.open(mode="w", format=tarfile.PAX_FORMAT, **target) as archive:
            _add_bytes(archive, "manifest.json", manifest_raw, self.source_date_epoch)
            _add_bytes(archive, config_name, raw_config, self.source_date_epoch)
            _add_bytes(archive, "repositories", repositories_raw, self.source_date_epoch)
            for index, (identifier, layer_path) in enumerate(zip(identifiers, layers)):
                _add_bytes(archive, identifier + "/VERSION", b"1.0\n", self.source_date_epoch)
                legacy = {"id": identifier}
                if index:
                    legacy["parent"] = identifiers[index - 1]
                _add_bytes(archive, identifier + "/json",
                           json.dumps(legacy, separators=(",", ":")).encode(),
                           self.source_date_epoch)
                layer_size = layer_path.stat().st_size
                callback = (lambda current, _total, label, base=written_bytes:
                            progress(base + current, total_bytes, label)) if total_bytes >= 8 * 1024 * 1024 else None
                add_file(archive, identifier + "/layer.tar", layer_path, self.source_date_epoch, callback)
                written_bytes += layer_size

    def verify(self, path, expected_tag):
        """重新读取输出归档，核对成员结构、有界 JSON、配置摘要及各层 DiffID。"""
        try:
            with tarfile.open(path, "r:") as archive:
                # extractfile(name) 会选中重复成员或跟随 tar 链接，不能用于发布门禁。
                members = archive_members(archive)

                def regular_member(name):
                    member = members.get(name)
                    if member is None or not member.isfile():
                        raise ArchiveError("Output missing regular file: " + name)
                    return member

                def read_json_bytes(name):
                    member = regular_member(name)
                    if member.size > MAX_JSON:
                        raise ArchiveError("Oversized output JSON: " + name)
                    with archive.extractfile(member) as stream:
                        raw = stream.read(MAX_JSON + 1)
                    if len(raw) != member.size or len(raw) > MAX_JSON:
                        raise ArchiveError("Invalid output JSON size: " + name)
                    return raw

                def invalid_constant(token):
                    raise ValueError("Non-JSON numeric constant: " + token)

                manifests = docker_manifest(json.loads(read_json_bytes("manifest.json"),
                                                       parse_constant=invalid_constant))
                if not isinstance(manifests, list) or len(manifests) != 1 or not isinstance(manifests[0], dict):
                    raise ArchiveError("Output manifest must contain exactly one image")
                manifest = manifests[0]
                if expected_tag not in (manifest.get("RepoTags") or []):
                    raise ArchiveError("Output image tag mismatch")
                config_name = outer_name(manifest["Config"])
                raw_config = read_json_bytes(config_name)
                if hashlib.sha256(raw_config).hexdigest() + ".json" != config_name:
                    raise ArchiveError("Output config SHA-256 mismatch")
                config = parse_image_json(raw_config, "output image config")
                rootfs = config.get("rootfs")
                if not isinstance(rootfs, dict) or rootfs.get("type") != "layers":
                    raise ArchiveError("Output rootfs.type must be layers")
                diff_ids = rootfs["diff_ids"]
                names = [outer_name(name) for name in manifest["Layers"]]
                if not isinstance(diff_ids, list) or not isinstance(names, list) or len(diff_ids) != len(names):
                    raise ArchiveError("Output layer count mismatch")
                image_history(config, len(diff_ids))
                if len(set(names)) != len(names):
                    raise ArchiveError("Output manifest contains duplicate layers")
                for name, expected in zip(names, diff_ids):
                    stream = archive.extractfile(regular_member(name))
                    hasher = hashlib.sha256()
                    with stream:
                        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                            hasher.update(block)
                        if hasher.hexdigest() != digest_hex(expected):
                            raise ArchiveError("Output layer DiffID mismatch: " + name)
                        verify_layer_tar(stream, name)
        except (tarfile.TarError, json.JSONDecodeError, KeyError, TypeError, ValueError,
                UnicodeDecodeError) as exc:
            raise ArchiveError("Invalid output image archive: {}".format(exc)) from exc
