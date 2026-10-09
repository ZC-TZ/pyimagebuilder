#!/usr/bin/env python3
"""无需 Docker，检查、验证、重新标记并管理镜像归档与本地存储。"""

import argparse
import io
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
import time
from pathlib import Path

from errors import ArchiveError, BuildError
from cas_store import CASStore, docker_archive_tag
from compat import is_linked_directory, remove_suffix, unlink_missing
from image_reader import (MAX_JSON, ImageArchiveReader, archive_members, docker_manifest,
                          image_history, outer_name, sha256_file)
from image_store import _archive_path, _check_archive_platform, default_store, locate_archive
from optimizer import audit_cache, prune_cache
from registry import RegistryClient, push
from settings import cache_directory, load_settings, repository_settings


def _manifest(archive):
    try:
        with tarfile.open(archive, "r:*") as source:
            member = archive_members(source)["manifest.json"]
            if not member.isfile() or member.size > MAX_JSON:
                raise ArchiveError("Missing or oversized manifest.json")
            stream = source.extractfile(member)
            if stream is None:
                raise ArchiveError("Missing manifest.json")
            with stream:
                manifest = json.load(stream)
            return docker_manifest(manifest)
    except (OSError, tarfile.TarError, ValueError, TypeError, KeyError,
            UnicodeDecodeError) as exc:
        raise ArchiveError("Cannot read Docker archive: " + str(exc)) from exc


def _select_tag(archive, tag=None):
    """选取精确标签；同一标签的平台选择交给完整归档读取器。"""
    manifest = _manifest(archive)
    if tag is None:
        tags = {value for item in manifest for value in (item.get("RepoTags") or [])}
        if len(tags) != 1:
            raise BuildError("Archive has multiple images/tags; specify --tag")
        tag = next(iter(tags))
    matches = [item for item in manifest if tag in (item.get("RepoTags") or [])]
    if not matches:
        raise BuildError("Archive does not contain tag " + tag)
    return tag


def _read(archive, tag=None, platform=None):
    tag = _select_tag(archive, tag)
    with tempfile.TemporaryDirectory(prefix="pyimagebuilder-inspect-") as temporary:
        image = ImageArchiveReader(archive, Path(temporary)).read(tag, platform)
        sizes = [path.stat().st_size for path in image.layers]
    return tag, image.source_manifest, image.config, sizes, image.config_digest


def inspect_archive(archive, tag=None, platform=None):
    """返回选定 Docker 归档的运行配置、平台、标签和层大小。"""
    selected, entry, config, sizes, config_digest = _read(archive, tag, platform)
    return {"archive": str(Path(archive).resolve()), "tags": entry.get("RepoTags") or [],
            "selected_tag": selected, "image_id": config_digest,
            "platform": config["os"] + "/" + config["architecture"],
            "created": config.get("created"), "config": config.get("config") or {},
            "rootfs_diff_ids": config["rootfs"]["diff_ids"],
            "layers": [{"name": name, "size": size} for name, size in
                       zip(entry["Layers"], sizes)], "archive_bytes": Path(archive).stat().st_size}


def history_archive(archive, tag=None, platform=None):
    """返回历史与层大小；无原始历史时明确报告不可用，不推测构建命令。"""
    selected, _, config, sizes, _ = _read(archive, tag, platform)
    history = image_history(config, len(sizes))
    if not history:
        return {"tag": selected, "history": [], "history_available": False,
                "unrecorded_layers": len(sizes)}
    rows = []
    layer = 0
    for item in history:
        if not isinstance(item, dict):
            raise ArchiveError("Invalid history entry")
        empty = item.get("empty_layer", False)
        size = 0 if empty else sizes[layer] if layer < len(sizes) else None
        if size is None:
            raise ArchiveError("Image history has more layers than rootfs")
        if not empty:
            layer += 1
        rows.append({"created": item.get("created"), "created_by": item.get("created_by"),
                     "comment": item.get("comment"), "size": size, "empty_layer": bool(empty)})
    if layer != len(sizes):
        raise ArchiveError("Image history has fewer layers than rootfs")
    return {"tag": selected, "history": list(reversed(rows)),
            "history_available": True, "unrecorded_layers": 0}


