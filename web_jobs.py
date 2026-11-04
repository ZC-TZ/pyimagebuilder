"""网页任务使用固定参数调用 CLI；在同一工作台串行操作镜像库。"""

import json
import os
import signal
import stat
import subprocess
import sys
import threading
import uuid
from collections import OrderedDict, deque
from pathlib import Path

from errors import BuildError
from build_args import arguments
from fast import fast_target, load_profile
from file_resources import fdopen_owned
from path_policy import output_path, resolve_host_path
from web_job_journal import JobJournal, form_snapshot


LABELS = {"fast": "快速打包", "build": "Dockerfile 构建", "check": "检查 Dockerfile",
          "pull": "拉取镜像", "load": "导入归档", "save": "导出镜像",
          "inspect": "查看归档", "verify": "验证归档", "df": "查看磁盘占用",
          "remove": "删除镜像引用", "tag": "改标签并另存", "push": "推送镜像",
          "history": "镜像层历史", "analyze": "分析镜像层", "optimize": "优化镜像",
          "flatten": "扁平化镜像", "multiarch": "合并多架构归档",
          "cache-clean": "清理过期构建缓存", "profile-test": "测试 Fast 基础镜像"}
PLATFORMS = ("linux/amd64", "linux/arm64")
FORMATS = ("docker", "oci", "both")
LOG_LINE_CHARS = 4000
LOG_RECORD_BYTES = 512 * 1024


def _text(data, name, default="", strip=True):
    value = data.get(name, default)
    if strip:
        value = value.strip()
    if len(value) > 4096 or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise BuildError("Invalid field: " + name)
    return value


def path_value(data, name, required=False):
    value = _text(data, name, strip=False)
    if not value.strip():
        if required:
            raise BuildError("Required field: " + name)
        return ""
    return value


def _required(data, name):
    value = _text(data, name)
    if not value:
        raise BuildError("Required field: " + name)
    return value


def _choice(data, name, choices, default):
    value = _text(data, name, default)
    if value not in choices:
        raise BuildError("Invalid option: " + name)
    return value


def _path(value):
    try:
        return Path(value).expanduser().absolute()
    except RuntimeError as exc:
        raise BuildError("Cannot resolve local path: " + str(exc)) from exc


def _target(data):
    target = output_path(_path(path_value(data, "output", required=True)))
    if target.suffix.lower() != ".tar" or target.exists():
        raise BuildError("Output must be a new .tar file: " + str(target))
    return target


def fast_plan(data, config, profiles, uploaded_package=None):
    """验证 Fast 表单并预览实际标签、部署位置和两个可能的输出。"""
    package = uploaded_package or resolve_host_path(_path(path_value(data, "package", required=True)))
    project = _required(data, "project")
    version = _required(data, "version")
    profile_name = _choice(data, "profile", profiles, "")
    if not package.is_file():
        raise BuildError("找不到应用包：" + str(package))
    if config is None:
        raise BuildError("Fast 需要配置文件")
    profile = load_profile(config, profile_name)
    kind = _choice(data, "format", FORMATS, "docker")
    platform = _choice(data, "platform", PLATFORMS, PLATFORMS[0])
    explicit = path_value(data, "output")
    if uploaded_package is not None and not explicit and not profile.get("outputDir"):
        raise BuildError("此模板未设置输出目录，请填写输出 tar 路径")
    front, tag, target = fast_target(package, project, version, profile, _path(explicit) if explicit else None)
    outputs = [target]
    if kind == "both":
        outputs.append(output_path(target.with_name(target.stem + ".oci.tar")))
    if any(path.suffix.lower() != ".tar" for path in outputs):
        raise BuildError("输出路径必须以 .tar 结尾")
    if uploaded_package is not None and any(resolve_host_path(package.parent) in path.parents for path in outputs):
        raise BuildError("输出路径不能位于应用包暂存目录内")
    if any(path in (package, config, profile.get("baseTar"), profile.get("serverConfig")) for path in outputs):
        raise BuildError("输出路径不能和输入文件相同")
    deploy = profile["deployDir"] + "/" + project + ("f/" if front else ".war")
    return {"package": package, "project": project, "version": version, "profile": profile_name,
            "platform": platform, "format": kind, "tag": tag, "outputs": outputs,
            "base": profile["baseImage"], "deploy": deploy,
            "conflicts": [path for path in outputs if path.exists()]}


