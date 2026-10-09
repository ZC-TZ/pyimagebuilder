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
from pathlib import Path

from cache import CACHE_VERSION
from compat import is_linked_directory, unlink_missing
from errors import ArchiveError, BuildError
from file_publish import publish_new_file
from image_reader import (BaseImage, ImageArchiveReader, archive_members, digest_hex,
                          docker_manifest, image_history, sha256_file)
from image_writer import ImageArchiveWriter
from oci_writer import OCIImageWriter
from reproducible import parse_epoch
from rootfs import RootFSIndex, clean_path


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
        return json.load(stream)


def _identify(path, requested_tag):
    with tarfile.open(path, "r:*") as archive:
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


def load_image(path, workspace, tag=None, with_image=False):
    """验证并准备层；with_image 保留原始配置及来源元数据，供优化器转存使用。

    默认四元组供只读分析使用，禁止将其中的解析配置当作原样转存数据。
    """
    path = Path(path).resolve()
    kind, tag = _identify(path, tag)
    workspace = Path(workspace)
    if kind == "docker":
        image = ImageArchiveReader(path, workspace / "layers").read(tag)
        return (kind, tag, image) if with_image else (kind, tag, image.config, image.layers)
    OCIImageWriter().verify(path, tag)
    with tarfile.open(path, "r:") as archive:
        members = archive_members(archive)
        index = _member_json(archive, "index.json")
        manifest_digest = index["manifests"][0]["digest"]
        manifest = _member_json(archive, "blobs/sha256/" + digest_hex(manifest_digest))
        config_name = "blobs/sha256/" + digest_hex(manifest["config"]["digest"])
        with archive.extractfile(members[config_name]) as stream:
            config_raw = stream.read()
        config = json.loads(config_raw)
        layer_dir = workspace / "layers"
        layer_dir.mkdir(parents=True, exist_ok=True)
        layers = []
        for number, descriptor in enumerate(manifest["layers"]):
            name = "blobs/sha256/" + digest_hex(descriptor["digest"])
            source = archive.extractfile(members[name])
            if source is None:
                raise ArchiveError("Missing OCI layer: " + name)
            target = layer_dir / ("layer-{}.tar".format(number))
            with source, open(target, "wb") as output:
                shutil.copyfileobj(source, output, BUFFER)
            layers.append(target)
    image = BaseImage(config, layers, [tag], manifest, manifest["config"]["digest"], config_raw, index)
    return (kind, tag, image) if with_image else (kind, tag, config, layers)


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


def _signature(index, memo):
    result = []
    for path, entry in sorted(index.entries.items()):
        if entry.kind == "other":
            raise BuildError("Cannot safely optimize an image with special rootfs entries")
        if not entry.layer:
            result.append((path, "implicit-directory"))
            continue
        identity = (entry.layer, entry.member)
        if identity not in memo:
            with tarfile.open(entry.layer, "r:") as archive:
                member = archive.getmember(entry.member)
                content = None
                if member.isfile():
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise ArchiveError("Unreadable visible file: " + path)
                    with stream:
                        content = _hash_stream(stream)
                memo[identity] = (member.type, member.size, member.mode, member.uid,
                                  member.gid, member.mtime, member.linkname,
                                  member.devmajor, member.devminor,
                                  json.dumps(member.pax_headers, sort_keys=True), content)
        result.append((path, entry.kind, memo[identity]))
    return tuple(result)


def analyze_image(path, tag=None, large_mib=50):
    """报告层大小、隐藏字节和重复文件内容；隐藏判断包含硬链接对旧内容的引用。"""
    with tempfile.TemporaryDirectory(prefix="pyimagebuilder-analyze-") as temp:
        kind, tag, config, layers = load_image(path, temp, tag)
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
                "archive_bytes": Path(path).stat().st_size,
                "uncompressed_layer_bytes": sum(item["tar_bytes"] for item in layers_report),
                "hidden_file_bytes": sum(item["hidden_file_bytes"] for item in layers_report),
                "large_layers": large,
                "layers": layers_report, "duplicate_content": duplicates[:50],
                "duplicate_groups": len(duplicates), "recommendations": advice,
                "note": "Duplicate and hidden byte counts are opportunities, not guaranteed savings."}


