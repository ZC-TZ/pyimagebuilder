"""以 tar 形式写出并验证 OCI Image Layout。"""

import copy
import hashlib
import io
import json
import tarfile

from errors import ArchiveError
from image_reader import (archive_members, digest_hex, image_config_bytes, image_history, image_json_bytes,
                          parse_image_json, same_json_value)
from reproducible import add_file


INDEX_TYPE = "application/vnd.oci.image.index.v1+json"
MANIFEST_TYPE = "application/vnd.oci.image.manifest.v1+json"
CONFIG_TYPE = "application/vnd.oci.image.config.v1+json"
LAYER_TYPE = "application/vnd.oci.image.layer.v1.tar"
BUFFER = 4 * 1024 * 1024


def _json_bytes(value):
    """为明确的新 OCI JSON 对象编码；已有 blob 不能经此函数恢复摘要。"""
    return image_json_bytes(value)


def _descriptor(media_type, digest, size):
    return {"mediaType": media_type, "digest": digest, "size": size}


def _blob_name(digest):
    return "blobs/sha256/" + digest_hex(digest)


def _add_bytes(archive, name, raw, epoch=0):
    info = tarfile.TarInfo(name)
    info.mode = 0o644
    info.size = len(raw)
    info.mtime = epoch
    archive.addfile(info, io.BytesIO(raw))


