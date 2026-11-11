#!/usr/bin/env python3
"""离线分析镜像空间、保守裁剪完整层，并审计指令缓存。"""

import argparse
import copy
import hashlib
import json
import math
import os
import shutil
import sys
import tarfile
import tempfile
from collections import defaultdict
from contextlib import ExitStack, contextmanager
from pathlib import Path

from cache import CACHE_VERSION
from cache_identity import file_identity, stat_identity
from cache_activity import cache_activity
from compat import is_linked_directory, unlink_missing
from path_policy import output_path
from errors import ArchiveError, BuildError
from file_publish import publication_transaction, publish_new_file
from file_resources import open_regular_file
from image_reader import (BaseImage, DigestReader, ImageArchiveReader, archive_members, digest_hex,
                          docker_manifest, image_config_bytes, image_history, parse_image_json, sha256_file)
from image_reader import verify_layer_tar
from image_writer import ImageArchiveWriter
from oci_writer import OCIImageWriter
from platforms import architecture
from reproducible import parse_epoch
from rootfs import RootFSIndex, clean_path
from permissions import normalized_metadata


BUFFER = 4 * 1024 * 1024
MAX_JSON = 16 * 1024 * 1024


def _member_json(archive, name):
    try:
        member = archive_members(archive)[name]
    except KeyError as exc:
        raise ArchiveError("Missing archive JSON: " + name) from exc
    if not member.isfile() or member.size > MAX_JSON:
        raise ArchiveError("Missing or oversized archive JSON: " + name)
    stream = archive.extractfile(member)
    if stream is None:
        raise ArchiveError("Unreadable archive JSON: " + name)
    with stream:
        raw = stream.read(MAX_JSON + 1)
    if len(raw) != member.size or len(raw) > MAX_JSON:
        raise ArchiveError("Invalid archive JSON size: " + name)
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ArchiveError("Invalid archive JSON: " + name) from exc


def _identify(archive, requested_tag):
    names = archive_members(archive)
    if "manifest.json" in names:
        manifest = docker_manifest(_member_json(archive, "manifest.json"))
        if not isinstance(manifest, list) or not manifest:
            raise ArchiveError("Invalid Docker manifest")
        if any(not isinstance(item, dict) or
               (item.get("RepoTags") is not None and
                (not isinstance(item["RepoTags"], list) or
                 any(not isinstance(tag, str) for tag in item["RepoTags"])))
               for item in manifest):
            raise ArchiveError("Invalid Docker manifest tags")
        tags = [tag for item in manifest if isinstance(item, dict)
                for tag in (item.get("RepoTags") or []) if isinstance(tag, str)]
        if requested_tag is None:
            if len(set(tags)) != 1:
                raise BuildError("Specify --tag for a multi-image Docker archive")
            requested_tag = tags[0]
        if requested_tag not in tags:
            raise BuildError("Tag is not present in Docker archive")
        return "docker", requested_tag
    if "index.json" in names:
        index = _member_json(archive, "index.json")
        entries = index.get("manifests") if isinstance(index, dict) else None
        if not isinstance(entries, list) or len(entries) != 1:
            raise BuildError("Phase 14 accepts single-platform OCI archives only")
        item = entries[0]
        annotations = item.get("annotations") if isinstance(item, dict) else None
        tag = annotations.get("org.opencontainers.image.ref.name") if isinstance(annotations, dict) else None
        if not isinstance(tag, str) or (requested_tag is not None and requested_tag != tag):
            raise BuildError("OCI archive tag is missing or differs from --tag")
        return "oci", tag
    raise ArchiveError("Expected a Docker save or single-platform OCI tar")


@contextmanager
def _archive_source(path):
    """贯穿识别、读取与无变化复制，收尾时核对实际句柄和源路径。"""
    with open_regular_file(path) as stream:
        stamp = file_identity(path, content=False)
        initial = stat_identity(os.fstat(stream.fileno()))
        if initial[:4] != stamp[:4]:
            raise ArchiveError("Source archive changed while opening")
        yield stream
        if (stat_identity(os.fstat(stream.fileno())) != initial or
                file_identity(path, content=False) != stamp):
            raise ArchiveError("Source archive changed during optimization or analysis")


