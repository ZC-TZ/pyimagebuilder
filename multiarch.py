#!/usr/bin/env python3
"""合并两个已验证的单平台 OCI 归档，生成多平台镜像布局。"""

import argparse
import hashlib
import io
import json
import os
import tarfile
import tempfile
from contextlib import ExitStack
from pathlib import Path

from errors import ArchiveError, BuildError
from file_publish import publish_new_file
from image_reader import archive_members, digest_hex
from oci_writer import (CONFIG_TYPE, INDEX_TYPE, LAYER_TYPE, MANIFEST_TYPE,
                        OCIImageWriter, _json_bytes)
from reproducible import parse_epoch


BUFFER = 4 * 1024 * 1024


def _json_member(archive, name, members=None):
    """使用规范化索引读取 JSON，避免校验与消费对 ./ 路径别名解释不一致。"""
    try:
        member = (archive_members(archive) if members is None else members)[name]
    except KeyError as exc:
        raise ArchiveError("Missing OCI member: " + name) from exc
    if not member.isfile() or member.size > 16 * 1024 * 1024:
        raise ArchiveError("Invalid or oversized OCI JSON: " + name)
    stream = archive.extractfile(member)
    if stream is None:
        raise ArchiveError("Unreadable OCI JSON: " + name)
    with stream:
        try:
            return json.load(stream)
        except (ValueError, UnicodeError) as exc:
            raise ArchiveError("Invalid OCI JSON: " + name) from exc


def _add_bytes(archive, name, raw, epoch):
    info = tarfile.TarInfo(name)
    info.mode = 0o644
    info.mtime = epoch
    info.size = len(raw)
    archive.addfile(info, io.BytesIO(raw))


def _source(path, expected_architecture):
    with tarfile.open(path, "r:") as archive:
        index = _json_member(archive, "index.json")
        entries = index.get("manifests") if isinstance(index, dict) else None
        if not isinstance(entries, list) or len(entries) != 1:
            raise ArchiveError("Input OCI archive must contain one platform")
        entry = entries[0]
        if not isinstance(entry, dict) or not isinstance(entry.get("annotations"), dict):
            raise ArchiveError("Invalid input OCI manifest descriptor")
        if entry.get("platform") != {"os": "linux", "architecture": expected_architecture}:
            raise ArchiveError("Input OCI platform is not linux/" + expected_architecture)
        tag = entry.get("annotations", {}).get("org.opencontainers.image.ref.name")
        if not isinstance(tag, str):
            raise ArchiveError("Input OCI archive has no tag")
    OCIImageWriter().verify(path, tag)
    return entry


def combine(amd64_archive, arm64_archive, output, tag, source_date_epoch=0):
    """合并 amd64 与 arm64 归档，生成确定性的多平台 OCI index tar。"""
    epoch = parse_epoch(source_date_epoch)
    output = Path(output).resolve()
    if output.suffix.lower() != ".tar" or output.exists():
        raise BuildError("Multi-platform output must be a new .tar file")
    paths = [Path(amd64_archive).resolve(), Path(arm64_archive).resolve()]
    if paths[0] == paths[1] or output in paths:
        raise BuildError("Multi-platform input and output paths must differ")
    entries = [_source(paths[0], "amd64"), _source(paths[1], "arm64")]
    for entry in entries:
        entry["annotations"].update({"org.opencontainers.image.ref.name": tag,
                                     "io.containerd.image.name": tag})
    index_raw = _json_bytes({"schemaVersion": 2, "mediaType": INDEX_TYPE,
                             "manifests": entries})
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pyimagebuilder-multiarch-", dir=output.parent) as temp:
        partial = Path(temp) / "multiarch.tar"
        with ExitStack() as stack:
            sources = [stack.enter_context(tarfile.open(path, "r:")) for path in paths]
            blobs = {}
            for source in sources:
                for name, member in archive_members(source).items():
                    if name.startswith("blobs/sha256/"):
                        previous = blobs.get(name)
                        if previous is not None and previous[1].size != member.size:
                            raise ArchiveError("Conflicting duplicate OCI blob")
                        blobs.setdefault(name, (source, member))
            with tarfile.open(partial, "w", format=tarfile.PAX_FORMAT) as target:
                _add_bytes(target, "oci-layout", _json_bytes({"imageLayoutVersion": "1.0.0"}), epoch)
                _add_bytes(target, "index.json", index_raw, epoch)
                for name, (source, member) in sorted(blobs.items()):
                    info = tarfile.TarInfo(name)
                    info.mode = 0o644
                    info.mtime = epoch
                    info.size = member.size
                    stream = source.extractfile(member)
                    if stream is None:
                        raise ArchiveError("Unreadable OCI blob: " + name)
                    with stream:
                        target.addfile(info, stream)
        verify(partial, tag)
        publish_new_file(partial, output)
    return output