def command_for(action, data, config, profiles, uploaded_package=None):
    """只拼装已知子命令的参数；路径不是选项，仓库凭据由原 CLI 读取。"""
    platform = _choice(data, "platform", PLATFORMS, PLATFORMS[0])
    common = ["--platform=" + platform]
    shared = ["--config=" + str(config)] if config is not None else []
    progress = ["--progress=json"]
    outputs = []
    if action == "fast":
        plan = fast_plan(data, config, profiles, uploaded_package)
        outputs = plan["outputs"]
        if plan["conflicts"]:
            raise BuildError("输出文件已存在：" + "、".join(map(str, plan["conflicts"])))
        args = [action, str(plan["package"]), plan["project"], plan["version"], "--profile=" + plan["profile"],
                "--format=" + plan["format"], "--output=" + str(outputs[0])] + shared + common + progress
    elif action in ("build", "check"):
        context = _path(path_value(data, "context", required=True))
        if not context.is_dir():
            raise BuildError("Build context must be a directory")
        args = ["build", str(context)] + shared + common
        if path_value(data, "base-tar") and path_value(data, "base-map"):
            raise BuildError("基础 tar 与基础镜像映射只能选一项")
        for key in ("dockerfile", "base-tar", "base-map"):
            value = path_value(data, key)
            if value and (key == "dockerfile" or action == "build"):
                args.append("--" + key + "=" + str(_path(value)))
        raw_args = data.get("build-args", "")
        if len(raw_args) > 8192:
            raise BuildError("构建参数太长")
        build_args = [line.strip() for line in raw_args.splitlines() if line.strip()]
        arguments(build_args)
        args += ["--build-arg=" + item for item in build_args]
        if action == "check":
            args.append("--check")
        else:
            target = _target(data)
            kind = _choice(data, "format", FORMATS, "docker")
            args += ["--tag=" + _required(data, "tag"), "--format=" + kind,
                     "--output=" + str(target)] + progress
            if _text(data, "target"):
                args.append("--target=" + _text(data, "target"))
            for key in ("pull", "no-cache"):
                if _text(data, key):
                    args.append("--" + key)
            if _text(data, "offline"):
                args.append("--offline")
            if _text(data, "run"):
                if sys.platform != "linux":
                    raise BuildError("RUN is available only on a Linux host")
                args.append("--run")
            outputs = [target]
            if kind == "both":
                outputs.append(target.with_name(target.stem + ".oci.tar"))
            epoch = _text(data, "epoch")
            if epoch:
                args.append("--source-date-epoch=" + epoch)
            lock = path_value(data, "input-lock")
            if lock:
                args += ["--hermetic", "--input-lock=" + str(_path(lock))]
                outputs.append(Path(str(target) + ".hermetic.json"))
            sign = path_value(data, "sign-key")
            if _text(data, "attest") or sign:
                args.append("--attest")
                outputs.extend(Path(str(target) + suffix) for suffix in (".spdx.json", ".provenance.json"))
            if sign:
                args.append("--sign-key=" + str(_path(sign)))
                outputs.append(Path(str(target) + ".dsse.json"))
    elif action == "multiarch":
        amd64, arm64 = (_path(path_value(data, key, required=True)) for key in ("amd64", "arm64"))
        if not amd64.is_file() or not arm64.is_file():
            raise BuildError("请选择两个已有的 OCI 归档")
        target = _target(data)
        args = ["manifest", "create", "--amd64=" + str(amd64), "--arm64=" + str(arm64),
                "--tag=" + _required(data, "tag"), "--output=" + str(target)]
        epoch = _text(data, "epoch")
        if epoch:
            args.append("--source-date-epoch=" + epoch)
        outputs = [target]
    elif action in ("pull", "save", "remove"):
        reference = _required(data, "reference")
        if reference.startswith("-") or any(char.isspace() for char in reference):
            raise BuildError("Invalid image reference")
        sources = ("", "registry", "artifactory") + (("local",) if action != "pull" else ())
        source = _choice(data, "source", sources, "")
        args = ["rmi" if action == "remove" else action, reference] + shared + common
        if source:
            args.append("--source=" + source)
        if action == "pull":
            args += progress
        elif action == "save":
            target = _target(data)
            args.append("--output=" + str(target))
            outputs = [target]
        elif _required(data, "confirm") != reference:
            raise BuildError("Confirm removal by entering the exact image reference again")
    elif action in ("load", "inspect", "verify"):
        archive = _path(path_value(data, "archive", required=True))
        if not archive.is_file():
            raise BuildError("Archive must be an existing local file")
        args = [action, "--input=" + str(archive)] + shared + common if action == "load" else [action, str(archive)]
        tag = _text(data, "tag")
        if tag:
            args.append("--tag=" + tag)
    elif action == "df":
        args = ["system", "df"] + shared
    else:
        raise BuildError("Unknown web action")
    return args, outputs