def _load_image(path, stream, workspace, tag, platform):
    """保留已校验的原始 OCI JSON，并验证实际写入私有层文件的字节。"""
    stream.seek(0)
    with tarfile.open(fileobj=stream, mode="r:*") as archive:
        kind, tag = _identify(archive, tag)
    if kind == "docker":
        image = ImageArchiveReader(path, workspace / "layers").read(tag, platform, fileobj=stream)
        if tag not in image.repo_tags:
            raise ArchiveError("Docker archive tag changed during reading")
        image.original_config_bytes()
        return kind, tag, image, None

    stream.seek(0)
    snapshot = OCIImageWriter().verify(path, tag, fileobj=stream)
    index = parse_image_json(snapshot["index.json"], "OCI index")
    descriptor = index["manifests"][0]
    manifest_digest = descriptor["digest"]
    manifest_raw = snapshot["blobs/sha256/" + digest_hex(manifest_digest)]
    manifest = parse_image_json(manifest_raw, "OCI manifest")
    config_digest = manifest["config"]["digest"]
    config_raw = snapshot["blobs/sha256/" + digest_hex(config_digest)]
    config = parse_image_json(config_raw, "OCI config")
    if platform is not None and (config.get("os"), config.get("architecture")) != ("linux", architecture(platform)):
        raise ArchiveError("OCI archive platform does not match " + platform)

    stream.seek(0)
    with tarfile.open(fileobj=stream, mode="r:") as archive:
        members = archive_members(archive)
        layer_entries = manifest["layers"]
        names = ["blobs/sha256/" + digest_hex(entry["digest"]) for entry in layer_entries]
        if set(members) != set(snapshot).union(names) or any(not member.isfile() for member in members.values()):
            raise ArchiveError("OCI members changed after verification")
        for name, raw in snapshot.items():
            member = members[name]
            with archive.extractfile(member) as metadata:
                if member.size != len(raw) or metadata.read(len(raw) + 1) != raw:
                    raise ArchiveError("OCI metadata changed after verification: " + name)
        layer_dir = workspace / "layers"
        layer_dir.mkdir(parents=True, exist_ok=True)
        layers = []
        for number, (name, entry) in enumerate(zip(names, layer_entries)):
            member = members[name]
            if member.size != entry["size"]:
                raise ArchiveError("OCI layer size changed after verification: " + name)
            target = layer_dir / ("layer-{}.tar".format(number))
            with archive.extractfile(member) as payload, target.open("wb") as output:
                copied = DigestReader(payload)
                shutil.copyfileobj(copied, output, BUFFER)
            if copied.current != entry["size"] or copied.digest.hexdigest() != digest_hex(entry["digest"]):
                raise ArchiveError("OCI layer content changed after verification: " + name)
            layers.append(target)
    image = BaseImage(config, layers, [tag], manifest, config_digest, config_raw, index,
                      manifest_raw, manifest_digest)
    return kind, tag, image, snapshot


def load_image(path, workspace, tag=None, with_image=False, *, platform=None):
    """返回已验证镜像；默认四元组供分析使用，转存必须取原始配置。"""
    path = Path(path).resolve()
    with _archive_source(path) as stream:
        kind, tag, image, _ = _load_image(path, stream, Path(workspace), tag, platform)
    return (kind, tag, image) if with_image else (kind, tag, image.config, image.layers)


def _index(layers):
    result = RootFSIndex()
    for layer in layers:
        result.apply_layer(layer)
    return result


def _hash_stream(stream):
    value = hashlib.sha256()
    for block in iter(lambda: stream.read(BUFFER), b""):
        value.update(block)
    return value.hexdigest()