def verify(path, expected_tag):
    """验证合并归档中的平台描述及其引用的全部 blob。"""
    try:
        with tarfile.open(path, "r:") as archive:
            members = archive_members(archive)
            if any(not member.isfile() for member in members.values()):
                raise ArchiveError("Non-file OCI member")
            if _json_member(archive, "oci-layout", members) != {"imageLayoutVersion": "1.0.0"}:
                raise ArchiveError("Invalid OCI layout marker")
            index = _json_member(archive, "index.json", members)
            entries = index.get("manifests") if isinstance(index, dict) else None
            if (not isinstance(index, dict) or index.get("mediaType") != INDEX_TYPE or
                    index.get("schemaVersion") != 2 or
                    not isinstance(entries, list) or len(entries) != 2):
                raise ArchiveError("Expected a two-platform OCI index")
            if [item.get("platform") if isinstance(item, dict) else None for item in entries] != [
                    {"os": "linux", "architecture": "amd64"},
                    {"os": "linux", "architecture": "arm64"}]:
                raise ArchiveError("OCI index must contain amd64 then arm64")
            expected_members = {"oci-layout", "index.json"}
            def check(descriptor, media_type):
                if (not isinstance(descriptor, dict) or descriptor.get("mediaType") != media_type or
                        type(descriptor.get("size")) is not int or descriptor["size"] < 0):
                    raise ArchiveError("Invalid OCI descriptor")
                name = "blobs/sha256/" + digest_hex(descriptor.get("digest"))
                member = members.get(name)
                if member is None or member.size != descriptor["size"]:
                    raise ArchiveError("OCI blob size mismatch: " + name)
                stream = archive.extractfile(member)
                if stream is None:
                    raise ArchiveError("Unreadable OCI blob: " + name)
                hasher = hashlib.sha256()
                with stream:
                    for block in iter(lambda: stream.read(BUFFER), b""):
                        hasher.update(block)
                if hasher.hexdigest() != digest_hex(descriptor["digest"]):
                    raise ArchiveError("OCI blob digest mismatch: " + name)
                expected_members.add(name)
                return name
            for entry in entries:
                if (not isinstance(entry, dict) or
                        not isinstance(entry.get("annotations"), dict) or
                        entry["annotations"].get("org.opencontainers.image.ref.name") != expected_tag):
                    raise ArchiveError("OCI index tag mismatch")
                manifest = _json_member(archive, check(entry, MANIFEST_TYPE), members)
                if (not isinstance(manifest, dict) or manifest.get("mediaType") != MANIFEST_TYPE or
                        manifest.get("schemaVersion") != 2):
                    raise ArchiveError("Invalid OCI image manifest")
                config = _json_member(archive, check(manifest.get("config"), CONFIG_TYPE), members)
                if (not isinstance(config, dict) or config.get("os") != "linux" or
                        config.get("architecture") != entry["platform"]["architecture"]):
                    raise ArchiveError("OCI config platform mismatch")
                layers = manifest.get("layers")
                rootfs = config.get("rootfs")
                diff_ids = rootfs.get("diff_ids") if isinstance(rootfs, dict) else None
                if not isinstance(layers, list) or not isinstance(diff_ids, list) or len(layers) != len(diff_ids):
                    raise ArchiveError("OCI layer count mismatch")
                for layer, diff_id in zip(layers, diff_ids):
                    if not isinstance(layer, dict) or layer.get("digest") != diff_id:
                        raise ArchiveError("OCI layer DiffID mismatch")
                    check(layer, LAYER_TYPE)
            if set(members) != expected_members:
                raise ArchiveError("Unexpected OCI layout members")
    except (tarfile.TarError, KeyError, TypeError, ValueError) as exc:
        raise ArchiveError("Invalid multi-platform OCI archive: " + str(exc)) from exc


def main(argv=None):
    """独立脚本入口：创建和验证多平台镜像归档。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--amd64", type=Path, required=True, help="Single-platform amd64 OCI tar")
    parser.add_argument("--arm64", type=Path, required=True, help="Single-platform arm64 OCI tar")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--source-date-epoch", default=os.environ.get("SOURCE_DATE_EPOCH", "0"))
    args = parser.parse_args(argv)
    try:
        result = combine(args.amd64, args.arm64, args.output, args.tag, args.source_date_epoch)
    except (BuildError, OSError) as exc:
        parser.exit(1, "Multi-platform build failed: {}\n".format(exc))
    print("Created {}".format(result))
    return 0


if __name__ == "__main__":
    main()