class OCIImageWriter:
    """生成并验证单平台 OCI 镜像布局归档。"""
    def __init__(self, source_date_epoch=0):
        self.source_date_epoch = source_date_epoch

    def write_new(self, output, config, layers, tag, progress=None,
                  manifest_template=None, index_template=None):
        """明确创建派生 OCI 镜像；扩展元数据模板不能保留原镜像的 digest。"""
        return self.write(output, config, layers, tag, progress=progress,
                          manifest_template=manifest_template, index_template=index_template)

    def write_image(self, output, image, tag, progress=None):
        """以现有配置创建 OCI 布局，强制保留 ImageID。

        有原始 manifest 且无需改变描述符时保留其字节，否则生成未压缩层的 manifest。
        本方法重新创建 index，不能当作原样复制整个 OCI 的接口；
        OCI 无变化转存应复制已验证归档，以同时保留 manifest、index 和扩展字段。
        """
        raw = image.original_config_bytes()
        manifest_raw = image.original_manifest_bytes()
        template = parse_image_json(manifest_raw, "image manifest") if manifest_raw is not None else None
        return self.write(output, image.config, image.layers, tag, progress=progress, config_raw=raw,
                          manifest_template=template, manifest_raw=manifest_raw)

    def write(self, output, config, layers, tag, progress=None, config_raw=None,
              manifest_template=None, index_template=None, manifest_raw=None):
        """创建新的未压缩 OCI 布局；模板仅保留派生镜像的扩展元数据。

        manifest_raw 与模板绑定；只有最终 manifest 没有变化时才可复用原字节。
        发生转换时 manifest/index 会生成新的字节，模板仅用于保留扩展字段。
        config_raw 只用于保留 ImageID；原样转存 OCI 应复制已验证归档。
        此低层接口保留兼容性，业务路径选择 write_new 或 write_image 表达实际意图。
        """
        diff_ids = config.get("rootfs", {}).get("diff_ids")
        if not isinstance(diff_ids, list) or len(diff_ids) != len(layers):
            raise ArchiveError("OCI config/layer count mismatch")
        config_raw = image_config_bytes(config, config_raw)
        original_manifest = (image_json_bytes(manifest_template, manifest_raw, "image manifest")
                             if manifest_raw is not None else None)
        config_digest = "sha256:" + hashlib.sha256(config_raw).hexdigest()
        layer_descriptors = []
        inherited_layers = manifest_template.get("layers", []) if manifest_template is not None else []
        if manifest_template is not None and len(inherited_layers) != len(layers):
            raise ArchiveError("OCI manifest template/layer count mismatch")
        for number, (path, diff_id) in enumerate(zip(layers, diff_ids)):
            digest_hex(diff_id)
            descriptor = copy.deepcopy(inherited_layers[number]) if manifest_template is not None else {}
            descriptor.update(_descriptor(LAYER_TYPE, diff_id, path.stat().st_size))
            # 内联数据与外部 URL 属于旧 blob；不能随新内容继承。
            if manifest_template is not None and inherited_layers[number].get("digest") != diff_id:
                descriptor.pop("data", None)
                descriptor.pop("urls", None)
            layer_descriptors.append(descriptor)
        manifest = copy.deepcopy(manifest_template) if manifest_template is not None else {}
        config_descriptor = copy.deepcopy(manifest.get("config", {}))
        config_descriptor.update(_descriptor(CONFIG_TYPE, config_digest, len(config_raw)))
        if manifest.get("config", {}).get("digest") != config_digest:
            config_descriptor.pop("data", None)
            config_descriptor.pop("urls", None)
        manifest.update(schemaVersion=2, mediaType=MANIFEST_TYPE,
                        config=config_descriptor, layers=layer_descriptors)
        manifest_raw = (original_manifest if original_manifest is not None and
                        same_json_value(manifest, manifest_template) else _json_bytes(manifest))
        manifest_digest = "sha256:" + hashlib.sha256(manifest_raw).hexdigest()
        index = copy.deepcopy(index_template) if index_template is not None else {}
        manifest_reference = copy.deepcopy(index["manifests"][0]) if index_template is not None else {}
        manifest_reference.update(_descriptor(MANIFEST_TYPE, manifest_digest, len(manifest_raw)))
        manifest_reference.pop("data", None)
        manifest_reference.pop("urls", None)
        manifest_reference["platform"] = {"architecture": config["architecture"],
                                          "os": config["os"]}
        manifest_reference.setdefault("annotations", {}).update({
            "org.opencontainers.image.ref.name": tag,
            "io.containerd.image.name": tag,
        })
        index.update(schemaVersion=2, mediaType=INDEX_TYPE, manifests=[manifest_reference])
        index_raw = _json_bytes(index)
        total_bytes = (sum(path.stat().st_size for path, descriptor in
                           {item[1]["digest"]: item for item in zip(layers, layer_descriptors)}.values())
                       if progress is not None else 0)
        written_bytes = 0
        with tarfile.open(output, "w", format=tarfile.PAX_FORMAT) as archive:
            _add_bytes(archive, "oci-layout", _json_bytes({"imageLayoutVersion": "1.0.0"}),
                       self.source_date_epoch)
            _add_bytes(archive, "index.json", index_raw, self.source_date_epoch)
            _add_bytes(archive, _blob_name(config_digest), config_raw, self.source_date_epoch)
            _add_bytes(archive, _blob_name(manifest_digest), manifest_raw, self.source_date_epoch)
            seen = {config_digest, manifest_digest}
            for path, descriptor in zip(layers, layer_descriptors):
                if descriptor["digest"] not in seen:
                    callback = (lambda current, _total, label, base=written_bytes:
                                progress(base + current, total_bytes, label)) if total_bytes >= 8 * 1024 * 1024 else None
                    add_file(archive, _blob_name(descriptor["digest"]), path, self.source_date_epoch, callback)
                    written_bytes += path.stat().st_size
                    seen.add(descriptor["digest"])

    def verify(self, path, expected_tag):
        """导出后检查 OCI descriptor、平台、blob 大小与摘要。"""
        try:
            with tarfile.open(path, "r:") as archive:
                members = archive_members(archive)
                for name, member in members.items():
                    if not member.isfile():
                        raise ArchiveError("OCI layout member is not a file: " + name)

                def read_json(name):
                    member = members.get(name)
                    if member is None or member.size > 16 * 1024 * 1024:
                        raise ArchiveError("Missing or oversized OCI JSON: " + name)
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise ArchiveError("Unreadable OCI JSON: " + name)
                    with stream:
                        return json.load(stream)

                def verify_blob(descriptor, media_type):
                    if not isinstance(descriptor, dict) or descriptor.get("mediaType") != media_type:
                        raise ArchiveError("OCI descriptor media type mismatch")
                    if type(descriptor.get("size")) is not int or descriptor["size"] < 0:
                        raise ArchiveError("Invalid OCI descriptor size")
                    digest = descriptor.get("digest")
                    name = _blob_name(digest)
                    member = members.get(name)
                    if member is None or member.size != descriptor.get("size"):
                        raise ArchiveError("OCI blob missing or size mismatch: " + name)
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise ArchiveError("Unreadable OCI blob: " + name)
                    hasher = hashlib.sha256()
                    with stream:
                        for block in iter(lambda: stream.read(BUFFER), b""):
                            hasher.update(block)
                    if hasher.hexdigest() != digest_hex(digest):
                        raise ArchiveError("OCI blob digest mismatch: " + name)
                    return name

                if read_json("oci-layout") != {"imageLayoutVersion": "1.0.0"}:
                    raise ArchiveError("Invalid OCI layout marker")
                index = read_json("index.json")
                if (not isinstance(index, dict) or index.get("schemaVersion") != 2 or
                        index.get("mediaType") != INDEX_TYPE or
                        not isinstance(index.get("manifests"), list) or len(index["manifests"]) != 1):
                    raise ArchiveError("Invalid OCI index")
                reference = index["manifests"][0]
                if not isinstance(reference, dict):
                    raise ArchiveError("Invalid OCI manifest descriptor")
                annotations = reference.get("annotations")
                if (not isinstance(annotations, dict) or
                        annotations.get("org.opencontainers.image.ref.name") != expected_tag):
                    raise ArchiveError("OCI image reference mismatch")
                manifest_name = verify_blob(reference, MANIFEST_TYPE)
                manifest = read_json(manifest_name)
                if (not isinstance(manifest, dict) or manifest.get("schemaVersion") != 2 or
                        manifest.get("mediaType") != MANIFEST_TYPE or
                        not isinstance(manifest.get("layers"), list)):
                    raise ArchiveError("Invalid OCI manifest")
                config_name = verify_blob(manifest.get("config"), CONFIG_TYPE)
                config = read_json(config_name)
                if (not isinstance(config, dict) or not isinstance(config.get("rootfs"), dict) or
                        config["rootfs"].get("type") != "layers"):
                    raise ArchiveError("Invalid OCI image config")
                diff_ids = config["rootfs"].get("diff_ids")
                if not isinstance(diff_ids, list) or len(diff_ids) != len(manifest["layers"]):
                    raise ArchiveError("OCI layer count mismatch")
                image_history(config, len(diff_ids))
                if reference.get("platform") != {"architecture": config.get("architecture"),
                                                  "os": config.get("os")}:
                    raise ArchiveError("OCI platform mismatch")
                for descriptor, diff_id in zip(manifest["layers"], diff_ids):
                    if not isinstance(descriptor, dict):
                        raise ArchiveError("Invalid OCI layer descriptor")
                    if descriptor.get("digest") != diff_id:
                        raise ArchiveError("Uncompressed OCI layer must equal its DiffID")
                    verify_blob(descriptor, LAYER_TYPE)
                expected = {"oci-layout", "index.json", manifest_name, config_name}
                expected.update(_blob_name(item["digest"]) for item in manifest["layers"])
                if set(members) != expected:
                    raise ArchiveError("Unexpected or missing OCI layout members")
        except (tarfile.TarError, json.JSONDecodeError, KeyError, TypeError, ValueError,
                UnicodeDecodeError) as exc:
            raise ArchiveError("Invalid OCI layout archive: {}".format(exc)) from exc