def _signature(index, memo, reporter=None):
    """比较最终文件树、共享 inode 拓扑及有效元数据，不比较硬链接的创建顺序。"""
    result = []
    if index.root_metadata is not None:
        member = index.root_metadata
        result.append(("", "root-directory", (member.mode, member.uid, member.gid,
                       member.mtime, json.dumps(member.pax_headers, sort_keys=True))))
    needed = defaultdict(set)
    for entry in index.entries.values():
        if entry.layer and (entry.layer, entry.member) not in memo:
            needed[entry.layer].add(entry.member)
    total, completed = sum(len(names) for names in needed.values()), 0
    # 按层顺序解析一次；不能为每个文件重新 getmember 并扫描整份大 tar。
    for layer, names in needed.items():
        with tarfile.open(layer, "r:") as archive:
            for raw in archive:
                if raw.name not in names:
                    continue
                member = normalized_metadata(raw)
                if not (member.isfile() or member.isdir() or member.issym() or
                        member.isfifo() or member.ischr() or member.isblk()):
                    raise ArchiveError("Unsupported rootfs entry type during optimization: " + repr(member.type))
                if (member.isfifo() or member.ischr() or member.isblk()) and member.size:
                    raise ArchiveError("Special rootfs entry has unexpected payload: " + member.name)
                content = None
                if member.isfile():
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise ArchiveError("Unreadable visible file: " + member.name)
                    with stream:
                        content = _hash_stream(stream)
                memo[(layer, raw.name)] = (member.type, member.size, member.mode, member.uid,
                                          member.gid, member.mtime, member.linkname,
                                          member.devmajor, member.devminor,
                                          json.dumps(member.pax_headers, sort_keys=True), content)
                completed += 1
                if reporter is not None:
                    reporter.progress(completed, total, "Compare optimization files", unit="files")
        if any((layer, name) not in memo for name in names):
            raise ArchiveError("Missing rootfs backing member during optimization")
    inode_names = {}
    for path, entry in sorted(index.entries.items()):
        if not entry.layer:
            result.append((path, "implicit-directory"))
            continue
        effective = list(memo[(entry.layer, entry.member)])
        if entry.metadata is not None:
            metadata = entry.metadata
            effective[2:6] = [metadata.mode, metadata.uid, metadata.gid, metadata.mtime]
            effective[9] = json.dumps(metadata.pax_headers, sort_keys=True)
        # 文件内容来源已经固定。最终拓扑按可见路径分组，不按原 tar 链接头指向谁。
        # 两份相同字节的独立文件不能与共享 inode 的两个名字混为一谈。
        regular = entry.kind in ("file", "hardlink")
        alias = inode_names.setdefault(id(entry.inode), path) if regular else None
        result.append((path, "file" if regular else entry.kind, tuple(effective), alias))
    return tuple(result)