def verify_archive(archive, tag=None, platform=None):
    """验证选定 Docker 归档，并报告整个归档文件的 SHA-256。"""
    selected, entry, config, sizes, _ = _read(archive, tag, platform)
    return {"archive": str(Path(archive).resolve()), "tag": selected,
            "platform": config["os"] + "/" + config["architecture"],
            "config": entry["Config"], "layers_verified": len(sizes),
            "layer_bytes_verified": sum(sizes), "sha256": sha256_file(archive)}


def list_images(store=None):
    """列出镜像库中的 tar 与 CAS 引用；无法读取的归档作为错误条目报告。"""
    root = Path(store or default_store()).resolve()
    result = []
    if not root.exists():
        return {"image_store": str(root), "images": result}
    for path in sorted(root.glob("*.tar")):
        try:
            for entry in _manifest(path):
                with tarfile.open(path, "r:*") as source:
                    config_member = archive_members(source)[outer_name(entry["Config"])]
                    if not config_member.isfile() or config_member.size > MAX_JSON:
                        raise ArchiveError("Missing or oversized image config")
                    with source.extractfile(config_member) as stream:
                        config = json.load(stream)
                    if not isinstance(config, dict):
                        raise ArchiveError("Image config is not an object")
                result.append({"tags": entry.get("RepoTags") or [],
                               "platform": str(config.get("os")) + "/" + str(config.get("architecture")),
                               "archive": str(path), "bytes": path.stat().st_size})
        except (ArchiveError, OSError, KeyError, TypeError, ValueError, tarfile.TarError) as exc:
            result.append({"archive": str(path), "error": str(exc)})
    for ref in CASStore(root).list_refs():
        try:
            path = _archive_path(root, ref["reference"], ref["platform"], ref["source"])
            digest_alias = docker_archive_tag(ref["reference"]) != ref["reference"]
            if not path.is_file() or digest_alias:
                result.append({"tags": [ref["reference"]], "platform": ref["platform"],
                               "source": ref["source"], "storage": "cas",
                               "archive": str(path) if path.is_file() else None,
                               "bytes": 0 if path.is_file() else
                                        sum(item["size"] for item in ref["layers"])})
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            result.append({"storage": "cas", "error": "Invalid CAS ref: " + str(exc)})
    return {"image_store": str(root), "images": result}


def load_archive(archive, store=None, tag=None, platform=None, replace=False):
    """验证 Docker tar，按选定标签和平台导入本地来源。

    保存 tar 适配文件和去重的 CAS ref；同名本地标签内容变化时要求 replace=True。
    Registry、Artifactory 来源的同名缓存独立存在，不受本次导入覆盖。
    本地来源引用锁覆盖冲突判断、tar/ref 发布与失败恢复；竞争者立即收到 busy。
    """
    archive = Path(archive).resolve()
    selected, _, config, _, _ = _read(archive, tag, platform)
    platform = config["os"] + "/" + config["architecture"]
    digest = sha256_file(archive)
    root = Path(store or default_store()).resolve()
    root.mkdir(parents=True, exist_ok=True)
    cas = CASStore(root)
    with cas.ref_lock(selected, platform, "local"):
        return _load_archive_locked(archive, root, cas, selected, platform, digest, replace)


