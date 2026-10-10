"""按内容摘要保存镜像数据，供 builder 直接读取或按需导出 Docker tar。

config、manifest 和层 blob 先校验落盘，ref 最后原子发布。
中断导入可能留下孤儿 blob，但不会发布只导入了一部分的镜像引用。
读取引用时核对关联 blob 的摘要；构建通过 open_base 取得有序的未压缩层。
"""

import hashlib
import argparse
import gzip
import json
import os
import tarfile
import tempfile
import sys
import zlib
from pathlib import Path

from compat import is_linked_directory, unlink_missing
from errors import ArchiveError, BuildError
from image_reader import (BaseImage, ImageArchiveReader, archive_members, digest_hex,
                          image_history, sha256_file)
from image_writer import ImageArchiveWriter
from oci_writer import OCIImageWriter, CONFIG_TYPE, LAYER_TYPE, MANIFEST_TYPE
from platforms import architecture, normalize_platform
from settings import cache_directory, load_settings
from store_lock import file_lock


SCHEMA_VERSION = 1
BUFFER = 4 * 1024 * 1024


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def docker_archive_tag(reference):
    """把 digest 引用映射为 Docker save 可使用的稳定 RepoTag；不改变 CAS 检索键。"""
    repository, separator, digest = reference.rpartition("@")
    if separator:
        if not repository:
            raise ArchiveError("Invalid digest image reference")
        return repository + ":pulled-" + digest_hex(digest)[:12]
    return reference


