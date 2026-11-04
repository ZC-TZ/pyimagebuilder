"""工作台表单和展示层：配置仅展示白名单字段。"""

import hmac
import os
import secrets
import shutil
import stat
import sys
from html import escape
from urllib.parse import parse_qs, quote, urlencode

from errors import BuildError
from image_cli import list_images
from settings import cache_directory
from web_jobs import FORMATS, LABELS, PLATFORMS, Jobs, command_for, file_stamp, open_artifact


STATES = {"queued": "排队中", "running": "运行中", "success": "成功", "failed": "失败", "cancelled": "已取消"}


def _e(value):
    return escape(str(value))


def _input(name, label, placeholder="", required=False):
    return '<label>{}<input name="{}" placeholder="{}" {}></label>'.format(
        label, name, _e(placeholder), "required" if required else "")


def _select(name, label, options, selected=""):
    choices = "".join('<option value="{}"{}>{}</option>'.format(
        _e(value), " selected" if value == selected else "", _e(value or "自动")) for value in options)
    return '<label>{}<select name="{}">{}</select></label>'.format(label, name, choices)


def _check(name, label):
    return '<label class="check"><input type="checkbox" name="{}" value="1">{}</label>'.format(name, label)


def _section(title, body):
    return '<article><h2>{}</h2>{}</article>'.format(title, body)


def _pairs(values):
    return "<dl>" + "".join("<dt>{}</dt><dd><code>{}</code></dd>".format(label, _e(value))
                            for label, value in values) + "</dl>"


class Workbench:
    def __init__(self, settings, config, directory):
        self.settings, self.config, self.directory = settings, config, directory
        self.prefix = "/session/" + secrets.token_urlsafe(24)
        self.config_arg = config if config.is_file() else None
        self.profiles = settings.get("fast", {}).get("profiles", {})
        self.jobs = Jobs(directory)

    def nav(self):
        return '<nav class="tabs" aria-label="工作台">{}</nav>'.format("".join(
            '<a href="{}{}">{}</a>'.format(self.prefix, path, label) for path, label in
            (("/workbench", "构建"), ("/jobs", "任务"), ("/images", "镜像库"),
             ("/history", "打包台账"), ("/config", "配置"))))

    def form(self, action, body, label=None):
        return '<form class="job-form" method="post" action="{}/submit/{}">{}<button type="submit">{}</button></form>'.format(
            self.prefix, action, body, label or LABELS[action])

    def builds(self):
        platform = _select("platform", "平台", PLATFORMS, PLATFORMS[0])
        formats = _select("format", "归档格式", FORMATS, FORMATS[0])
        destination = _input("output", "输出 tar 路径", "/data/images/hisq_front-1.0.0.tar", True)
        context = _input("context", "构建上下文目录", "/data/project", True)
        dockerfile = _input("dockerfile", "Dockerfile（可选）", "默认使用上下文中的 Dockerfile")
        if self.profiles:
            default = self.settings["fast"].get("defaultProfile", "")
            fast = self.form("fast", _input("package", "WAR／dist.zip 路径", "/data/packages/dist.zip", True)
                             + _input("project", "项目", "hisq", True) + _input("version", "版本", "1.0.0", True)
                             + _select("profile", "模板", sorted(self.profiles), default)
                             + destination + platform + formats)
        else:
            fast = '<p>当前配置没有 Fast 模板。<a href="{}/config">查看配置</a>。</p>'.format(self.prefix)
        ordinary = context + dockerfile + _input("base-tar", "基础镜像 tar（可选）") + \
            _input("tag", "镜像标签", "hisq_back:1.0.0", True) + destination + platform + formats + \
            _input("target", "目标阶段（可选）") + _check("offline", "仅使用本地基础镜像") + \
            _check("pull", "刷新基础镜像") + _check("no-cache", "禁用构建缓存")
        if sys.platform == "linux":
            ordinary += _check("run", "允许执行可信 Dockerfile 的 RUN")
        return "".join((_section("快速打包", fast), _section("普通 Dockerfile 构建", self.form("build", ordinary)),
                        _section("静态检查 Dockerfile", self.form("check", context + dockerfile + platform))))

    def images(self):
        result = list_images(cache_directory(self.settings, "imageStore", None))
        items = []
        for item in result["images"]:
            tags = "、".join(str(tag) for tag in item.get("tags", []))
            items.append(_section(_e(tags or "镜像条目"), _pairs((
                ("平台", item.get("platform", "—")), ("来源", item.get("source", item.get("storage", "归档"))),
                ("归档", item.get("archive") or "—"))) +
                ('<p>{}</p>'.format(_e(item["error"])) if item.get("error") else "")))
        platform = _select("platform", "平台", PLATFORMS, PLATFORMS[0])
        reference = _input("reference", "镜像引用", "harbor.example/project/image:1", True)
        archive = _input("archive", "现有 tar 路径", "/data/images/app.tar", True)
        tag = _input("tag", "镜像标签（可选）")
        source = ("", "registry", "artifactory")
        return "".join((
            _section("镜像库位置", '<p><code>{}</code></p><p>{} 条</p>'.format(_e(result["image_store"]), len(items))),
            _section("拉取基础镜像", self.form("pull", reference + platform + _select("source", "来源", source))),
            _section("导入 Docker tar", self.form("load", archive + tag + platform)),
            _section("从镜像库导出 tar", self.form("save", reference + platform +
                     _select("source", "来源", source + ("local",)) + _input("output", "输出 tar 路径", "", True))),
            _section("检查现有 Docker tar", self.form("inspect", archive + tag) + self.form("verify", archive + tag)),
            _section("磁盘占用", self.form("df", "")),
            _section("删除本地镜像引用", self.form("remove", reference + platform +
                     _select("source", "来源", source + ("local",)) +
                     _input("confirm", "再次输入完整镜像引用", "", True))),
            "".join(items)))

    def configuration(self):
        cache = self.settings.get("cache", {})
        sections = [_section("运行位置", _pairs((
            ("配置文件", str(self.config) + ("" if self.config.is_file() else "（未创建）")),
            ("打包台账", self.directory), ("镜像库", cache.get("imageStore", "系统默认")),
            ("层缓存", cache.get("layerCache", "系统临时目录")),
            ("默认 Fast 模板", self.settings.get("fast", {}).get("defaultProfile", "—")))))]
        for name, data in sorted(self.profiles.items()):
            sections.append(_section("Fast · " + _e(name), _pairs((
                ("类型", data.get("flavor", "—")), ("基础镜像", data.get("baseImage", "—")),
                ("容器部署目录", data.get("deployDir", "默认")), ("输出目录", data.get("outputDir", "应用包所在目录"))))))
        sources = self.settings.get("repositories", {})
        if sources:
            sections.append(_section("已配置仓库", _pairs(
                (_e(host if "@" not in host else "隐藏的仓库地址"), data.get("source", "registry"))
                for host, data in sorted(sources.items()))))
        return "".join(sections)

    def tasks(self):
        jobs = self.jobs.snapshot()
        if not jobs:
            return '<p class="empty">还没有网页任务。</p>'
        return "".join(_section(LABELS[job["action"]], '<p>{}　<code>{}</code> · <a href="{}/job?{}">查看任务</a></p>'.format(
            STATES[job["state"]], _e(job["label"]), self.prefix, urlencode({"id": job["id"]}))) for job in jobs)

    def job_page(self, job):
        identity, state = job["id"], job["state"]
        running = state in ("queued", "running")
        content = '<p><code>{}</code></p><p><strong>{}</strong>　{}</p>'.format(
            _e(job["label"]), STATES[state], _e(job["status"]))
        if state == "queued" or (state == "running" and os.name == "posix" and not job.get("interrupt")):
            content += '<form method="post" action="{}/cancel?{}"><button>取消任务</button></form>'.format(
                self.prefix, urlencode({"id": identity}))
        if "code" in job:
            content += "<p>退出码：{}</p>".format(job["code"])
        for index, (path, _) in enumerate(job["files"]):
            content += '<p><a href="{}/download?{}">下载归档</a>　<code>{}</code></p>'.format(
                self.prefix, _e(urlencode({"id": identity, "file": index})), _e(path))
        content += '<h2>任务日志</h2><pre>{}</pre>'.format(_e("\n".join(job["logs"]) or "等待输出…"))
        return _section(LABELS[job["action"]], content), running

    def submit(self, action, data):
        args, outputs = command_for(action, data, self.config_arg, self.profiles)
        label = next((data[key] for key in ("project", "tag", "reference", "archive", "context") if data.get(key)), "")
        return self.jobs.submit(action, args, outputs, label[:300])

    def close(self):
        self.jobs.close()


