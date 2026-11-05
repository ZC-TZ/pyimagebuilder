"""持久保存网页任务的状态和可复用表单；不落盘子进程原始日志。"""

import json
import os
import re
import stat
import tempfile
from datetime import datetime, timezone

from pathlib import Path

from compat import unlink_missing
from file_resources import fdopen_owned


IDENTITY = re.compile(r"[0-9a-f]{32}\Z")
ARTIFACT_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
TASK_ARTIFACT_SUFFIXES = (".tar", ".spdx.json", ".provenance.json", ".dsse.json", ".hermetic.json")
MAX_JOB_BYTES = 512 * 1024
FORM_FIELDS = {
    "fast": ("package", "project", "version", "profile", "output", "platform", "format"),
    "build": ("context", "dockerfile", "base-tar", "tag", "output", "platform", "format",
              "target", "offline", "pull", "no-cache", "run", "base-map", "epoch", "input-lock", "input-lock-job", "attest"),
}
STATES = ("queued", "running", "success", "failed", "cancelled")


def form_snapshot(action, data):
    fields = FORM_FIELDS.get("build" if action == "lock-inputs" else action, ())
    return {key: data[key] for key in fields
            if isinstance(data.get(key), str) and len(data[key]) <= 4096
            and (action != "lock-inputs" or key != "input-lock-job")}


def task_artifact_suffixes(action):
    return (".json",) if action == "lock-inputs" else TASK_ARTIFACT_SUFFIXES


def artifact_records(value, action):
    if not isinstance(value, list):
        return []
    records = []
    for item in value[:8]:
        if not isinstance(item, dict):
            continue
        path, size, digest = item.get("path"), item.get("bytes"), item.get("sha256")
        if (isinstance(path, str) and 0 < len(path) <= 4096 and "\0" not in path and
                Path(path).is_absolute() and path.lower().endswith(task_artifact_suffixes(action)) and
                type(size) is int and size >= 0 and isinstance(digest, str) and ARTIFACT_DIGEST.fullmatch(digest)):
            records.append({"path": path, "bytes": size, "sha256": digest})
    return records


class JobJournal:
    def __init__(self, directory):
        self.directory = directory / "web-jobs"

    def write(self, job):
        """状态更新原子替换同一任务文件，其他会话写自己的 UUID。"""
        identity = job["id"]
        form = form_snapshot(job["action"], job.get("form", {}))
        item = {"version": 1, "id": identity, "action": job["action"], "state": job["state"],
                "label": job["label"] if form or job["action"] == "multiarch" else "", "form": form,
                "updated": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        if "code" in job:
            item["code"] = job["code"]
        if "elapsed" in job:
            item["elapsed"] = job["elapsed"]
        if job["state"] == "success" and job.get("artifacts"):
            item["artifacts"] = artifact_records(job["artifacts"], job["action"])
        self.directory.mkdir(parents=True, exist_ok=True)
        fd, pending = tempfile.mkstemp(prefix=".web-job-", suffix=".part", dir=self.directory)
        try:
            with fdopen_owned(fd, "w", encoding="utf-8") as stream:
                json.dump(item, stream, ensure_ascii=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(pending, str(self.directory / (identity + ".json")))
        finally:
            unlink_missing(pending)

    def read(self, identity):
        if not IDENTITY.fullmatch(identity):
            return None
        path = self.directory / (identity + ".json")
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_JOB_BYTES:
                return None
            item = json.loads(path.read_text(encoding="utf-8"))
            if (not isinstance(item, dict) or item.get("version") != 1 or item.get("id") != identity or
                    not isinstance(item.get("action"), str) or item.get("state") not in STATES or
                    not isinstance(item.get("label"), str) or not isinstance(item.get("updated"), str) or
                    not isinstance(item.get("form"), dict)):
                return None
            item["form"] = form_snapshot(item["action"], item["form"])
            if type(item.get("elapsed")) is not int or item["elapsed"] < 0:
                item.pop("elapsed", None)
            item["artifacts"] = (artifact_records(item.get("artifacts"), item["action"])
                                 if item["state"] == "success" else [])
            return item
        except (OSError, ValueError, RecursionError):
            return None

    def recent(self, limit=60):
        paths = []
        for path in self.directory.glob("*.json"):
            if IDENTITY.fullmatch(path.stem):
                try:
                    info = path.lstat()
                except OSError:
                    continue
                if stat.S_ISREG(info.st_mode):
                    paths.append((info.st_mtime_ns, path.stem))
        result = []
        for _, identity in sorted(paths, reverse=True):
            item = self.read(identity)
            if item is not None:
                result.append(item)
                if len(result) >= limit:
                    break
        return result
