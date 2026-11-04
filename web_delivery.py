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
from errors import BuildError
from image_cli import _check_tag, list_images
from settings import cache_directory
from web_artifacts import artifact_state
from web_delivery_worker import push_destination
from web_jobs import PLATFORMS, _required, _target, file_stamp, open_artifact


DELIVERY_ACTIONS = {"verify": "校验", "load": "导入镜像库", "tag": "改标签并另存",
                    "push": "推送到 Registry / Harbor", "save": "导出 Docker tar"}


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
                identity = _key([tag, platform, archive, source])
                items.append({"id": identity, "tag": tag, "platform": platform, "archive": archive,
                              "source": source, "format": "docker" if archive else "cas"})
        row["delivery_items"] = items
    return result


class Delivery:
    def __init__(self, settings, config, directory):
        self.settings, self.config, self.directory = settings, config, directory
        self.pending = OrderedDict()
        self.lock = threading.Lock()

    def select(self, data):
        scope, identity = data.get("scope"), data.get("id", "")
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
        if selected["format"] == "cas":
            return ("verify", "save")
        if selected["format"] == "oci":
            return ("verify", "load", "push")
        return ("verify", "load", "tag", "push")

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
            with open_artifact(Path(selected["archive"])) as stream:
                result["stamp"] = file_stamp(os.fstat(stream.fileno()))
        else:
            result["source"] = selected["source"]
        if action in ("tag", "save"):
            result["output"] = str(_target(data))
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

    def confirm(self, token):
        with self.lock:
            entry = self.pending.pop(token, None)
        if entry is None or entry[0] <= time.monotonic():
            raise BuildError("推送确认已过期或已使用，请重新预览")
        return entry[1]
