"""工作台表单和展示层：配置仅展示白名单字段。"""

import hmac
import json
import os
import secrets
import stat
import sys
from html import escape
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode

from errors import BuildError
from web_artifacts import space_report
from web_job_journal import form_snapshot
from web_delivery import DELIVERY_ACTIONS, Delivery, library_items
from web_jobs import FORMATS, LABELS, PLATFORMS, Jobs, command_for, fast_plan, file_stamp, open_artifact
from web_maintenance import PROFILE_FIELDS, _revision, cache_fingerprint, cache_plan, cache_root, candidate_profile, save_profile


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
        self.delivery = Delivery(settings, self.config_arg, directory)
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
        ordinary += ('<details class="advanced"><summary>高级构建选项</summary>' +
                     field("base-map", "基础镜像映射 JSON") +
                     field("epoch", "可复现时间戳（Unix 秒）") +
                     field("input-lock", "封闭构建输入锁 JSON") +
                     '<label>构建参数（每行 NAME=VALUE，不填凭据）<textarea name="build-args" rows="3"></textarea></label>' +
                     _check("attest", "生成 SBOM 和 provenance", draft.get("attest")) +
                     field("sign-key", "本机签名私钥路径（可选）") + "</details>")
        if action == "fast":
            return _section("复用快速打包参数", fast)
        if action == "build":
            return _section("复用 Dockerfile 构建参数", self.form("build", ordinary))
        multiarch = self.form("multiarch", _input("amd64", "amd64 OCI tar", "", True) +
                              _input("arm64", "arm64 OCI tar", "", True) +
                              _input("tag", "多平台镜像标签", "", True) + destination +
                              _input("epoch", "可复现时间戳（可选）"))
        return "".join((_section("快速打包", fast), _section("普通 Dockerfile 构建", self.form("build", ordinary)),
                        _section("静态检查 Dockerfile", self.form("check", context + dockerfile + platform)),
                        _section("多架构 OCI 合并", multiarch)))

    def images(self, search="", platform_filter="", source_filter=""):
        result = library_items(self.settings)
        items = []
        for item in result["images"]:
            tags = "、".join(str(tag) for tag in item.get("tags", []))
            source_kind = item.get("source", "archive")
            if ((search and search.casefold() not in tags.casefold()) or
                    (platform_filter and platform_filter != item.get("platform")) or
                    (source_filter and source_filter != source_kind)):
                continue
            items.append(_section(_e(tags or "镜像条目"), _pairs((
                ("平台", item.get("platform", "—")), ("来源", item.get("source", item.get("storage", "归档"))),
                ("归档", item.get("archive") or "—"))) +
                ('<p>{}</p>'.format(_e(item["error"])) if item.get("error") else "") +
                "".join('<p>{} <code>{}</code></p>'.format(
                    self.delivery_link("library", selected["id"]), _e(selected["tag"]))
                    for selected in item["delivery_items"])))
        platform = _select("platform", "平台", PLATFORMS, PLATFORMS[0])
        reference = _input("reference", "镜像引用", "harbor.example/project/image:1", True)
        archive = _input("archive", "现有 tar 路径", "/data/images/app.tar", True)
        tag = _input("tag", "镜像标签（可选）")
        source = ("", "registry", "artifactory")
        filters = '<form method="get" action="{}/images">{}{}{}<button>筛选</button></form>'.format(
            self.prefix, _input("search", "标签包含", "", value=search),
            _select("platform", "筛选平台", ("",) + PLATFORMS, platform_filter, {"": "全部"}),
            _select("source", "筛选来源", ("", "local", "registry", "artifactory", "archive"), source_filter,
                    {"": "全部", "archive": "未标注来源的归档"}))
        return "".join((
            _section("镜像库位置", '<p><code>{}</code></p><p>显示 {} / {} 条</p>'.format(
                _e(result["image_store"]), len(items), len(result["images"])) + filters),
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

    def delivery_link(self, scope, identity, index=None):
        params = {"scope": scope, "id": identity}
        if index is not None:
            params["file"] = index
        return '<a href="{}/delivery?{}">产物操作</a>'.format(self.prefix, _e(urlencode(params)))

    def delivery_page(self, data):
        item = self.delivery.select(data)
        fields = (("标签", item["tag"]), ("平台", item["platform"]), ("格式", item["format"].upper()),
                  ("源归档", item["archive"] or "镜像库 CAS 引用"))
        if item.get("source"):
            fields += (("来源", item["source"]),)
        hidden = "".join('<input type="hidden" name="{}" value="{}">'.format(key, _e(data[key]))
                         for key in ("scope", "id", "file") if key in data)
        buttons = []
        for action in self.delivery.actions(item):
            content = hidden + '<input type="hidden" name="action" value="{}">'.format(action)
            if action in ("tag", "save", "optimize", "flatten"):
                content += _input("output", "新 tar 输出路径", "/data/images/release.tar", True)
            if action == "tag":
                content += _input("new-tag", "新镜像标签", "hisq_front:1.0.1", True)
            if action == "push":
                content += _input("destination", "目标 Registry / Harbor 标签", "harbor.example/project/image:1", True)
            buttons.append(_section(DELIVERY_ACTIONS[action],
                '<form class="job-form" action="{}/delivery/prepare" method="post">{}<button>{}</button></form>'.format(
                    self.prefix, content, "预览推送" if action == "push" else DELIVERY_ACTIONS[action])))
        return _section("所选产物", _pairs(fields)) + "".join(buttons)

    def push_confirmation(self, plan):
        token = self.delivery.offer(plan)
        fields = (("源归档", plan["archive"]), ("源镜像", plan["tag"]),
                  ("平台", plan["platform"]), ("目标镜像", plan["destination"]))
        content = _pairs(fields) + "<p>仅向上述仓库推送。检测到远端标签已存在时会拒绝推送。</p>"
        content += '<form action="{}/delivery/confirm" method="post"><input type="hidden" name="token" value="{}"><button>确认推送</button></form>'.format(
            self.prefix, token)
        return _section("确认推送", content)

    def submit_delivery(self, plan):
        outputs = [Path(plan["output"])] if "output" in plan else []
        return self.jobs.submit(plan["action"], [json.dumps(plan, ensure_ascii=True)], outputs,
                                (plan.get("destination") or plan.get("tag") or plan.get("label", ""))[:300], delivery=True)

    def preview_fast(self, data):
        plan = fast_plan(data, self.config_arg, self.profiles)
        return {
            "tag": plan["tag"], "base": plan["base"], "deploy": plan["deploy"],
            "outputs": list(map(str, plan["outputs"])), "conflicts": list(map(str, plan["conflicts"]))}

    def space(self, kind=None, days=30):
        usage, candidates, estimate, summary = space_report(
            self.settings, self.directory, None if kind == "cache" else kind, days)
        confirmation = ""
        if kind == "cache":
            from cache_activity import cache_activity
            root = cache_root(self.settings)
            if root.is_dir() and ((root / "entries").exists() or (root / "layers").exists()):
                with cache_activity(root, maintenance=True):
                    plan = cache_plan(root, days)
                candidates = [{"path": path, "reason": reason, "bytes": size} for path, reason, size, _ in plan]
                estimate = sum(row[2] for row in plan)
                if plan:
                    token = self.delivery.offer({"action": "cache-clean", "cache": str(root),
                                "config": str(self.config_arg) if self.config_arg is not None else None,
                                "label": str(root), "days": days, "fingerprint": cache_fingerprint(plan)})
                    confirmation = '<form method="post" action="{}/cache/confirm"><input type="hidden" name="token" value="{}"><button>确认清理以上构建缓存</button></form>'.format(self.prefix, token)
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
                shown = candidates if kind == "cache" else candidates[:200]
                details += "<ul>" + "".join("<li><code>{}</code> · {} · {} B</li>".format(
                    _e(item["path"]), _e(item.get("reason", item.get("storage", ""))),
                    _e(item.get("bytes", "—"))) for item in shown) + "</ul>"
                if len(candidates) > len(shown):
                    details += "<p>只显示前 200 项，总计 {} 项。</p>".format(len(candidates))
            elif not summary:
                details += "<p>没有符合条件的文件。</p>"
            forms += _section(SPACE_KINDS[kind], details + "<p>实际释放量受共享层和硬链接影响。</p>" + confirmation)
        return forms

    def configuration(self):
        cache = self.settings.get("cache", {})
        sections = [_section("运行位置", _pairs((
            ("配置文件", str(self.config) + ("" if self.config.is_file() else "（未创建）")),
            ("打包台账", self.directory), ("镜像库", cache.get("imageStore", "系统默认")),
            ("层缓存", cache.get("layerCache", "系统临时目录")),
            ("默认 Fast 模板", self.settings.get("fast", {}).get("defaultProfile", "—")))))]
        sections.append(_section("Fast 模板管理", '<p><a href="{}/profile">新增模板</a></p>'.format(self.prefix)))
        for name, data in sorted(self.profiles.items()):
            sections.append(_section("Fast · " + _e(name), _pairs((
                ("类型", data.get("flavor", "—")), ("基础镜像", data.get("baseImage", "—")),
                ("容器部署目录", data.get("deployDir", "默认")), ("输出目录", data.get("outputDir", "应用包所在目录")))) +
                '<p><a href="{}/profile?{}">编辑或测试模板</a></p>'.format(self.prefix, _e(urlencode({"name": name})))))
        sources = self.settings.get("repositories", {})
        if sources:
            sections.append(_section("已配置仓库", _pairs(
                (_e(host if "@" not in host else "隐藏的仓库地址"), data.get("source", "registry"))
                for host, data in sorted(sources.items()))))
        return "".join(sections)

    def profile_page(self, name=None):
        if name is not None and name not in self.profiles:
            raise BuildError("模板不存在，请刷新配置页")
        previous = self.profiles.get(name, {})
        labels = {"baseImage": "基础镜像引用", "baseTar": "本地基础 tar", "baseUrl": "Artifactory 地址",
                  "owner": "属主", "deployDir": "镜像内部署目录", "serverConfig": "TongWeb 配置文件",
                  "outputDir": "默认输出目录"}
        form = '<input type="hidden" name="mode" value="{}">'.format("edit" if name is not None else "new")
        if name is not None:
            form += '<input type="hidden" name="name" value="{}"><p>模板：<code>{}</code></p>'.format(_e(name), _e(name))
        else:
            form += _input("name", "新模板名称", "tomcat-local", True)
        form += _select("flavor", "模板类型", ("tomcat", "tongweb"), previous.get("flavor", "tomcat"))
        for field, label in labels.items():
            value = previous.get(field) or ""
            if field == "baseUrl" and ("?" in value or "@" in value):
                value = ""
            form += _input(field, label, "", field == "baseImage", value)
        form += _check("default", "设为默认 Fast 模板")
        form += _select("platform", "基础镜像测试平台", PLATFORMS, PLATFORMS[0])
        return _section("Fast 模板", '<p>本地 tar 和 Artifactory 地址二选一。测试不保存配置。</p>' +
            '<form method="post" action="{}/profile/preview">{}<button>预览配置差异</button><button formaction="{}/profile/test">测试基础镜像</button></form>'.format(
                self.prefix, form, self.prefix))

    def profile_preview(self, data):
        with candidate_profile(self.config, data) as (name, old, new, old_default, new_default, raw, _, _):
            fields = [(key, old.get(key), new.get(key)) for key in PROFILE_FIELDS if old.get(key) != new.get(key)]
            if old_default != new_default:
                fields.append(("默认模板", old_default, new_default))
            def safe(key, value):
                if key == "baseUrl" and value and ("?" in value or "@" in value):
                    return "（隐藏的旧地址）"
                return value if value is not None else "—"
            diff = _pairs((key, "{} → {}".format(safe(key, previous), safe(key, current)))
                          for key, previous, current in fields)
            if not fields:
                return _section("模板 " + _e(name), "<p>没有字段变化。</p>")
            token = self.delivery.offer({"action": "profile-save", "form": data, "revision": _revision(raw)})
            return _section("模板差异 · " + _e(name), diff +
                '<form method="post" action="{}/profile/confirm"><input type="hidden" name="token" value="{}"><button>保存配置</button></form>'.format(
                    self.prefix, token))

    def save_profile(self, plan):
        settings = save_profile(self.config, plan["form"], plan["revision"])
        self.settings, self.config_arg = settings, self.config
        self.profiles = settings["fast"].get("profiles", {})
        self.delivery.settings, self.delivery.config = settings, self.config

    def test_profile(self, data):
        platform = data.get("platform", PLATFORMS[0])
        if platform not in PLATFORMS:
            raise BuildError("无效的测试平台")
        return self.submit_delivery({"action": "profile-test", "config": str(self.config),
                                     "form": data, "platform": platform, "label": data.get("name", "")})

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
        metrics = job.get("metrics", {})
        if metrics:
            names = {"before_bytes": "处理前", "after_bytes": "处理后", "saved_bytes": "体积差（节省）",
                     "archive_bytes": "归档大小", "hidden_file_bytes": "被覆盖文件字节",
                     "duplicate_groups": "重复内容组", "large_layers": "大层编号",
                     "deleted": "已删除文件数", "bytes": "已删除文件字节"}
            content += _pairs((names[key], "{:,} B".format(value) if key.endswith("bytes") else value)
                              for key, value in metrics.items() if key in names)
        if job.get("form") and not running:
            content += '<p><a href="{}/reuse?{}">复用参数</a>，提交前可修改版本或输出。</p>'.format(
                self.prefix, urlencode({"id": identity}))
        for index, (path, _) in enumerate(job["files"]):
            content += '<p><a href="{}/download?{}">{}</a>　<code>{}</code></p>'.format(
                self.prefix, _e(urlencode({"id": identity, "file": index})),
                "下载归档" if path.suffix.lower() == ".tar" else "下载附属文件", _e(path))
        if job.get("archived"):
            content += "<p>这里只保存任务结果；成功的归档请到打包台账查看。</p>"
        else:
            content += '<h2>任务日志</h2><pre>{}</pre>'.format(_e("\n".join(job["logs"]) or "等待输出…"))
            if job["logs"]:
                content += '<p><a href="{}/job-log?{}">导出当前日志</a>　<small>分享前请检查命令输出。</small></p>'.format(
                    self.prefix, urlencode({"id": identity}))
        return _section(LABELS[job["action"]], content), running

    def submit(self, action, data):
        args, outputs = command_for(action, data, self.config_arg, self.profiles)
        label = next((data[key] for key in ("project", "tag", "reference", "archive", "context") if data.get(key)), "")
        draft = form_snapshot(action, data)
        for key in ("package", "context", "dockerfile", "base-tar", "base-map", "input-lock", "output"):
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
