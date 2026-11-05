"""历史归档的状态、校验下载和只读磁盘统计。"""

import hashlib
import hmac
import os
import re
import stat
import tempfile
import time
from pathlib import Path

from build_records import list_builds
from cas_store import CASStore
from image_cli import disk_usage, prune_images, prune_old_cache
from image_store import default_store
from settings import cache_directory
from web_jobs import file_stamp, open_artifact


SHA256 = re.compile(r"sha256:[a-f0-9]{64}\Z")


def artifact_state(item, *, suffixes=(".tar",)):
    """列表只检查路径和大小；点击下载时才读取文件内容。"""
    path = Path(item["path"])
    if not path.is_absolute() or not path.name.lower().endswith(suffixes):
        return "unsafe", None
    try:
        info = path.lstat()
    except FileNotFoundError:
        return "missing", None
    except (OSError, ValueError):
        return "unreadable", None
    if not stat.S_ISREG(info.st_mode):
        return "unsafe", None
    size, digest = item.get("bytes"), item.get("sha256")
    if type(size) is int and info.st_size != size:
        return "changed", info
    if type(size) is not int or not isinstance(digest, str) or not SHA256.fullmatch(digest):
        return "unverified", info
    return "available", info


def verified_artifact(item, *, suffixes=(".tar",)):
    """核对台账摘要后复用同一个文件句柄，避免重新按路径打开另一份文件。"""
    status, _ = artifact_state(item, suffixes=suffixes)
    if status != "available":
        return status, None
    stream = None
    try:
        stream = open_artifact(Path(item["path"]))
        start = os.fstat(stream.fileno())
        if not stat.S_ISREG(start.st_mode) or start.st_size != item["bytes"]:
            return "changed", None
        digest = hashlib.sha256()
        remaining = start.st_size
        while remaining:
            block = stream.read(min(4 * 1024 * 1024, remaining))
            if not block:
                return "changed", None
            digest.update(block)
            remaining -= len(block)
        end = os.fstat(stream.fileno())
        if (file_stamp(start) != file_stamp(end) or
                not hmac.compare_digest("sha256:" + digest.hexdigest(), item["sha256"])):
            return "changed", None
        stream.seek(0)
        result, stream = stream, None
        return "available", result
    except (OSError, ValueError):
        return "unreadable", None
    finally:
        if stream is not None:
            stream.close()


def _outside_managed(path, roots):
    try:
        canonical = path.resolve()
    except (OSError, RuntimeError):
        return None
    if any(root == canonical or root in canonical.parents for root in roots):
        return None
    return canonical


def space_report(settings, directory, kind=None, days=30):
    """复用 CLI 预览，不把交付文件当成可自动删除的缓存。"""
    store = Path(cache_directory(settings, "imageStore", None) or default_store()).resolve()
    cache = Path(cache_directory(settings, "layerCache", Path(tempfile.gettempdir()) /
                                 "pyimagebuilder-layer-cache")).resolve()
    usage = disk_usage(store, cache)
    threshold = time.time() - days * 86400
    archived = {}
    for row in list_builds(directory):
        for item in row["artifacts"]:
            status, info = artifact_state(item)
            if status not in ("available", "unverified", "changed"):
                continue
            path = _outside_managed(Path(item["path"]), (store, cache))
            if path is not None and path not in archived:
                archived[path] = (info, row["tag"])
    usage["deliveries"] = {"files": len(archived), "bytes": sum(info.st_size for info, _ in archived.values())}
    candidates = []
    estimate = 0
    summary = ""
    if kind == "images":
        preview = prune_images(store, days, dry_run=True)
        candidates = preview["candidates"]
        estimate = preview["reclaimable_bytes"]
        summary = "可能包含仍有标签的镜像；预计大小包含失去引用的层。"
    elif kind == "cache" and cache.exists():
        preview = prune_old_cache(cache, days, dry_run=True)
        candidates = [{"path": path, "reason": "过期条目"} for path in preview["expired_entries"]]
        candidates += preview["cleanup"]["invalid_entries"] + preview["cleanup"]["orphan_layers"]
        estimate = preview["reclaimable_bytes"]
    elif kind == "orphans":
        preview = CASStore(store).prune_orphans(dry_run=True)
        summary = "无引用 blob：{} 个。".format(preview["orphan_blobs"])
        estimate = preview["reclaimable_bytes"]
    elif kind == "deliveries":
        candidates = [{"path": str(path), "bytes": info.st_size, "reason": tag}
                      for path, (info, tag) in sorted(archived.items()) if info.st_mtime < threshold]
        estimate = sum(item["bytes"] for item in candidates)
        summary = "仅按当前文件的修改时间列出已登记的路径，删除前请核对归档内容和交付需要。"
    return usage, candidates, estimate, summary