def _load_archive_locked(archive, root, cas, selected, platform, digest, replace):
    """持锁更新 tar 与 CAS ref；归档发布失败时，旧快照不会覆盖并发更新者。"""
    target = _archive_path(root, selected, platform, "local")
    if not target.exists() and cas.has_ref(selected, platform, "local") and not replace:
        raise BuildError("Stored tag conflicts with a CAS-only image; use --replace: " + selected)
    if target == archive:
        cas.import_docker(archive, selected, platform, "local", replace=replace)
        return {"tag": selected, "platform": platform, "archive": str(target), "status": "already stored"}
    if target.exists():
        if sha256_file(target) == digest:
            cas.import_docker(target, selected, platform, "local", replace=replace)
            return {"tag": selected, "platform": platform, "archive": str(target), "status": "already stored"}
        if not replace:
            raise BuildError("Stored tag conflicts; use --replace to change it: " + selected)
    pending = target.with_name(target.name + ".part")
    if pending.exists():
        raise BuildError("Incomplete image import already exists: " + str(pending))
    previous_ref = cas.snapshot_ref(selected, platform, "local")
    created = False
    ref_published = False
    try:
        with open(archive, "rb") as source, open(pending, "xb") as output:
            created = True
            shutil.copyfileobj(source, output, 4 * 1024 * 1024)
        if sha256_file(pending) != digest:
            raise ArchiveError("Imported archive changed while copying")
        cas.import_docker(pending, selected, platform, "local", replace=True)
        ref_published = True
        os.replace(pending, target)
    except Exception:
        # 创建失败或复制失败时，本次尚未改变 ref，不能回滚别人的导入。
        if ref_published:
            cas.restore_ref(selected, platform, "local", previous_ref)
        raise
    finally:
        if created:
            unlink_missing(pending)
    return {"tag": selected, "platform": platform, "archive": str(target), "sha256": digest}


def save_archive(reference, output, store=None, platform="linux/amd64", source=None):
    """将指定存储镜像导出到新路径；需要时由 CAS 生成 Docker tar。"""
    kind, archive = locate_archive(store, reference, platform, source)
    output = Path(output).resolve()
    if output.exists() or output == archive:
        raise BuildError("Save output must be a new, distinct file")
    output.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        with open(archive, "rb") as source_stream, open(output, "xb") as target:
            created = True
            shutil.copyfileobj(source_stream, target, 4 * 1024 * 1024)
    except Exception:
        # 退出 with 后句柄才关闭，Windows 此时才能删除残片。
        # 排他创建失败时不能删除别的进程已创建的同名输出。
        if created:
            unlink_missing(output)
        raise
    return {"tag": reference, "platform": platform, "source": kind, "output": str(output)}


def remove_image(reference, store=None, platform="linux/amd64", source=None):
    """持引用锁删除归档及 ref；共享 blob 留给显式 prune 回收。

    删除 tar 失败时恢复 ref，避免归档仍在却丢失构建索引。
    不在 rmi 中执行全库 GC，以免误删其他导入尚未发布引用的 blob。
    """
    root = Path(store or default_store()).resolve()
    cas = CASStore(root)
    matches = []
    for kind in ((source,) if source else ("registry", "artifactory", "local")):
        archive = _archive_path(root, reference, platform, kind)
        if archive.is_file():
            _check_archive_platform(archive, reference, platform)
            matches.append((kind, archive))
        elif cas.has_ref(reference, platform, kind):
            cas.resolve(reference, platform, kind)
            matches.append((kind, None))
    if not matches:
        raise BuildError("Image not present in PyImageBuilder store: {} ({})".format(reference, platform))
    if len(matches) > 1:
        raise BuildError("Multiple stored sources for {}; specify --source".format(reference))
    kind, _ = matches[0]
    with cas.ref_lock(reference, platform, kind):
        # 来源选择后可能发生更新；持锁重新确认本次实际删除的对象。
        archive = _archive_path(root, reference, platform, kind)
        has_archive = archive.is_file()
        if has_archive:
            _check_archive_platform(archive, reference, platform)
        elif not cas.has_ref(reference, platform, kind):
            raise BuildError("Image not present in PyImageBuilder store: " + reference)
        if cas.has_ref(reference, platform, kind):
            cas.resolve(reference, platform, kind)
        previous_ref = cas.snapshot_ref(reference, platform, kind)
        cas.remove_ref(reference, platform, kind)
        try:
            if has_archive:
                archive.unlink()
        except Exception:
            cas.restore_ref(reference, platform, kind, previous_ref)
            raise
    return {"removed": reference, "platform": platform, "source": kind}