def analyze_image(path, tag=None, large_mib=50, *, platform=None):
    """报告层大小、隐藏字节和重复文件内容；隐藏判断包含硬链接对旧内容的引用。"""
    path = Path(path).resolve()
    with tempfile.TemporaryDirectory(prefix="pyimagebuilder-analyze-") as temp:
        with _archive_source(path) as source:
            archive_size = os.fstat(source.fileno()).st_size
            kind, tag, image, _ = _load_image(path, source, Path(temp), tag, platform)
        config, layers = image.config, image.layers
        final = _index(layers)
        live_content = {(entry.layer, entry.member) for entry in final.entries.values()
                        if entry.kind in ("file", "hardlink") and entry.layer}
        duplicate_content = defaultdict(list)
        layers_report = []
        for number, layer in enumerate(layers):
            payload = hidden = 0
            largest = []
            with tarfile.open(layer, "r:") as archive:
                for member in archive:
                    name = clean_path(member.name)
                    if not name or not member.isfile() or name.rsplit("/", 1)[-1].startswith(".wh."):
                        continue
                    payload += member.size
                    if (str(layer), member.name) not in live_content:
                        hidden += member.size
                    if member.size:
                        largest.append((member.size, name))
                    if member.size:
                        stream = archive.extractfile(member)
                        if stream is None:
                            raise ArchiveError("Unreadable image file: " + name)
                        with stream:
                            digest = _hash_stream(stream)
                        duplicate_content[(member.size, digest)].append((number + 1, name))
            layers_report.append({"number": number + 1, "tar_bytes": layer.stat().st_size,
                                  "file_payload_bytes": payload, "hidden_file_bytes": hidden,
                                  "largest_files": [{"path": name, "bytes": size}
                                                    for size, name in sorted(largest, reverse=True)[:5]]})
        duplicates = []
        for (size, digest), occurrences in duplicate_content.items():
            if len(occurrences) > 1:
                duplicates.append({"sha256": digest, "file_bytes": size,
                                   "copies": len(occurrences),
                                   "potential_duplicate_bytes": size * (len(occurrences) - 1),
                                   "locations": [{"layer": layer, "path": name}
                                                 for layer, name in occurrences[:20]]})
        duplicates.sort(key=lambda item: item["potential_duplicate_bytes"], reverse=True)
        threshold = int(large_mib * 1024 * 1024)
        large = [item["number"] for item in layers_report if item["tar_bytes"] >= threshold]
        advice = []
        if large:
            advice.append("Inspect largest_files in large_layers; move build-only artifacts to a separate stage.")
        if any(item["hidden_file_bytes"] for item in layers_report):
            advice.append("Files replaced or deleted in later layers still occupy earlier layers; combine creation and cleanup where possible.")
        if duplicates:
            advice.append("Check duplicate_content locations for repeated COPY/ADD outputs; verify whether both paths are needed.")
        return {"format": kind, "tag": tag, "architecture": config.get("architecture"),
                "archive_bytes": archive_size,
                "uncompressed_layer_bytes": sum(item["tar_bytes"] for item in layers_report),
                "hidden_file_bytes": sum(item["hidden_file_bytes"] for item in layers_report),
                "large_layers": large,
                "layers": layers_report, "duplicate_content": duplicates[:50],
                "duplicate_groups": len(duplicates), "recommendations": advice,
                "note": "Duplicate and hidden byte counts are opportunities, not guaranteed savings."}


