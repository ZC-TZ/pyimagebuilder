"""工作台表单和展示层：配置仅展示白名单字段。"""

import hmac
import os
import secrets
import stat
import sys
from html import escape
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode

from errors import BuildError
from image_cli import list_images
from settings import cache_directory
from web_artifacts import space_report
from web_job_journal import form_snapshot
from web_jobs import FORMATS, LABELS, PLATFORMS, Jobs, command_for, fast_plan, file_stamp, open_artifact


STATES = {"queued": "排队中", "running": "运行中", "success": "成功", "failed": "失败",
          "cancelled": "已取消", "unknown": "之前会话未记录最终结果"}
SPACE_KINDS = {"images": "旧镜像", "cache": "过期构建缓存", "orphans": "无引用镜像层",
               "deliveries": "旧交付 tar（仅列出）"}

FAST_SCRIPT = r"""
(() => {
  const form = document.querySelector('form[action$="/submit/fast"]');
  if (!form) return;
  const preview = document.getElementById('fast-preview');
  const output = form.elements.output;
  const original = output.placeholder;
  let timer, request, generation = 0;
  function update() {
    const current = ++generation;
    clearTimeout(timer);
    if (request) request.abort();
    output.placeholder = original;
    if (!['package', 'project', 'version'].every(key => form.elements[key].value.trim())) {
      preview.textContent = '填好应用包、项目和版本后显示预览。';
      return;
    }
    preview.textContent = '正在计算预览…';
    timer = setTimeout(async () => {
      request = new AbortController();
      try {
        const response = await fetch(form.action.replace(/\/submit\/fast$/, '/preview-fast'), {
          method: 'POST', body: new URLSearchParams(new FormData(form)), signal: request.signal
        });
        const plan = await response.json();
        if (generation !== current) return;
        if (!response.ok) { preview.textContent = plan.error || '无法预览'; return; }
        output.placeholder = plan.outputs[0];
        preview.textContent = ['标签：' + plan.tag, '基础镜像：' + plan.base, '部署：' + plan.deploy,
          '输出：' + plan.outputs.join('、'), plan.conflicts.length ? '已存在：' + plan.conflicts.join('、') : '']
          .filter(Boolean).join('\n');
      } catch (error) {
        if (generation === current && error.name !== 'AbortError') preview.textContent = '暂时无法预览，提交时仍会校验。';
      }
    }, 350);
  }
  form.addEventListener('input', update);
  form.addEventListener('change', update);
  update();
})();
"""


def _e(value):
    return escape(str(value))


def _input(name, label, placeholder="", required=False, value=""):
    return '<label>{}<input name="{}" placeholder="{}" value="{}" {}></label>'.format(
        label, name, _e(placeholder), _e(value), "required" if required else "")


def _select(name, label, options, selected="", labels=None):
    choices = "".join('<option value="{}"{}>{}</option>'.format(
        _e(value), " selected" if value == selected else "", _e((labels or {}).get(value, value or "自动"))) for value in options)
    return '<label>{}<select name="{}">{}</select></label>'.format(label, name, choices)