def prune_images(store=None, older_than_days=None, dry_run=False):
    """删除或预览超过指定年龄的存储镜像，包括有标签镜像。"""
    if older_than_days is None or older_than_days < 0:
        raise BuildError("image prune requires --older-than DAYS >= 0")
    root = Path(store or default_store()).resolve()
    threshold = time.time() - older_than_days * 86400
    candidates = []
    for path in sorted(root.glob("*.tar")) if root.is_dir() else []:
        if path.is_symlink():
            continue
        if path.stat().st_mtime < threshold:
            candidates.append({"path": str(path), "bytes": path.stat().st_size})
    cas = CASStore(root)
    refs = cas.list_refs()
    for ref in refs:
        if not all(key in ref for key in ("reference", "platform", "source")):
            continue
        archive = _archive_path(root, ref["reference"], ref["platform"], ref["source"])
        ref_path = cas._ref(ref["reference"], ref["platform"], ref["source"])
        if not archive.is_file() and ref_path.stat().st_mtime < threshold:
            candidates.append({"path": str(ref_path), "bytes": 0, "storage": "cas"})
    selected_paths = {Path(item["path"]) for item in candidates}
    refs_to_remove = {cas._ref(ref["reference"], ref["platform"], ref["source"])
                      for ref in refs if all(key in ref for key in
                                             ("reference", "platform", "source")) and
                      (_archive_path(root, ref["reference"], ref["platform"], ref["source"])
                       in selected_paths or
                       cas._ref(ref["reference"], ref["platform"], ref["source"])
                       in selected_paths)}
    tar_bytes = sum(item["bytes"] for item in candidates)
    cas_bytes = (cas.prune_orphans(dry_run=True, excluding=refs_to_remove)["reclaimable_bytes"]
                 if candidates else 0)
    if not dry_run:
        for item in candidates:
            path = Path(item["path"])
            unlink_missing(path)
        for ref_path in refs_to_remove:
            unlink_missing(ref_path)
        if candidates:
            cas.prune_orphans()
    return {"image_store": str(root), "older_than_days": older_than_days,
            "dry_run": dry_run, "candidates": candidates,
            "reclaimable_bytes": tar_bytes + cas_bytes}


def disk_usage(store=None, cache_dir=None):
    """统计镜像库和指令缓存的普通文件及字节，避免硬链接重复计数。"""
    image_root = Path(store or default_store()).resolve()
    cache_root = Path(cache_dir).resolve()
    def count(root):
        seen = set()
        total = files = 0
        if root.is_dir():
            for path in root.rglob("*"):
                if not path.is_file() or path.is_symlink():
                    continue
                data = path.stat()
                key = data.st_dev, data.st_ino
                if key in seen:
                    continue
                seen.add(key)
                total += data.st_size
                files += 1
        return {"path": str(root), "files": files, "bytes": total}
    return {"image_store": count(image_root), "layer_cache": count(cache_root)}


def prune_old_cache(directory, older_than_days, dry_run=False):
    """删除过期指令条目，再清理已无有效引用的层；预览按删除后的引用计算。"""
    if older_than_days < 0:
        raise BuildError("--older-than must be nonnegative")
    root = Path(directory).resolve()
    if is_linked_directory(root / "entries") or is_linked_directory(root / "layers"):
        raise BuildError("Cache entries/layers directories must not be symlinks")
    if not (root / "entries").is_dir() or not (root / "layers").is_dir():
        raise BuildError("Expected a pyimagebuilder cache with entries/ and layers/")
    threshold = time.time() - older_than_days * 86400
    entries = [path for path in sorted((root / "entries").glob("*.json"))
               if not path.is_symlink() and path.stat().st_mtime < threshold]
    expired_bytes = sum(path.stat().st_size for path in entries)
    if not dry_run:
        for path in entries:
            path.unlink()
        cleanup = prune_cache(root)
    else:
        cleanup = audit_cache(root, excluding=entries)
    return {"cache_dir": str(root), "expired_entries": [str(path) for path in entries],
            "dry_run": dry_run, "cleanup": cleanup,
            "reclaimable_bytes": expired_bytes + cleanup["reclaimable_bytes"]}


_TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}\Z")
_REPO = re.compile(r"[a-z0-9][a-z0-9._:/-]*\Z")