def optimize_image(path, output, tag=None, source_date_epoch=0, reporter=None, *, platform=None):
    """仅移除已证明不影响最终文件树的完整层，并同步重写配置。

    不是文件级压缩；比较共享 inode 和设备/FIFO 元数据，无法证明等价的层保留。
    """
    output = output_path(output)
    path = Path(path).resolve()
    if output == path or output.exists() or output.suffix.lower() != ".tar":
        raise BuildError("Optimization output must be a new .tar file")
    epoch = parse_epoch(source_date_epoch)
    output.parent.mkdir(parents=True, exist_ok=True)
    with publication_transaction() as published, \
            tempfile.TemporaryDirectory(prefix="pyimagebuilder-optimize-", dir=output.parent) as temp, \
            ExitStack() as source_scope:
        source = source_scope.enter_context(_archive_source(path))
        if reporter is not None:
            reporter.phase_start("Read optimization source")
        original_size = os.fstat(source.fileno()).st_size
        kind, tag, image, snapshot = _load_image(path, source, Path(temp) / "input", tag, platform)
        config, layers = image.config, image.layers
        if reporter is not None:
            reporter.phase_success()
            reporter.phase_start("Compare optimization filesystem")
        memo = {}
        original = _signature(_index(layers), memo, reporter)
        if reporter is not None:
            reporter.phase_success()
            reporter.phase_start("Check redundant layers")
        retained = list(range(len(layers)))
        removed = []
        for number in range(len(layers)):
            candidate = [item for item in retained if item != number]
            try:
                same = _signature(_index([layers[item] for item in candidate]), memo) == original
            except ArchiveError:
                same = False
            if same:
                retained = candidate
                removed.append(number)
            if reporter is not None:
                reporter.log("镜像层 {}/{}：{}".format(number + 1, len(layers), "已证明冗余，移除" if same else "保留"))
                reporter.progress(number + 1, len(layers), "Check redundant layers", unit="layers")
        if reporter is not None:
            reporter.phase_success()
            reporter.phase_start("Write optimized image")
        updated = copy.deepcopy(config)
        updated["rootfs"]["diff_ids"] = [config["rootfs"]["diff_ids"][item] for item in retained]
        history = image_history(config, len(layers))
        # 无历史的镜像照常裁层；保留缺省/null/[]，不能伪造其原始构建记录。
        if history:
            layer_number = -1
            filtered = []
            for item in history:
                if not item.get("empty_layer", False):
                    layer_number += 1
                    if layer_number in removed:
                        continue
                filtered.append(item)
            updated["history"] = filtered
        expected_config = image_config_bytes(updated) if removed else image.original_config_bytes()
        partial = Path(temp) / "optimized.tar"
        writer = ImageArchiveWriter(epoch) if kind == "docker" else OCIImageWriter(epoch)
        if not removed:
            if kind == "oci":
                # 没有优化发生时，manifest/index 也不能重编码或丢失注解。
                source.seek(0)
                with partial.open("xb") as target:
                    shutil.copyfileobj(source, target, BUFFER)
            else:
                writer.write_image(partial, image, tag)
        elif kind == "oci":
            template = copy.deepcopy(image.source_manifest)
            template["layers"] = [template["layers"][item] for item in retained]
            writer.write_new(partial, updated, [layers[item] for item in retained], tag,
                         manifest_template=template, index_template=image.source_index)
        else:
            writer.write_new(partial, updated, [layers[item] for item in retained], tag)
        if reporter is not None:
            reporter.phase_success()
            reporter.phase_start("Verify optimized filesystem")
        with _archive_source(partial) as verification:
            writer.verify(partial, tag)
            _, _, verified, verified_snapshot = _load_image(partial, verification, Path(temp) / "verify", tag, platform)
        if verified.original_config_bytes() != expected_config or (not removed and snapshot != verified_snapshot):
            raise ArchiveError("Optimized archive does not preserve the expected metadata identity")
        if _signature(_index(verified.layers), {}, reporter) != original:
            raise BuildError("Optimized image rootfs verification failed")
        if reporter is not None:
            reporter.phase_success()
            reporter.log("优化完成：移除 {} 层，保留 {} 层。未证明冗余的层不会删除。".format(len(removed), len(retained)))
        optimized_size = partial.stat().st_size
        source_scope.close()
        publish_new_file(partial, output)
        published.append(output)
        return {"output": str(output), "format": kind, "tag": tag,
                "removed_layer_numbers": [item + 1 for item in removed],
                "before_bytes": original_size, "after_bytes": optimized_size,
                "saved_bytes": original_size - optimized_size}


def cache_files(directory):
    """清理前预检全部缓存节点，空条目和排除项也不能通过链接读取库外文件。"""
    root = Path(directory).resolve()
    entries_dir = root / "entries"
    layers_dir = root / "layers"
    if is_linked_directory(entries_dir) or is_linked_directory(layers_dir):
        raise BuildError("Cache entries/layers directories must not be symlinks")
    if not entries_dir.is_dir() or not layers_dir.is_dir():
        raise BuildError("Expected a pyimagebuilder cache with entries/ and layers/")
    entries = sorted(entries_dir.glob("*.json"))
    layers = sorted(layers_dir.glob("*.tar"))
    for path in entries + layers:
        if path.is_symlink() or not path.is_file():
            raise BuildError("Cache entry/layer must be a regular file, not a symlink: " + str(path))
    return entries, layers