def file_stamp(value):
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def open_artifact(path):
    """先拒绝明显的链接与特殊文件，再由调用方按已打开的句柄核对身份。"""
    if not stat.S_ISREG(path.lstat().st_mode):
        raise OSError("Artifact is not a regular file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
    return fdopen_owned(os.open(str(path), flags), "rb")


class Jobs:
    """日志留在当前会话，任务状态与构建表单单独保存。"""

    def __init__(self, directory):
        self.directory = directory
        self.journal = JobJournal(directory)
        self.condition = threading.Condition()
        self.jobs = OrderedDict()
        self.queue = deque()
        self.closed = False
        self.worker = threading.Thread(target=self._run, name="web-build-worker")
        self.worker.start()

    def submit(self, action, args, outputs, label, form=None, delivery=False, cleanup=None):
        with self.condition:
            if self.closed or len(self.queue) >= 20:
                raise BuildError("Web job queue is full or closing")
            while len(self.jobs) >= 60:
                oldest = next((key for key, job in self.jobs.items()
                               if job["state"] in ("success", "failed", "cancelled")), None)
                if oldest is None:
                    raise BuildError("Web job queue is full")
                del self.jobs[oldest]
            identity = uuid.uuid4().hex
            self.jobs[identity] = {"id": identity, "action": action, "args": args, "outputs": outputs,
                                   "state": "queued", "logs": deque(maxlen=250), "log_end": 0, "files": [],
                                   "status": "", "process": None, "label": label,
                                   "form": form_snapshot(action, form or {}), "delivery": delivery,
                                   "cleanup": cleanup}
            self.queue.append(identity)
            self._save(self.jobs[identity])
            self.condition.notify()
            return identity

    @staticmethod
    def _append_log(job, line):
        if len(line) > LOG_LINE_CHARS:
            line = line[:LOG_LINE_CHARS - 1] + "…"
        job["logs"].append(line)
        job["log_end"] += 1

    def _save(self, job):
        try:
            self.journal.write(job)
        except (OSError, ValueError):
            # 台账由 CLI 单独提交；可选的任务摘要写失败不能改变归档的构建结果。
            if not job.get("journal_warning"):
                self._append_log(job, "任务摘要未能写入磁盘，关闭工作台后可能无法查看。")
                job["journal_warning"] = True

    def snapshot(self, identity=None):
        with self.condition:
            def view(job):
                snapshot = {key: list(value) if key in ("logs", "files") else value
                            for key, value in job.items() if key not in ("args", "process", "outputs", "cleanup")}
                if job["state"] == "running" and job.get("interrupt"):
                    snapshot["status"] = "正在取消"
                return snapshot
            if identity is None:
                return [view(job) for job in reversed(self.jobs.values())]
            job = self.jobs.get(identity)
            return view(job) if job is not None else None

    def cancel(self, identity):
        with self.condition:
            job = self.jobs.get(identity)
            if job is None:
                raise BuildError("Unknown job")
            if job["state"] == "queued":
                self.queue.remove(identity)
                job["state"] = "cancelled"
                self._finish(job)
            elif job["state"] == "running" and os.name == "posix" and not job.get("interrupt"):
                process = job["process"]
                if process is not None and process.poll() is None:
                    try:
                        process.send_signal(signal.SIGINT)
                    except ProcessLookupError:
                        pass
                    else:
                        job["interrupt"] = True

    def close(self):
        with self.condition:
            self.closed = True
            for identity in list(self.queue):
                self.cancel(identity)
            # POSIX 允许 CLI 执行自己的中断清理；Windows 等待任务收尾，避免硬杀留下半成品。
            for identity, job in self.jobs.items():
                if job["state"] == "running" and not job.get("interrupt"):
                    self.cancel(identity)
            self.condition.notify_all()
        self.worker.join()

    def _run(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.closed or self.queue)
                if self.closed:
                    return
                job = self.jobs[self.queue.popleft()]
                job["state"] = "running"
                self._save(job)
                options = {"start_new_session": True} if os.name == "posix" else {
                    "creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
                try:
                    entry = "web_delivery_worker.py" if job["delivery"] else "main.py"
                    command = [sys.executable, "-u", str(Path(__file__).with_name(entry))] + job["args"]
                    env = dict(os.environ, PYTHONIOENCODING="utf-8",
                               PYIMAGEBUILDER_BUILD_RECORDS_DIR=str(self.directory))
                    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                               stderr=subprocess.STDOUT, env=env, **options)
                    job["process"] = process
                except (OSError, ValueError) as exc:
                    self._append_log(job, str(exc))
                    job["state"] = "failed"
                    self._finish(job)
                    continue
            self._collect(job, process)

    def _collect(self, job, process):
        try:
            with process.stdout as stream:
                for raw in iter(lambda: stream.readline(LOG_RECORD_BYTES), b""):
                    line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                    if len(raw) == LOG_RECORD_BYTES and not raw.endswith(b"\n"):
                        # 有界读取，确保超长记录的后半段不会被误认作新的 JSON 事件。
                        with self.condition:
                            self._append_log(job, line[:LOG_LINE_CHARS - 1] + "…")
                        while raw and not raw.endswith(b"\n"):
                            raw = stream.readline(LOG_RECORD_BYTES)
                        continue
                    try:
                        event = json.loads(line)
                    except (ValueError, RecursionError):
                        event = None
                    with self.condition:
                        if job["delivery"] and isinstance(event, dict) and isinstance(event.get("web_metrics"), dict):
                            job["metrics"] = event["web_metrics"]
                            continue
                        if isinstance(event, dict) and "type" in event and "elapsed" in event:
                            current, total = event.get("current"), event.get("amount")
                            message = event.get("message") or event.get("phase") or event["type"]
                            if type(current) is int and type(total) is int and total > 0:
                                message = "{} · {:.0%}".format(message, min(max(current, 0), total) / total)
                            job["status"] = str(message)[:LOG_LINE_CHARS]
                            if event["type"] in ("heartbeat", "step_progress", "layer_progress"):
                                continue
                            line = "{}: {}".format(event["type"], message)
                        self._append_log(job, line)
        except OSError as exc:
            with self.condition:
                self._append_log(job, str(exc))
        code = process.wait()
        with self.condition:
            job["state"] = "success" if code == 0 else ("cancelled" if job.get("interrupt") else "failed")
            job["code"] = code
            if code == 0:
                for path in job["outputs"]:
                    try:
                        # Windows 的路径和句柄统计可能使用不同文件 ID 表示。
                        with open_artifact(path) as stream:
                            status = os.fstat(stream.fileno())
                            if stat.S_ISREG(status.st_mode):
                                job["files"].append((path, file_stamp(status)))
                    except OSError:
                        pass
            job["process"] = None
            self._finish(job)

    def _finish(self, job):
        cleanup = job.pop("cleanup", None)
        if cleanup is not None:
            try:
                cleanup()
            except OSError as exc:
                self._append_log(job, "未能清理暂存文件：{}".format(exc))
        self._save(job)