def _check_tag(tag):
    if not isinstance(tag, str) or ":" not in tag or tag.rfind(":") <= tag.rfind("/"):
        raise BuildError("New image reference must include repository:tag")
    repository, version = tag.rsplit(":", 1)
    if (not _REPO.fullmatch(repository) or not _TAG.fullmatch(version) or
            "//" in repository or ".." in repository or repository.endswith("/")):
        raise BuildError("Invalid new image reference")


def tag_archive(archive, new_tag, output, source_tag=None):
    """生成替换标签后的单镜像 Docker tar，保留输入归档。"""
    _check_tag(new_tag)
    archive, output = Path(archive).resolve(), Path(output).resolve()
    if output.exists() or output == archive:
        raise BuildError("Tag output must be a new, distinct file")
    if output.suffix.lower() != ".tar":
        raise BuildError("Tag output must end in .tar")
    # 改写标签前先验证源归档，避免畸形 manifest 触发未处理异常
    # 或被复制成新的无效归档。
    selected, entry, _, _, _ = _read(archive, source_tag)
    if len(_manifest(archive)) != 1:
        raise BuildError("Retag currently requires a single-image Docker archive")
    manifest = [{**entry, "RepoTags": [new_tag]}]
    repository, version = new_tag.rsplit(":", 1)
    top = entry["Layers"][-1].split("/", 1)[0] if entry["Layers"] else remove_suffix(entry["Config"], ".json")
    replacements = {"manifest.json": json.dumps(manifest, separators=(",", ":")).encode(),
                    "repositories": json.dumps({repository: {version: top}}, separators=(",", ":")).encode()}
    created = False
    try:
        with tarfile.open(archive, "r:*") as source:
            with open(output, "xb") as sink:
                created = True
                with tarfile.open(fileobj=sink, mode="w", format=tarfile.PAX_FORMAT) as target:
                    seen = set()
                    for member in source:
                        name = outer_name(member.name)
                        if name in seen:
                            raise ArchiveError("Duplicate archive member")
                        seen.add(name)
                        if name in replacements:
                            raw = replacements.pop(name)
                            member.size = len(raw)
                            target.addfile(member, io.BytesIO(raw))
                        else:
                            target.addfile(member, source.extractfile(member) if member.isfile() else None)
                    for name, raw in replacements.items():
                        member = tarfile.TarInfo(name)
                        member.size = len(raw)
                        target.addfile(member, io.BytesIO(raw))
        verify_archive(output, new_tag)
    except Exception:
        if created:
            unlink_missing(output)
        raise
    return {"source_tag": selected, "tag": new_tag, "output": str(output)}


def inspect_manifest(reference, username=None, password_env="PYIMAGEBUILDER_REGISTRY_PASSWORD",
                     ca_file=None, insecure_http=False, auth_host=None):
    """只获取并验证 Registry manifest/index，不下载镜像层。"""
    password = os.environ.get(password_env)
    client = RegistryClient(reference, username, password, ca_file, insecure_http, auth_host)
    value, media_type, digest, size = client.manifest(client.reference.version)
    return {"reference": client.reference.full, "digest": digest, "media_type": media_type,
            "size": size, "manifest": value}