def audit_cache(directory, excluding=()):
    """验证缓存条目，找出缺失、损坏或无引用的层；excluding 用于模拟条目删除。"""
    root = Path(directory).resolve()
    entries_dir, layers_dir = root / "entries", root / "layers"
    entries, layers = cache_files(root)
    referenced = set()
    invalid = []
    valid = 0
    excluded = {Path(path).resolve() for path in excluding}
    for path in entries:
        if path.resolve() in excluded:
            continue
        reason = None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if (not isinstance(data, dict) or data.get("version") != CACHE_VERSION or
                    data.get("key") != path.stem):
                reason = "wrong version or key"
            elif data.get("empty") is True and data.get("diff_id") is None:
                valid += 1
            else:
                digest = digest_hex(data.get("diff_id"))
                layer = layers_dir / (digest + ".tar")
                if (path.is_symlink() or layer.is_symlink() or not layer.is_file() or
                        type(data.get("size")) is not int or
                        layer.stat().st_size != data["size"] or sha256_file(layer) != digest):
                    reason = "missing or corrupt layer"
                else:
                    # is_tarfile 只检查开头；与缓存读取共用完整负载及结束位置检查。
                    with open(layer, "rb") as stream:
                        verify_layer_tar(stream, "cache audit layer")
                    valid += 1
                    referenced.add(layer.name)
        except (OSError, ValueError, TypeError, ArchiveError, tarfile.TarError) as exc:
            reason = "invalid metadata: " + type(exc).__name__
        if reason:
            invalid.append({"path": str(path), "reason": reason, "bytes": path.stat().st_size})
    orphan = [{"path": str(path), "bytes": path.stat().st_size}
              for path in layers if path.name not in referenced]
    return {"cache_dir": str(root), "valid_entries": valid, "invalid_entries": invalid,
            "orphan_layers": orphan,
            "reclaimable_bytes": sum(item["bytes"] for item in invalid + orphan)}


def prune_cache(directory):
    """删除审计认定无效的条目和孤儿层；dry_run 只报告计划。"""
    with cache_activity(directory, maintenance=True):
        return _prune_cache(directory)


def _prune_cache(directory):
    report = audit_cache(directory)
    for item in report["invalid_entries"] + report["orphan_layers"]:
        path = Path(item["path"])
        if path.is_symlink() or path.parent.resolve() not in (
                Path(directory).resolve() / "entries", Path(directory).resolve() / "layers"):
            raise BuildError("Unsafe cache prune path")
        unlink_missing(path)
    report["pruned"] = True
    return report


def main(argv=None):
    """独立脚本入口：分析镜像、保守优化或审计指令缓存。"""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    image = commands.add_parser("analyze", help="Report large layers and duplicate/hidden files")
    image.add_argument("--archive", type=Path, required=True)
    image.add_argument("--tag")
    image.add_argument("--platform", choices=("linux/amd64", "linux/arm64"))
    image.add_argument("--large-mib", type=float, default=50)
    image.add_argument("--output", type=Path, help="Optional JSON report file")
    optimize = commands.add_parser("optimize", help="Drop layers proven redundant for final rootfs")
    optimize.add_argument("--archive", type=Path, required=True)
    optimize.add_argument("--output", type=Path, required=True)
    optimize.add_argument("--tag")
    optimize.add_argument("--platform", choices=("linux/amd64", "linux/arm64"))
    optimize.add_argument("--source-date-epoch", default=os.environ.get("SOURCE_DATE_EPOCH", "0"))
    cache = commands.add_parser("cache", help="Audit stale or corrupt layer cache entries")
    cache.add_argument("--cache-dir", type=Path)
    cache.add_argument("--config", type=Path, help="Shared config.json path")
    cache.add_argument("--prune", action="store_true", help="Delete invalid entries and orphan layers")
    args = parser.parse_args(argv)
    try:
        if args.command == "analyze":
            if not math.isfinite(args.large_mib) or args.large_mib < 0:
                raise BuildError("--large-mib must be nonnegative")
            result = analyze_image(args.archive, args.tag, args.large_mib, platform=args.platform)
            if args.output:
                args.output = output_path(args.output)
                if args.output.exists():
                    raise BuildError("Report output already exists")
                with open(args.output, "x", encoding="utf-8") as stream:
                    stream.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        elif args.command == "optimize":
            with publication_transaction() as published:
                result = optimize_image(args.archive, args.output, args.tag, args.source_date_epoch, platform=args.platform)
                published.append(Path(result["output"]))
                print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
            return 0
        else:
            from settings import cache_directory, load_settings
            settings, _ = load_settings(args.config)
            directory = args.cache_dir or cache_directory(
                settings, "layerCache", Path(tempfile.gettempdir()) / "pyimagebuilder-layer-cache")
            result = prune_cache(directory) if args.prune else audit_cache(directory)
    except (ArchiveError, BuildError, OSError, tarfile.TarError, json.JSONDecodeError) as exc:
        parser.exit(1, "Analysis failed: {}\n".format(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
