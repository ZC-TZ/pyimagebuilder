"""交付菜单只接受台账 ID 或当前镜像库的条目 ID。"""

import hashlib
import json
import os
import secrets
import threading
import time
from collections import OrderedDict
from pathlib import Path

from build_records import get_build
from cache_identity import file_identity
from errors import BuildError
from image_cli import _check_tag, list_images
from image_reader import digest_hex
from image_store import _archive_path
from path_policy import output_path
from settings import cache_directory
from web_artifacts import artifact_state
from web_delivery_worker import push_destination
from web_jobs import PLATFORMS, _path, _required, _target, file_stamp, open_artifact, path_value, rootfs_changes


DELIVERY_ACTIONS = {"verify": "校验", "load": "导入镜像库", "tag": "改标签并另存",
                    "push": "推送到 Registry / Harbor", "save": "导出 Docker tar",
                    "history": "镜像层历史", "analyze": "大层与重复文件分析",
                    "optimize": "优化并另存", "flatten": "扁平化并另存", "rootfs-export": "导出 rootfs",
                    "slim": "裁剪与压缩镜像"}


def _key(parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=True, separators=(",", ":")).encode("ascii")).hexdigest()


def library_items(settings):
    """每个标签生成一个条目标识；同标签不同归档、平台或来源不会混在一起。"""
    result = list_images(cache_directory(settings, "imageStore", None))
    for row in result["images"]:
        items = []
        platform, archive, source = row.get("platform"), row.get("archive"), row.get("source")
        if platform in PLATFORMS and not row.get("error") and (archive or source in ("registry", "artifactory", "local")):
            for tag in row.get("tags", []):
                if not isinstance(tag, str) or not tag or len(tag) > 4096:
                    continue
                if archive and source is None:
                    for kind in ("local", "registry", "artifactory"):
                        try:
                            if Path(archive) == _archive_path(result["image_store"], tag, platform, kind):
                                source = kind
                                row["source"] = kind
                                break
                        except BuildError:
                            continue
                manifest = row.get("manifest") if not archive else None
                if not archive:
                    try:
                        digest_hex(manifest)
                    except BuildError:
                        continue
                identity = _key([tag, platform, archive, source, manifest])
                items.append({"id": identity, "tag": tag, "platform": platform, "archive": archive,
                              "source": source, "format": "docker" if archive else "cas",
                              "manifest": manifest, "store": result["image_store"]})
        row["delivery_items"] = items
    return result