def main(argv=None):
    """main.py 与独立脚本共用的镜像检查、存储、缓存和 Registry 命令入口。"""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("inspect", "verify", "history"):
        command = commands.add_parser(name, help=name.capitalize() + " a Docker save archive")
        command.add_argument("archive", type=Path)
        command.add_argument("--tag")
        command.add_argument("--platform", choices=("linux/amd64", "linux/arm64"))
    images = commands.add_parser("images", help="List archives in this tool's image store")
    images.add_argument("--image-store", type=Path)
    images.add_argument("--config", type=Path)
    load = commands.add_parser("load", help="Import Docker tar into PyImageBuilder image store")
    load.add_argument("-i", "--input", type=Path, required=True)
    load.add_argument("--tag")
    load.add_argument("--platform", choices=("linux/amd64", "linux/arm64"))
    load.add_argument("--image-store", type=Path)
    load.add_argument("--config", type=Path)
    load.add_argument("--replace", action="store_true")
    save = commands.add_parser("save", help="Export a stored image to Docker tar")
    save.add_argument("reference")
    save.add_argument("-o", "--output", type=Path, required=True)
    save.add_argument("--platform", choices=("linux/amd64", "linux/arm64"), default="linux/amd64")
    save.add_argument("--source", choices=("local", "registry", "artifactory"))
    save.add_argument("--image-store", type=Path)
    save.add_argument("--config", type=Path)
    remove = commands.add_parser("rmi", help="Remove an image from PyImageBuilder image store")
    remove.add_argument("reference")
    remove.add_argument("--platform", choices=("linux/amd64", "linux/arm64"), default="linux/amd64")
    remove.add_argument("--source", choices=("local", "registry", "artifactory"))
    remove.add_argument("--image-store", type=Path)
    remove.add_argument("--config", type=Path)
    image = commands.add_parser("image", help="Manage PyImageBuilder stored images")
    image_actions = image.add_subparsers(dest="image_action", required=True)
    image_prune = image_actions.add_parser("prune", help="Remove aged stored image archives")
    image_prune.add_argument("--older-than", type=float, required=True, metavar="DAYS")
    image_prune.add_argument("--dry-run", action="store_true")
    image_prune.add_argument("--image-store", type=Path)
    image_prune.add_argument("--config", type=Path)
    system = commands.add_parser("system", help="Show PyImageBuilder disk usage")
    system_actions = system.add_subparsers(dest="system_action", required=True)
    system_df = system_actions.add_parser("df")
    system_df.add_argument("--image-store", type=Path)
    system_df.add_argument("--cache-dir", type=Path)
    system_df.add_argument("--config", type=Path)
    cache = commands.add_parser("cache", help="Audit or prune this tool's layer cache")
    cache_actions = cache.add_subparsers(dest="cache_action", required=True)
    for name in ("ls", "prune"):
        action = cache_actions.add_parser(name)
        action.add_argument("--cache-dir", type=Path)
        action.add_argument("--config", type=Path)
        if name == "prune":
            action.add_argument("--older-than", type=float, metavar="DAYS")
            action.add_argument("--dry-run", action="store_true")
    tagging = commands.add_parser("tag", help="Retag a Docker save archive into a new tar")
    tagging.add_argument("archive", type=Path)
    tagging.add_argument("new_tag")
    tagging.add_argument("-o", "--output", type=Path, required=True)
    tagging.add_argument("--source-tag")
    remote = commands.add_parser("manifest", help="Inspect, create, or push a multi-platform manifest")
    remote_actions = remote.add_subparsers(dest="manifest_action", required=True)
    remote_inspect = remote_actions.add_parser("inspect")
    remote_inspect.add_argument("reference")
    remote_create = remote_actions.add_parser("create", help="Combine amd64 and arm64 OCI archives")
    remote_create.add_argument("--amd64", type=Path, required=True)
    remote_create.add_argument("--arm64", type=Path, required=True)
    remote_create.add_argument("-o", "--output", type=Path, required=True)
    remote_create.add_argument("-t", "--tag", required=True)
    remote_create.add_argument("--source-date-epoch", default="0")
    remote_push = remote_actions.add_parser("push", help="Push a multi-platform OCI archive")
    remote_push.add_argument("archive", type=Path)
    remote_push.add_argument("reference")
    remote_push.add_argument("--overwrite", action="store_true")
    for item in (remote_inspect, remote_push):
        item.add_argument("--config", type=Path)
        item.add_argument("--username")
        item.add_argument("--password-env")
        item.add_argument("--ca-file", type=Path)
        item.add_argument("--insecure-http", action="store_true")
        item.add_argument("--auth-host")
    upload = commands.add_parser("push", help="Push a local archive to an OCI Registry")
    upload.add_argument("archive", type=Path)
    upload.add_argument("reference")
    upload.add_argument("--username")
    upload.add_argument("--config", type=Path)
    upload.add_argument("--password-env")
    upload.add_argument("--ca-file", type=Path)
    upload.add_argument("--insecure-http", action="store_true")
    upload.add_argument("--auth-host")
    upload.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    try:
        settings, _ = load_settings(getattr(args, "config", None))
        connection = repository_settings(settings, args.reference) if (
            args.command == "push" or args.command == "manifest" and
            args.manifest_action in ("inspect", "push")) else {}
        if args.command == "inspect":
            result = inspect_archive(args.archive, args.tag, args.platform)
        elif args.command == "verify":
            result = verify_archive(args.archive, args.tag, args.platform)
        elif args.command == "history":
            result = history_archive(args.archive, args.tag, args.platform)
        elif args.command == "images":
            result = list_images(args.image_store or cache_directory(settings, "imageStore", None))
        elif args.command == "load":
            result = load_archive(args.input, args.image_store or cache_directory(settings, "imageStore", None),
                                  args.tag, args.platform, args.replace)
        elif args.command == "save":
            result = save_archive(args.reference, args.output,
                                  args.image_store or cache_directory(settings, "imageStore", None),
                                  args.platform, args.source)
        elif args.command == "rmi":
            result = remove_image(args.reference,
                                  args.image_store or cache_directory(settings, "imageStore", None),
                                  args.platform, args.source)
        elif args.command == "image":
            result = prune_images(args.image_store or cache_directory(settings, "imageStore", None),
                                  args.older_than, args.dry_run)
        elif args.command == "system":
            result = disk_usage(args.image_store or cache_directory(settings, "imageStore", None),
                                args.cache_dir or cache_directory(settings, "layerCache",
                                    Path(tempfile.gettempdir()) / "pyimagebuilder-layer-cache"))
        elif args.command == "cache":
            directory = args.cache_dir or cache_directory(
                settings, "layerCache", Path(tempfile.gettempdir()) / "pyimagebuilder-layer-cache")
            if args.cache_action == "ls":
                result = audit_cache(directory)
            elif args.older_than is not None:
                result = prune_old_cache(directory, args.older_than, args.dry_run)
            elif args.dry_run:
                result = audit_cache(directory)
            else:
                result = prune_cache(directory)
        elif args.command == "tag":
            result = tag_archive(args.archive, args.new_tag, args.output, args.source_tag)
        elif args.command == "manifest":
            if args.manifest_action == "create":
                from multiarch import combine
                output = combine(args.amd64, args.arm64, args.output, args.tag,
                                 args.source_date_epoch)
                result = {"tag": args.tag, "output": str(output), "platforms":
                          ["linux/amd64", "linux/arm64"]}
            elif args.manifest_action == "inspect":
                result = inspect_manifest(args.reference,
                                          args.username or connection.get("username"),
                                          args.password_env if args.password_env is not None else connection.get(
                                              "passwordEnv", "PYIMAGEBUILDER_REGISTRY_PASSWORD"),
                                          args.ca_file or connection.get("caFile"),
                                          args.insecure_http or connection.get("insecureHttp", False),
                                          args.auth_host or connection.get("authHost"))
            else:
                from registry import push_index
                password_env = args.password_env or connection.get(
                    "passwordEnv", "PYIMAGEBUILDER_REGISTRY_PASSWORD")
                result = {"reference": args.reference, "digest": push_index(
                    args.archive, args.reference, args.username or connection.get("username"),
                    os.environ.get(password_env), args.ca_file or connection.get("caFile"),
                    args.insecure_http or connection.get("insecureHttp", False),
                    args.auth_host or connection.get("authHost"), args.overwrite)}
        else:
            password_env = args.password_env or connection.get(
                "passwordEnv", "PYIMAGEBUILDER_REGISTRY_PASSWORD")
            password = os.environ.get(password_env)
            result = {"reference": args.reference, "digest": push(
                args.archive, args.reference, args.username or connection.get("username"),
                password, args.ca_file or connection.get("caFile"),
                args.insecure_http or connection.get("insecureHttp", False),
                args.auth_host or connection.get("authHost"), args.overwrite)}
    except (ArchiveError, BuildError, OSError, tarfile.TarError, ValueError, TypeError) as exc:
        print("Image command failed: {}".format(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