def _check(name, label, checked=False):
    return '<label class="check"><input type="checkbox" name="{}" value="1"{}>{}</label>'.format(
        name, " checked" if checked else "", label)


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
             ("/history", "打包台账"), ("/space", "空间"), ("/config", "配置"))))

    def form(self, action, body, label=None):
        return '<form class="job-form" method="post" action="{}/submit/{}">{}<button type="submit">{}</button></form>'.format(
            self.prefix, action, body, label or LABELS[action])

    def builds(self, action=None, draft=None):
        draft = draft or {}

        def field(name, label, placeholder="", required=False):
            return _input(name, label, placeholder, required, draft.get(name, ""))

        platform = _select("platform", "平台", PLATFORMS, draft.get("platform", PLATFORMS[0]))
        formats = _select("format", "归档格式", FORMATS, draft.get("format", FORMATS[0]))
        destination = field("output", "输出 tar 路径", "/data/images/hisq_front-1.0.0.tar", True)
        context = field("context", "构建上下文目录", "/data/project", True)
        dockerfile = field("dockerfile", "Dockerfile（可选）", "默认使用上下文中的 Dockerfile")
        if self.profiles:
            default = draft.get("profile", self.settings["fast"].get("defaultProfile", ""))
            fast = self.form("fast", field("package", "WAR／dist.zip 路径", "/data/packages/dist.zip", True)
                             + field("project", "项目", "hisq", True) + field("version", "版本", "1.0.0", True)
                             + _select("profile", "模板", sorted(self.profiles), default)
                             + field("output", "输出 tar 路径（可选）", "留空按模板自动生成")
                             + platform + formats)
            fast += '<p id="fast-preview" class="muted preview" aria-live="polite">留空使用默认输出路径。</p>'
        else:
            fast = '<p>当前配置没有 Fast 模板。<a href="{}/config">查看配置</a>。</p>'.format(self.prefix)
        ordinary = context + dockerfile + field("base-tar", "基础镜像 tar（可选）") + \
            field("tag", "镜像标签", "hisq_back:1.0.0", True) + destination + platform + formats + \
            field("target", "目标阶段（可选）") + _check("offline", "仅使用本地基础镜像", draft.get("offline")) + \
            _check("pull", "刷新基础镜像", draft.get("pull")) + _check("no-cache", "禁用构建缓存", draft.get("no-cache"))
        if sys.platform == "linux":
            ordinary += _check("run", "允许执行可信 Dockerfile 的 RUN", draft.get("run"))
        if action == "fast":
            return _section("复用快速打包参数", fast)
        if action == "build":
            return _section("复用 Dockerfile 构建参数", self.form("build", ordinary))
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

    def preview_fast(self, data):
        plan = fast_plan(data, self.config_arg, self.profiles)
        return {
            "tag": plan["tag"], "base": plan["base"], "deploy": plan["deploy"],
            "outputs": list(map(str, plan["outputs"])), "conflicts": list(map(str, plan["conflicts"]))}

    def space(self, kind=None, days=30):
        usage, candidates, estimate, summary = space_report(self.settings, self.directory, kind, days)
        forms = '<form method="get" action="{}/space"><label>文件年龄（天）<input name="days" type="number" min="0" max="36500" value="{}" required></label>{}<button>预览</button></form>'.format(
            self.prefix, days, _select("kind", "检查范围", tuple(SPACE_KINDS), kind or "images", SPACE_KINDS))
        for key, title in (("image_store", "镜像库"), ("layer_cache", "构建缓存"), ("deliveries", "已登记的交付 tar")):
            item = usage[key]
            forms += _section(title, _pairs((("文件数", item["files"]), ("文件字节", "{:,} B".format(item["bytes"])),
                                             ("位置", item.get("path", "台账中存在的库外归档")))))
        if kind is not None:
            details = '<p>候选文件估算：<strong>{:,} B（{:.1f} MiB）</strong></p><p>{}</p>'.format(
                estimate, estimate / (1024 * 1024), _e(summary))
            if candidates:
                details += "<ul>" + "".join("<li><code>{}</code> · {}</li>".format(
                    _e(item["path"]), _e(item.get("reason", item.get("storage", "")))) for item in candidates[:200]) + "</ul>"
                if len(candidates) > 200:
                    details += "<p>只显示前 200 项，总计 {} 项。</p>".format(len(candidates))
            elif not summary:
                details += "<p>没有符合条件的文件。</p>"
            forms += _section(SPACE_KINDS[kind], details + "<p>这是预览。实际释放量受共享层和硬链接影响。</p>")
        return forms

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
        active = {item["id"] for item in jobs}
        jobs += [self._archived(item) for item in self.jobs.journal.recent() if item["id"] not in active]
        if not jobs:
            return '<p class="empty">还没有网页任务。</p>'
        return "".join(_section(LABELS[job["action"]], '<p>{}　<code>{}</code> · <a href="{}/job?{}">查看任务</a></p>'.format(
            STATES[job["state"]], _e(job["label"] or job.get("updated", "")), self.prefix,
            urlencode({"id": job["id"]}))) for job in jobs if job["action"] in LABELS)

    def _archived(self, item):
        state = "unknown" if item["state"] in ("queued", "running") else item["state"]
        return dict(item, state=state, status=item["updated"], logs=[], files=[], archived=True)

    def get_job(self, identity):
        live = self.jobs.snapshot(identity)
        if live is not None:
            return live
        item = self.jobs.journal.read(identity)
        return self._archived(item) if item is not None and item["action"] in LABELS else None

    def job_page(self, job):
        identity, state = job["id"], job["state"]
        running = state in ("queued", "running")
        content = '<p><code>{}</code></p><p><strong>{}</strong>　{}</p>'.format(
            _e(job["label"]), STATES[state], _e(job["status"]))
        if state == "queued" or (state == "running" and os.name == "posix" and not job.get("interrupt")):
            content += '<form method="post" action="{}/cancel?{}"><button>取消任务</button></form>'.format(
                self.prefix, urlencode({"id": identity}))
        if "code" in job:
            content += "<p>退出码：{}</p>".format(_e(job["code"]))
        if job.get("form") and not running:
            content += '<p><a href="{}/reuse?{}">复用参数</a>，提交前可修改版本或输出。</p>'.format(
                self.prefix, urlencode({"id": identity}))
        for index, (path, _) in enumerate(job["files"]):
            content += '<p><a href="{}/download?{}">下载归档</a>　<code>{}</code></p>'.format(
                self.prefix, _e(urlencode({"id": identity, "file": index})), _e(path))
        if job.get("archived"):
            content += "<p>这里只保存任务结果；成功的归档请到打包台账查看。</p>"
        else:
            content += '<h2>任务日志</h2><pre>{}</pre>'.format(_e("\n".join(job["logs"]) or "等待输出…"))
        return _section(LABELS[job["action"]], content), running

    def submit(self, action, data):
        args, outputs = command_for(action, data, self.config_arg, self.profiles)
        label = next((data[key] for key in ("project", "tag", "reference", "archive", "context") if data.get(key)), "")
        draft = form_snapshot(action, data)
        for key in ("package", "context", "dockerfile", "base-tar", "output"):
            if draft.get(key, "").strip():
                draft[key] = str(Path(draft[key].strip()).expanduser().absolute())
        return self.jobs.submit(action, args, outputs, label[:300], draft)

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
        send_artifact(handler, stream, path.name, status.st_size)


def send_artifact(handler, stream, filename, size):
    handler.send_response(200)
    handler.send_header("Content-Type", "application/octet-stream")
    handler.send_header("Content-Length", str(size))
    handler.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(filename, safe=""))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.end_headers()
    remaining = size
    while remaining:
        block = stream.read(min(1024 * 1024, remaining))
        if not block:
            break
        handler.wfile.write(block)
        remaining -= len(block)