class Delivery:
    def __init__(self, settings, config, directory, job_lookup=None):
        self.settings, self.config, self.directory = settings, config, directory
        self.pending = OrderedDict()
        self.lock = threading.Lock()
        self.job_lookup = job_lookup

    def select(self, data):
        scope, identity = data.get("scope"), data.get("id", "")
        if scope == "job" and self.job_lookup is not None:
            job = self.job_lookup(identity)
            if (job is None or job["action"] != "multiarch" or job["state"] != "success"
                    or not job.get("label") or len(job.get("artifacts", [])) != 1):
                raise BuildError("未找到可交付的多架构归档")
            item = job["artifacts"][0]
            if artifact_state(item)[0] != "available":
                raise BuildError("多架构归档已移走或变更")
            return {"format": "oci-index", "archive": item["path"], "record": item,
                    "tag": job["label"], "platform": "linux/amd64 + linux/arm64"}
        if scope == "history":
            raw_index = data.get("file", "")
            if not raw_index.isascii() or not raw_index.isdecimal() or len(raw_index) > 3:
                raise BuildError("无效的归档序号")
            row = get_build(self.directory, identity)
            index = int(raw_index)
            if row is None or index >= len(row["artifacts"]) or row["platform"] not in PLATFORMS:
                raise BuildError("没有找到该历史归档")
            item = row["artifacts"][index]
            if artifact_state(item)[0] != "available":
                raise BuildError("历史归档已移走、变更或没有可用摘要")
            return {"format": item["format"], "archive": item["path"], "record": item,
                    "tag": row["tag"], "platform": row["platform"]}
        if scope == "library" and len(identity) == 64:
            for row in library_items(self.settings)["images"]:
                for item in row["delivery_items"]:
                    if item["id"] == identity:
                        return item
        raise BuildError("未找到所选镜像库条目，请刷新镜像列表")

    @staticmethod
    def actions(selected):
        if selected["format"] == "oci-index":
            return ("verify", "push")
        if selected["format"] == "cas":
            return ("verify", "save", "history", "analyze", "optimize", "flatten", "rootfs-export", "slim")
        if selected["format"] == "oci":
            return ("verify", "load", "push", "history", "analyze", "optimize", "flatten", "rootfs-export", "slim")
        return ("verify", "load", "tag", "push", "history", "analyze", "optimize", "flatten", "rootfs-export", "slim")

    def plan(self, data):
        selected = self.select(data)
        action = data.get("action")
        if action not in self.actions(selected):
            raise BuildError("所选归档不支持该操作")
        result = {key: selected[key] for key in ("format", "archive", "tag", "platform")}
        result.update(action=action, config=str(self.config) if self.config is not None else None)
        if selected.get("record") is not None:
            result["record"] = {key: selected["record"][key] for key in ("path", "bytes", "sha256")}
        elif selected["archive"]:
            path = Path(selected["archive"])
            with open_artifact(path) as stream:
                result["stamp"] = file_stamp(os.fstat(stream.fileno()))
                if os.name == "nt":
                    identity = file_identity(path)
                    if tuple(identity[:4]) != result["stamp"][:4]:
                        raise BuildError("源归档已变更，请重新选择")
                    result["digest"] = identity[-1]
        else:
            result.update(source=selected["source"], manifest=selected["manifest"], store=selected["store"])
        if action in ("tag", "save", "optimize", "flatten", "rootfs-export"):
            result["output"] = str(_target(data))
        if action == "flatten":
            result["changes"] = rootfs_changes(data)
        if action == "slim":
            from slim_rules import DEFAULT_LIMIT, load_patterns
            compressed = bool(data.get("gzip"))
            target = _path(path_value(data, "output", required=True))
            compressed = compressed or str(target).lower().endswith((".tar.gz", ".tgz"))
            if compressed and target.suffix.lower() == ".tar":
                target = Path(str(target) + ".gz")
            if not str(target).lower().endswith((".tar.gz", ".tgz") if compressed else (".tar",)):
                raise BuildError("请选择新的 tar 或 tar.gz 输出路径")
            target = output_path(target)
            report = output_path(Path(str(target) + ".slim.json"))
            if target.exists() or report.exists():
                raise BuildError("瘦身输出或报告已存在，请选择新文件名")
            options = {}
            for field, key in (("delete", "deletes"), ("keep", "keeps")):
                text = data.get(field, "")
                if len(text) > 8192:
                    raise BuildError("瘦身规则过长")
                options[key] = load_patterns([line.strip() for line in text.splitlines()
                                             if line.strip() and not line.lstrip().startswith("#")])
            try:
                level = int(data.get("compress-level", "6"))
                limit = int(data.get("upload-limit", str(DEFAULT_LIMIT)))
            except ValueError as exc:
                raise BuildError("压缩级别和上传限制必须是整数") from exc
            if not 1 <= level <= 9 or not 0 < limit <= 2 ** 63 - 1:
                raise BuildError("压缩级别须为 1–9，上传限制须为正字节数")
            result.update(output=str(target), report=str(report), gzip_output=compressed,
                          compress_level=level, upload_limit=limit, auto_protect=not bool(data.get("no-auto-protect")),
                          analyze_only=bool(data.get("analyze-only")), **options)
        if action == "tag":
            new_tag = _required(data, "new-tag")
            _check_tag(new_tag)
            result["new_tag"] = new_tag
        elif action == "push":
            result["destination"] = push_destination(self.settings, _required(data, "destination"))
        return result

    def offer(self, plan):
        with self.lock:
            now = time.monotonic()
            for token, (expires, _) in list(self.pending.items()):
                if expires <= now:
                    del self.pending[token]
            while len(self.pending) >= 64:
                self.pending.popitem(last=False)
            token = secrets.token_urlsafe(24)
            self.pending[token] = (now + 600, plan)
            return token

    def confirm(self, token, action, accept):
        with self.lock:
            entry = self.pending.get(token)
            if entry is None or entry[0] <= time.monotonic() or entry[1]["action"] != action:
                raise BuildError("确认已过期、已使用或操作不匹配，请重新预览")
            # 入队被拒绝时仍可重试。校验和接收放在同一把锁内，避免重复提交。
            result = accept(entry[1])
            del self.pending[token]
            return result