def form_data(handler):
    """只接受有界的浏览器表单，不提供上传或任意命令接口。"""
    if handler.headers.get_content_type() != "application/x-www-form-urlencoded":
        raise ValueError("Invalid form content type")
    lengths = handler.headers.get_all("Content-Length") or []
    if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdecimal() or len(lengths[0]) > 6:
        raise ValueError("Invalid form length")
    size = int(lengths[0])
    if size > 32768 or handler.headers.get("Transfer-Encoding") is not None:
        raise ValueError("Invalid form length")
    raw = handler.rfile.read(size)
    if len(raw) != size:
        raise ValueError("Incomplete form")
    values = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
    if len(values) > 24 or any(len(items) != 1 for items in values.values()):
        raise ValueError("Invalid form fields")
    return {name: items[0] for name, items in values.items()}


def session_route(path, prefix):
    provided = path[:len(prefix)]
    if not provided.isascii() or not hmac.compare_digest(provided, prefix):
        return None
    route = path[len(prefix):]
    return route if route.startswith("/") else None


def download(handler, job, index):
    """只导出已完成任务登记的归档，打开文件后再次核对文件身份。"""
    if job is None or not 0 <= index < len(job["files"]):
        return handler._reply(404, "Artifact not found")
    path, expected = job["files"][index]
    try:
        stream = open_artifact(path)
    except OSError:
        return handler._reply(409, "Artifact was moved or changed")
    with stream:
        status = os.fstat(stream.fileno())
        if not stat.S_ISREG(status.st_mode) or file_stamp(status) != expected:
            return handler._reply(409, "Artifact was moved or changed")
        handler.send_response(200)
        handler.send_header("Content-Type", "application/octet-stream")
        handler.send_header("Content-Length", str(status.st_size))
        handler.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(path.name, safe=""))
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.end_headers()
        shutil.copyfileobj(stream, handler.wfile, 1024 * 1024)