def optimize_image(path, output, tag=None, source_date_epoch=0):
    """仅移除已证明不影响最终文件树的完整层，并同步重写配置。

    不是文件级压缩；遇到硬链接或特殊文件等无法保守验证的结构时拒绝优化。
    """
    output = Path(output).resolve()
    path = Path(path).resolve()
    if output == path or output.exists() or output.suffix.lower() != ".tar":
        raise BuildError("Optimization output must be a new .tar file")
    epoch = parse_epoch(source_date_epoch)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pyimagebuilder-optimize-", dir=output.parent) as temp:
        kind, tag, image = load_image(path, Path(temp) / "input", tag, with_image=True)
        config, layers = image.config, image.layers
        for layer in layers:
            with tarfile.open(layer, "r:") as archive:
                if any(member.islnk() or member.ischr() or member.isblk() or member.isfifo()
                       for member in archive):
                    raise BuildError("Cannot safely prune layers containing hardlinks or special files")
        memo = {}
        original = _signature(_index(layers), memo)
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
        partial = Path(temp) / "optimized.tar"
        writer = ImageArchiveWriter(epoch) if kind == "docker" else OCIImageWriter(epoch)
        if not removed:
            if kind == "oci":
                # 没有优化发生时，manifest/index 也不能重编码或丢失注解。
                shutil.copyfile(path, partial)
            else:
                writer.write_image(partial, image, tag)
        elif kind == "oci":
            template = copy.deepcopy(image.source_manifest)
            template["layers"] = [template["layers"][item] for item in retained]
            writer.write_new(partial, updated, [layers[item] for item in retained], tag,
                         manifest_template=template, index_template=image.source_index)
        else:
            writer.write_new(partial, updated, [layers[item] for item in retained], tag)
        writer.verify(partial, tag)
        _, _, _, verified_layers = load_image(partial, Path(temp) / "verify", tag)
        if _signature(_index(verified_layers), {}) != original:
            raise BuildError("Optimized image rootfs verification failed")
        original_size = path.stat().st_size
        optimized_size = partial.stat().st_size
        publish_new_file(partial, output)
        return {"output": str(output), "format": kind, "tag": tag,
                "removed_layer_numbers": [item + 1 for item in removed],
                "before_bytes": original_size, "after_bytes": optimized_size,
                "saved_bytes": original_size - optimized_size}


def audit_cache(directory, excluding=()):
    """验证缓存条目，找出缺失、损坏或无引用的层；excluding 用于模拟条目删除。"""
    root = Path(directory).resolve()
    entries_dir = root / "entries"
    layers_dir = root / "layers"
    if is_linked_directory(entries_dir) or is_linked_directory(layers_dir):
        raise BuildError("Cache entries/layers directories must not be symlinks")
    if not entries_dir.is_dir() or not layers_dir.is_dir():
        raise BuildError("Expected a pyimagebuilder cache with entries/ and layers/")
    referenced = set()
    invalid = []
    valid = 0
    excluded = {Path(path).resolve() for path in excluding}
    for path in sorted(entries_dir.glob("*.json")):
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
                if (not layer.is_file() or type(data.get("size")) is not int or
                        layer.stat().st_size != data["size"] or sha256_file(layer) != digest or
                        not tarfile.is_tarfile(layer)):
                    reason = "missing or corrupt layer"
                else:
                    valid += 1
                    referenced.add(layer.name)
        except (OSError, ValueError, TypeError, ArchiveError, tarfile.TarError) as exc:
            reason = "invalid metadata: " + type(exc).__name__
        if reason:
            invalid.append({"path": str(path), "reason": reason, "bytes": path.stat().st_size})
    orphan = [{"path": str(path), "bytes": path.stat().st_size}
              for path in sorted(layers_dir.glob("*.tar")) if path.name not in referenced]
    return {"cache_dir": str(root), "valid_entries": valid, "invalid_entries": invalid,
            "orphan_layers": orphan,
            "reclaimable_bytes": sum(item["bytes"] for item in invalid + orphan)}


def prune_cache(directory):
    """删除审计认定无效的条目和孤儿层；dry_run 只报告计划。"""
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
    image.add_argument("--large-mib", type=float, default=50)
    image.add_argument("--output", type=Path, help="Optional JSON report file")
    optimize = commands.add_parser("optimize", help="Drop layers proven redundant for final rootfs")
    optimize.add_argument("--archive", type=Path, required=True)
    optimize.add_argument("--output", type=Path, required=True)
    optimize.add_argument("--tag")
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
            result = analyze_image(args.archive, args.tag, args.large_mib)
            if args.output:
                if args.output.exists():
                    raise BuildError("Report output already exists")
                args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                                       encoding="utf-8")
        elif args.command == "optimize":
            result = optimize_image(args.archive, args.output, args.tag, args.source_date_epoch)
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