class CASStore:
    """按 SHA-256 去重 config、manifest 和层，并按引用、平台及来源维护 ref。"""

    def __init__(self, root):
        self.root = Path(root).resolve() / "cas"
        self.blobs = self.root / "blobs" / "sha256"
        self.refs = self.root / "refs"
        self.locks = self.root / "locks"
        if any(is_linked_directory(path) for path in
               (self.root, self.root / "blobs", self.blobs, self.refs, self.locks)):
            raise ArchiveError("CAS internal directories must not be symlinks")

    def _blob(self, digest):
        return self.blobs / digest_hex(digest)

    def _ref(self, reference, platform, source):
        key = hashlib.sha256((source + "\0" + reference + "\0" + platform).encode("utf-8")).hexdigest()
        return self.refs / (key + ".json")

    def ref_lock(self, reference, platform, source):
        """按引用、平台及来源互斥更新；调用方应覆盖快照、发布和失败恢复全过程。"""
        name = self._ref(reference, normalize_platform(platform), source).stem + ".lock"
        return file_lock(self.locks / name)

    def has_ref(self, reference, platform, source):
        """只检查指定 ref 文件是否存在；需要验证内容时应调用 resolve。"""
        return self._ref(reference, normalize_platform(platform), source).is_file()

    def remove_ref(self, reference, platform, source):
        """删除指定 ref，不直接删除共享 blob；无引用内容由 prune_orphans 清理。"""
        with self.ref_lock(reference, platform, source):
            unlink_missing(self._ref(reference, normalize_platform(platform), source))

    def list_refs(self):
        """列出已发布的 ref 元数据；孤儿 blob 不视为镜像，列表操作不代替完整校验。"""
        result = []
        for path in sorted(self.refs.glob("*.json")) if self.refs.is_dir() else []:
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                if value.get("schemaVersion") == SCHEMA_VERSION:
                    result.append(value)
            except (OSError, ValueError, AttributeError):
                continue
        return result

    def _put_stream(self, stream, digest, size):
        """写入新 blob 时核对输入长度和摘要，再原子发布。

        同摘要文件已存在时，校验该文件后直接复用，不再读取传入的 stream。
        """
        expected = digest_hex(digest)
        target = self._blob(digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_file():
            if target.is_symlink():
                raise ArchiveError("CAS blob path is a symlink: " + digest)
            if target.stat().st_size != size or sha256_file(target) != expected:
                raise ArchiveError("CAS blob is corrupt: " + digest)
            return target
        descriptor, pending = tempfile.mkstemp(prefix="blob-", suffix=".part", dir=target.parent)
        hasher = hashlib.sha256()
        written = 0
        try:
            with os.fdopen(descriptor, "wb") as output:
                for block in iter(lambda: stream.read(BUFFER), b""):
                    output.write(block)
                    hasher.update(block)
                    written += len(block)
                output.flush()
                os.fsync(output.fileno())
            if written != size or hasher.hexdigest() != expected:
                raise ArchiveError("CAS blob size or digest mismatch: " + digest)
            # 并发导入可能发布相同摘要；即使目标已存在，
            # 也必须核对大小和内容，不能只信任文件名。
            if target.exists():
                if target.is_symlink():
                    raise ArchiveError("CAS blob path is a symlink: " + digest)
                if target.stat().st_size != size or sha256_file(target) != expected:
                    raise ArchiveError("CAS blob collision: " + digest)
            else:
                os.replace(pending, target)
        finally:
            if os.path.exists(pending):
                os.unlink(pending)
        return target

    def _put_bytes(self, raw):
        import io
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        self._put_stream(io.BytesIO(raw), digest, len(raw))
        return digest

    def store_decoded_layer(self, path, diff_id):
        """校验并保存拉取阶段已解压的层，避免随后构建再次解压。"""
        path = Path(path)
        if not tarfile.is_tarfile(path):
            raise ArchiveError("Decoded CAS layer is not a tar archive: " + diff_id)
        with open(path, "rb") as stream:
            return self._put_stream(stream, diff_id, path.stat().st_size)

    def _publish(self, reference, platform, source, config_digest, layers,
                 replace=False, manifest_raw=None):
        """在所有 blob 落盘后发布 ref；未允许 replace 时拒绝改变既有镜像的 manifest。"""
        if manifest_raw is None:
            manifest_raw = _json({"schemaVersion": 2, "mediaType": MANIFEST_TYPE,
                                  "config": {"mediaType": CONFIG_TYPE, "digest": config_digest,
                                             "size": self._blob(config_digest).stat().st_size},
                                  "layers": [{"mediaType": item["mediaType"],
                                              "digest": item["digest"], "size": item["size"]}
                                             for item in layers]})
        manifest_digest = self._put_bytes(manifest_raw)
        value = {"schemaVersion": SCHEMA_VERSION, "reference": reference,
                 "platform": platform, "source": source, "manifest": manifest_digest,
                 "config": config_digest, "layers": layers}
        # 写入前就核对 raw manifest、config 与 ref 的绑定；不能发布后等 resolve 才发现。
        # 层已由 _put_stream 校验，这里避免再次完整读取大层。
        self._validate_ref_blobs(value, platform, verify_layers=False)
        with self.ref_lock(reference, platform, source):
            return self._publish_ref(value, replace)

    def _publish_ref(self, value, replace):
        """在持锁期间检查冲突并发布，避免两个首次导入同时通过不存在检查。"""
        reference, platform, source = value["reference"], value["platform"], value["source"]
        manifest_digest = value["manifest"]
        self.refs.mkdir(parents=True, exist_ok=True)
        destination = self._ref(reference, platform, source)
        if destination.is_file() and not replace:
            try:
                previous = json.loads(destination.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ArchiveError("Invalid existing CAS ref: " + str(destination)) from exc
            if not isinstance(previous, dict):
                raise ArchiveError("Invalid existing CAS ref: " + str(destination))
            if previous.get("manifest") != manifest_digest:
                raise BuildError("CAS tag conflicts; use --replace: " + reference)
        descriptor, pending = tempfile.mkstemp(prefix="ref-", suffix=".part", dir=self.refs)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(_json(value))
                output.flush()
                os.fsync(output.fileno())
            os.replace(pending, destination)
        finally:
            if os.path.exists(pending):
                os.unlink(pending)
        return value

    def snapshot_ref(self, reference, platform, source):
        """保存当前 ref 的原始字节用于失败恢复；不存在时返回 None。"""
        with self.ref_lock(reference, platform, source):
            path = self._ref(reference, normalize_platform(platform), source)
            return path.read_bytes() if path.is_file() else None

    def restore_ref(self, reference, platform, source, raw):
        """持锁恢复先前 ref；调用方须从快照开始持有 ref_lock，避免恢复过期快照。"""
        with self.ref_lock(reference, platform, source):
            self._restore_ref_locked(reference, platform, source, raw)

    def _restore_ref_locked(self, reference, platform, source, raw):
        path = self._ref(reference, normalize_platform(platform), source)
        if raw is None:
            unlink_missing(path)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, pending = tempfile.mkstemp(prefix="ref-restore-", suffix=".part", dir=path.parent)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
            os.replace(pending, path)
        finally:
            if os.path.exists(pending):
                os.unlink(pending)

    def import_registry(self, manifest_raw, config_path, layer_paths,
                        reference, platform, source="artifactory", replace=False):
        """按 descriptor 摘要保存原始 Registry blob，包括 config 和层。

        层可以是压缩或未压缩内容；本方法不解压，也不独立核对层的 DiffID。
        """
        platform = normalize_platform(platform)
        try:
            manifest = json.loads(manifest_raw)
            config_entry = manifest["config"]
            descriptors = manifest["layers"]
            config = json.loads(Path(config_path).read_bytes())
            diff_ids = config["rootfs"]["diff_ids"]
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ArchiveError("Invalid Registry manifest or config") from exc
        if (not isinstance(manifest, dict) or not isinstance(config, dict) or
                not isinstance(config_entry, dict) or
                manifest.get("schemaVersion") != 2 or not isinstance(descriptors, list) or
                not isinstance(diff_ids, list) or len(descriptors) != len(diff_ids) or
                len(layer_paths) != len(descriptors)):
            raise ArchiveError("Registry config/layer descriptors do not match")
        if config.get("os") != "linux" or config.get("architecture") != architecture(platform):
            raise ArchiveError("Registry image platform does not match " + platform)
        config_digest = config_entry.get("digest")
        if config_entry.get("size") is not None and config_entry["size"] != Path(config_path).stat().st_size:
            raise ArchiveError("Registry config descriptor size mismatch")
        with open(config_path, "rb") as stream:
            self._put_stream(stream, config_digest, Path(config_path).stat().st_size)
        layers = []
        for entry, path, diff_id in zip(descriptors, layer_paths, diff_ids):
            if not isinstance(entry, dict):
                raise ArchiveError("Invalid Registry layer descriptor")
            digest = entry.get("digest")
            digest_hex(diff_id)
            size = Path(path).stat().st_size
            if entry.get("size") is not None and entry["size"] != size:
                raise ArchiveError("Registry layer descriptor size mismatch")
            with open(path, "rb") as stream:
                self._put_stream(stream, digest, size)
            with open(path, "rb") as stream:
                magic = stream.read(2)
            media_type = entry.get("mediaType") or ("application/vnd.oci.image.layer.v1.tar+gzip"
                     if magic == b"\x1f\x8b" else LAYER_TYPE)
            if not isinstance(media_type, str):
                raise ArchiveError("Invalid Registry layer media type")
            layers.append({"digest": digest, "diff_id": diff_id,
                           "mediaType": media_type, "size": size})
        return self._publish(reference, platform, source, config_digest, layers,
                             replace, manifest_raw)

    def import_docker(self, archive, reference, platform, source="local", replace=False):
        """完整验证 Docker save 归档，去重未压缩层，最后发布指定标签及平台的 ref。"""
        platform = normalize_platform(platform)
        archive = Path(archive).resolve()
        with tempfile.TemporaryDirectory(prefix="cas-import-") as temporary:
            image = ImageArchiveReader(archive, Path(temporary)).read(reference, platform)
            if reference not in image.repo_tags and docker_archive_tag(reference) not in image.repo_tags:
                raise ArchiveError("CAS import reference is not tagged in Docker archive: " + reference)
            # 沿用 reader 已验证的字节快照；重开归档会把未经同次验证的配置混入旧层。
            config_digest = self._put_bytes(image.original_config_bytes())
            layers = []
            for path, diff_id in zip(image.layers, image.config["rootfs"]["diff_ids"]):
                size = path.stat().st_size
                with open(path, "rb") as stream:
                    self._put_stream(stream, diff_id, size)
                layers.append({"digest": diff_id, "diff_id": diff_id,
                               "mediaType": LAYER_TYPE, "size": size})
            return self._publish(reference, platform, source, config_digest, layers, replace)

    def import_oci(self, archive, reference, platform, source="local", replace=False):
        """导入单平台未压缩 OCI layout，保留校验过的原始 manifest 字节与摘要。"""
        platform = normalize_platform(platform)
        snapshot = OCIImageWriter().verify(archive, docker_archive_tag(reference))
        with tarfile.open(archive, "r:") as source_archive:
            members = archive_members(source_archive)
            # 锚定第一次校验的原始身份；替换后的自洽 manifest/config 不能替代快照。
            for name, raw in snapshot.items():
                member = members.get(name)
                if member is None or not member.isfile() or member.size != len(raw):
                    raise ArchiveError("OCI metadata changed after verification: " + name)
                with source_archive.extractfile(member) as stream:
                    if stream.read(len(raw) + 1) != raw:
                        raise ArchiveError("OCI metadata changed after verification: " + name)
            index = json.loads(snapshot["index.json"])
            descriptor = index["manifests"][0]
            if descriptor["platform"] != {"os": "linux", "architecture": architecture(platform)}:
                raise ArchiveError("OCI archive platform does not match " + platform)
            manifest_name = "blobs/sha256/" + digest_hex(descriptor["digest"])
            manifest_raw = snapshot[manifest_name]
            manifest = json.loads(manifest_raw)
            config_entry = manifest["config"]
            config_name = "blobs/sha256/" + digest_hex(config_entry["digest"])
            config_raw = snapshot[config_name]
            config = json.loads(config_raw)
            if config.get("architecture") != architecture(platform) or config.get("os") != "linux":
                raise ArchiveError("OCI config platform does not match " + platform)
            diff_ids = config.get("rootfs", {}).get("diff_ids")
            if not isinstance(diff_ids, list) or len(diff_ids) != len(manifest["layers"]):
                raise ArchiveError("OCI config/layer count mismatch")
            expected_members = set(snapshot)
            expected_members.update("blobs/sha256/" + digest_hex(entry["digest"])
                                    for entry in manifest["layers"])
            if set(members) != expected_members or any(not member.isfile() for member in members.values()):
                raise ArchiveError("OCI members changed after verification")
            for entry in manifest["layers"]:
                name = "blobs/sha256/" + digest_hex(entry["digest"])
                if members[name].size != entry["size"]:
                    raise ArchiveError("OCI layer size changed after verification: " + name)
            config_digest = self._put_bytes(config_raw)
            layers = []
            for entry, diff_id in zip(manifest["layers"], diff_ids):
                if entry["mediaType"] != LAYER_TYPE or entry["digest"] != diff_id:
                    raise BuildError("CAS OCI import currently requires uncompressed layers")
                name = "blobs/sha256/" + digest_hex(entry["digest"])
                with source_archive.extractfile(members[name]) as stream:
                    self._put_stream(stream, entry["digest"], entry["size"])
                layers.append({"digest": entry["digest"], "diff_id": diff_id,
                               "mediaType": entry["mediaType"], "size": entry["size"]})
        # 重建 manifest 会丢失注解并改变 digest；与 Registry 导入一样保存原始身份。
        return self._publish(reference, platform, source, config_digest, layers,
                             replace, manifest_raw)

    def resolve(self, reference, platform="linux/amd64", source=None):
        """校验 ref、manifest、config 和传输 blob 的一致性，再返回引用元数据。

        source=None 时要求来源唯一；同名镜像存在多个来源时必须明确选择。
        压缩层的摘要校验不等于 DiffID 校验，解压后的内容由 open_base/export_docker 核对。
        """
        platform = normalize_platform(platform)
        sources = (source,) if source is not None else ("local", "registry", "artifactory")
        found = []
        for kind in sources:
            path = self._ref(reference, platform, kind)
            if path.is_file():
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    raise ArchiveError("Invalid CAS ref: " + str(path)) from exc
                if (not isinstance(value, dict) or value.get("schemaVersion") != SCHEMA_VERSION or
                        value.get("reference") != reference or value.get("platform") != platform or
                        value.get("source") != kind or not isinstance(value.get("layers"), list)):
                    raise ArchiveError("CAS ref metadata mismatch: " + str(path))
                found.append(value)
        if not found:
            raise BuildError("Image not present in CAS: {} ({})".format(reference, platform))
        if len(found) != 1:
            raise BuildError("Multiple CAS sources for {}; specify --source".format(reference))
        value = found[0]
        return self._validate_ref_blobs(value, platform)

    def _validate_ref_blobs(self, value, platform, verify_layers=True):
        """核对引用与原始 manifest/config；读取镜像时还完整校验传输层摘要。

        GC 仅需确认可达关系，因此核对层存在性、大小及元数据，避免全盘重哈希大层。
        config 和 manifest 始终校验摘要，不能信任损坏 ref 中遗漏或改写的层列表。
        """
        try:
            for item in value["layers"]:
                if (not isinstance(item, dict) or
                        type(item.get("size")) is not int or item["size"] < 0 or
                        not isinstance(item.get("mediaType"), str) or
                        not item["mediaType"]):
                    raise ArchiveError("CAS ref has invalid layer descriptor")
                digest_hex(item["diff_id"])
            digests = [value["manifest"], value["config"]] + [item["digest"] for item in value["layers"]]
        except (KeyError, TypeError, AttributeError) as exc:
            raise ArchiveError("CAS ref has incomplete descriptors") from exc
        for index, digest in enumerate(digests):
            path = self._blob(digest)
            if (path.is_symlink() or not path.is_file() or
                    ((verify_layers or index < 2) and sha256_file(path) != digest_hex(digest))):
                raise ArchiveError("Missing or corrupt CAS blob: " + digest)
        if any(self._blob(item["digest"]).stat().st_size != item.get("size")
               for item in value["layers"]):
            raise ArchiveError("CAS layer descriptor size mismatch")
        try:
            manifest = json.loads(self._blob(value["manifest"]).read_bytes())
            config_entry = manifest["config"]
            manifest_layers = manifest["layers"]
            matches = (manifest.get("schemaVersion") == 2 and
                       isinstance(config_entry, dict) and
                       isinstance(manifest_layers, list) and
                       config_entry.get("digest") == value["config"] and
                       (config_entry.get("size") is None or
                        config_entry["size"] == self._blob(value["config"]).stat().st_size) and
                       len(manifest_layers) == len(value["layers"]) and
                       all(isinstance(entry, dict) and
                           entry.get("digest") == item["digest"] and
                           (entry.get("size") is None or entry["size"] == item["size"]) and
                           (entry.get("mediaType") is None or
                            entry["mediaType"] == item["mediaType"])
                           for entry, item in zip(manifest_layers, value["layers"])))
        except (ValueError, TypeError, AttributeError, KeyError) as exc:
            raise ArchiveError("Invalid CAS manifest") from exc
        if not matches:
            raise ArchiveError("CAS manifest/ref mismatch")
        try:
            config = json.loads(self._blob(value["config"]).read_bytes())
            rootfs = config["rootfs"]
            if (config.get("os") != "linux" or
                    config.get("architecture") != architecture(platform) or
                    rootfs.get("type") != "layers" or
                    rootfs.get("diff_ids") != [item["diff_id"] for item in value["layers"]]):
                raise ArchiveError("CAS config platform or DiffIDs mismatch")
            image_history(config, len(value["layers"]))
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ArchiveError("Invalid CAS config") from exc
        return value

    def prune_orphans(self, dry_run=False, excluding=()):
        """清理未被 ref 引用的 blob；现有镜像对应的已解压 DiffID 层也会保留。

        删除前核对所有保留引用与原始 manifest/config；损坏引用使整次清理失败。
        导入、拉取或构建期间不要并发执行清理，以免删除尚未发布引用的数据。
        """
        excluded = {Path(path).resolve() for path in excluding}
        if excluded and not dry_run:
            raise BuildError("Excluding refs is only supported for CAS prune dry runs")
        reachable = set()
        for ref_path in sorted(self.refs.glob("*.json")) if self.refs.is_dir() else []:
            if ref_path.resolve() in excluded:
                continue
            try:
                if ref_path.is_symlink():
                    raise ArchiveError("CAS ref path is a symlink")
                value = json.loads(ref_path.read_text(encoding="utf-8"))
                if (not isinstance(value, dict) or value.get("schemaVersion") != SCHEMA_VERSION or
                        not isinstance(value.get("layers"), list) or
                        any(not isinstance(value.get(key), str) or not value[key]
                            for key in ("reference", "platform", "source"))):
                    raise ValueError("invalid ref metadata")
                platform = normalize_platform(value["platform"])
                if (platform != value["platform"] or
                        self._ref(value["reference"], platform, value["source"]) != ref_path):
                    raise ValueError("ref identity does not match its filename")
                self._validate_ref_blobs(value, platform, verify_layers=False)
                digests = [value["manifest"], value["config"]]
                for item in value["layers"]:
                    digests.extend((item["digest"], item["diff_id"]))
                reachable.update(digest_hex(digest) for digest in digests)
            except (OSError, ValueError, TypeError, KeyError, AttributeError, ArchiveError, BuildError) as exc:
                raise ArchiveError("Cannot prune CAS with invalid ref {}: {}".format(ref_path, exc)) from exc
        orphans = []
        for path in sorted(self.blobs.iterdir()) if self.blobs.is_dir() else []:
            if path.is_symlink() or not path.is_file() or len(path.name) != 64:
                raise ArchiveError("Unexpected CAS blob entry: " + str(path))
            if path.name not in reachable:
                orphans.append(path)
        reclaimed = sum(path.stat().st_size for path in orphans)
        if not dry_run:
            for path in orphans:
                path.unlink()
        return {"orphan_blobs": len(orphans), "reclaimable_bytes": reclaimed,
                "dry_run": dry_run}

    def export_docker(self, reference, output, platform="linux/amd64", source=None):
        """从 CAS 生成 Docker save tar，并校验解压后的 DiffID 与最终归档。"""
        value = self.resolve(reference, platform, source)
        output = Path(output).resolve()
        if output.exists():
            raise BuildError("CAS export output already exists: " + str(output))
        config_raw = self._blob(value["config"]).read_bytes()
        config = json.loads(config_raw)
        if (config.get("rootfs", {}).get("diff_ids") !=
                [item["diff_id"] for item in value["layers"]]):
            raise ArchiveError("CAS config DiffIDs do not match ref")
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="cas-export-") as temporary:
            layers = []
            for index, item in enumerate(value["layers"]):
                blob = self._blob(item["digest"])
                media_type = item["mediaType"]
                if media_type.endswith(".tar") and item["digest"] == item["diff_id"]:
                    layer = blob
                elif media_type.endswith("+gzip") or media_type.endswith(".gzip"):
                    layer = Path(temporary) / ("layer-{}.tar".format(index))
                    try:
                        with gzip.open(blob, "rb") as source_stream, open(layer, "wb") as target:
                            for block in iter(lambda: source_stream.read(BUFFER), b""):
                                target.write(block)
                    except (OSError, EOFError, zlib.error) as exc:
                        raise ArchiveError("Cannot decompress CAS layer: " + str(exc)) from exc
                else:
                    raise BuildError("Unsupported CAS layer compression: " + media_type)
                if sha256_file(layer) != digest_hex(item["diff_id"]) or not tarfile.is_tarfile(layer):
                    raise ArchiveError("CAS layer DiffID or tar format mismatch")
                layers.append(layer)
            writer = ImageArchiveWriter()
            created = False
            try:
                archive_tag = docker_archive_tag(reference)
                # 预检不能代替排他创建，两个导出者可能同时看到路径不存在。
                # 已创建的句柄传给 writer，避免再次以覆盖模式打开路径。
                with open(output, "xb") as sink:
                    created = True
                    image = BaseImage(config, layers, [archive_tag], {}, value["config"], config_raw)
                    writer.write_image(output, image, archive_tag, fileobj=sink)
                writer.verify(output, archive_tag)
            except Exception:
                if created:
                    unlink_missing(output)
                raise
        return output

    def open_base(self, reference, platform="linux/amd64", source=None):
        """返回含配置和有序 layer.tar 路径的 BaseImage，供 builder 直接消费 CAS。

        缺少解压缓存时，先解压、核对 DiffID，再按内容摘要入库。
        不生成中间 Docker save tar；不支持的压缩格式明确报错。
        """
        value = self.resolve(reference, platform, source)
        try:
            config_raw = self._blob(value["config"]).read_bytes()
            config = json.loads(config_raw)
            diff_ids = config["rootfs"]["diff_ids"]
            if (config.get("os") != "linux" or
                    config.get("architecture") != architecture(normalize_platform(platform)) or
                    config["rootfs"].get("type") != "layers" or
                    not isinstance(diff_ids, list) or len(diff_ids) != len(value["layers"]) or
                    diff_ids != [item["diff_id"] for item in value["layers"]]):
                raise ArchiveError("CAS base config platform or DiffIDs mismatch")
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise ArchiveError("Invalid CAS base config") from exc
        paths = []
        for item in value["layers"]:
            media_type = item["mediaType"]
            diff_id = item["diff_id"]
            digest_hex(diff_id)
            if media_type.endswith(".tar") and item["digest"] == diff_id:
                layer = self._blob(diff_id)
            elif media_type.endswith("+gzip") or media_type.endswith(".gzip"):
                layer = self._blob(diff_id)
                if not layer.is_file():
                    self.blobs.mkdir(parents=True, exist_ok=True)
                    descriptor, pending = tempfile.mkstemp(prefix="decoded-", suffix=".part",
                                                            dir=self.blobs)
                    try:
                        with os.fdopen(descriptor, "wb") as target, \
                                gzip.open(self._blob(item["digest"]), "rb") as source_stream:
                            for block in iter(lambda: source_stream.read(BUFFER), b""):
                                target.write(block)
                        with open(pending, "rb") as source_stream:
                            self._put_stream(source_stream, diff_id, Path(pending).stat().st_size)
                    except (OSError, EOFError, zlib.error) as exc:
                        raise ArchiveError("Cannot decode CAS base layer: " + str(exc)) from exc
                    finally:
                        unlink_missing(pending)
                if layer.is_symlink() or sha256_file(layer) != digest_hex(diff_id):
                    raise ArchiveError("CAS decoded layer is corrupt: " + diff_id)
            else:
                raise BuildError("Unsupported CAS layer compression: " + media_type)
            if not tarfile.is_tarfile(layer):
                raise ArchiveError("CAS base layer is not a tar archive: " + diff_id)
            paths.append(layer)
        return BaseImage(config, paths, [reference],
                         {"CASManifest": value["manifest"], "Source": value["source"]},
                         value["config"], config_raw,
                         manifest_raw=self._blob(value["manifest"]).read_bytes(),
                         manifest_digest=value["manifest"])


def main(argv=None):
    """独立脚本入口：导入、检查、导出和清理 CAS 镜像库。"""
    parser = argparse.ArgumentParser(description="Manage verified image blobs and refs")
    actions = parser.add_subparsers(dest="action", required=True)
    for name in ("import", "inspect", "export", "prune"):
        action = actions.add_parser(name)
        action.add_argument("--config", type=Path)
        action.add_argument("--image-store", type=Path)
        if name in ("import", "inspect", "export"):
            action.add_argument("--platform", choices=("linux/amd64", "linux/arm64"),
                                default="linux/amd64")
            action.add_argument("--source", choices=("local", "registry", "artifactory"),
                                default="local" if name == "import" else None)
        if name == "import":
            action.add_argument("archive", type=Path)
            action.add_argument("reference")
            action.add_argument("--format", choices=("docker", "oci"), required=True)
            action.add_argument("--replace", action="store_true")
        elif name == "inspect":
            action.add_argument("reference")
        elif name == "export":
            action.add_argument("reference")
            action.add_argument("-o", "--output", type=Path, required=True)
        else:
            action.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        settings, _ = load_settings(args.config)
        store = CASStore(args.image_store or cache_directory(settings, "imageStore", None) or
                         Path(tempfile.gettempdir()) / "pyimagebuilder-image-store")
        if args.action == "import":
            if args.format == "docker":
                result = store.import_docker(args.archive, args.reference, args.platform,
                                             args.source, args.replace)
            else:
                result = store.import_oci(args.archive, args.reference, args.platform,
                                          args.source, args.replace)
        elif args.action == "inspect":
            result = store.resolve(args.reference, args.platform, args.source)
        elif args.action == "export":
            path = store.export_docker(args.reference, args.output, args.platform, args.source)
            result = {"reference": args.reference, "output": str(path)}
        else:
            result = store.prune_orphans(args.dry_run)
    except (ArchiveError, BuildError, OSError, ValueError, KeyError, TypeError) as exc:
        print("CAS command failed: {}".format(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
