"""工作台表单和展示层：配置仅展示白名单字段。"""

import hmac
import json
import os
import secrets
import sys
from collections import Counter
from functools import partial
from html import escape
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode

from errors import BuildError
from web_artifacts import artifact_state, space_report, verified_artifact
from web_job_journal import form_snapshot, task_artifact_suffixes
from web_delivery import DELIVERY_ACTIONS, Delivery, library_items
from web_jobs import FORMATS, LABELS, PLATFORMS, Jobs, _path, command_for, fast_plan, path_value
from web_maintenance import (PROFILE_FIELDS, _revision, cache_fingerprint, cache_plan, cache_root, candidate_profile,
                             image_store_root, old_image_rows, save_profile)
from web_ui import icon
from web_delivery_ui import DESCRIPTIONS, navigation, suggested_output
from web_uploads import FastUploads
from web_paths import check_oci_platform
from web_log import log_lines, log_metadata, pull_flow


STATES = {"queued": "排队中", "running": "运行中", "success": "成功", "failed": "失败",
          "cancelled": "已取消", "unknown": "之前会话未记录最终结果"}
SPACE_KINDS = {"images": "旧镜像", "cache": "过期构建缓存", "orphans": "无引用镜像层",
               "deliveries": "旧交付 tar（仅列出）"}
UPLOAD_FIELDS = {"fast": {"package": "package"}, "build": {"dockerfile": "dockerfile"},
                 "lock-inputs": {"dockerfile": "dockerfile"},
                 "check": {"dockerfile": "dockerfile"}, "load": {"archive": "archive"},
                 "inspect": {"archive": "archive"}, "verify": {"archive": "archive"},
                 "rootfs-export": {"archive": "archive"}, "rootfs-import": {"archive": "rootfs"},
                 "multiarch": {"amd64": "archive", "arm64": "archive"}}

BUILD_MODES_SCRIPT = r"""
(() => {
  const navigation = document.querySelector('[data-build-modes]');
  if (!navigation) return;
  const links = Array.from(navigation.querySelectorAll('a'));
  const panels = new Map(links.map(link => [link.hash, document.getElementById(link.hash.slice(1))]));
  if (Array.from(panels.values()).some(panel => !panel)) return;
  const fallback = '#' + navigation.dataset.buildModes;
  function select(focus) {
    const hash = panels.has(location.hash) ? location.hash : fallback;
    for (const link of links) {
      const selected = link.hash === hash;
      panels.get(link.hash).hidden = !selected;
      if (selected) link.setAttribute('aria-current', 'true');
      else link.removeAttribute('aria-current');
    }
    if (focus) panels.get(hash).querySelector('h2').focus();
  }
  navigation.addEventListener('click', event => {
    const link = event.target.closest('a');
    if (!link || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    if (location.hash !== link.hash) history.pushState(null, '', link.hash);
    select(true);
  });
  window.addEventListener('hashchange', () => select(false));
  select(false);
})();
"""

FAST_SCRIPT = r"""
(() => {
  const form = document.querySelector('form[action$="/submit/fast"]');
  if (!form) return;
  const preview = document.getElementById('fast-preview');
  const output = form.elements.output;
  const path = form.elements.package;
  const token = form.elements['package-upload'];
  const packageField = form.querySelector('[data-field="package"]');
  const original = output.placeholder;
  let timer, request, generation = 0;

  function update() {
    const current = ++generation;
    clearTimeout(timer);
    if (request) request.abort();
    output.placeholder = original;
    preview.dataset.tone = '';
    if (packageField.hasAttribute('data-staging')) {
      preview.textContent = '应用包暂存完成后继续预览。';
      return;
    }
    if ((!token.value && !path.value.trim()) ||
        !['project', 'version'].every(key => form.elements[key].value.trim())) {
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
        if (!response.ok) {
          preview.dataset.tone = 'alert';
          preview.textContent = plan.error || '无法预览';
          return;
        }
        output.placeholder = plan.outputs[0];
        preview.dataset.tone = plan.conflicts.length ? 'alert' : '';
        preview.textContent = ['标签：' + plan.tag, '基础镜像：' + plan.base, '部署：' + plan.deploy,
          '输出：' + plan.outputs.join('、'), plan.conflicts.length ? '已存在：' + plan.conflicts.join('、') : '']
          .filter(Boolean).join('\n');
      } catch (error) {
        if (generation === current && error.name !== 'AbortError') preview.textContent = '暂时无法预览，提交时仍会校验。';
      }
    }, 350);
  }
  form.addEventListener('path-input-change', update);
  form.addEventListener('input', update);
  form.addEventListener('change', update);
  update();
})();
"""


JOB_SCRIPT = r"""
(() => {
  const output = document.getElementById('job-output');
  if (!output) return;
  const latest = document.getElementById('job-latest');
  const follow = document.getElementById('job-follow');
  const details = document.getElementById('job-details');
  const status = document.getElementById('job-status');
  const flow = document.getElementById('job-flow');
  const pollStatus = document.getElementById('job-poll-status');
  const exportLog = document.getElementById('job-export');
  const omitted = document.getElementById('job-log-omitted');
  let after = Number(output.dataset.after);
  let first = after - output.childElementCount;
  let detailsHTML = details.innerHTML;
  let flowHTML = flow ? flow.innerHTML : '';
  let following = true;
  let resizing = false;

  function showFollow() {
    const message = following ? '正在跟随最新日志' : '已暂停跟随，可继续查看上方日志';
    if (follow.textContent !== message) follow.textContent = message;
  }
  function jump() {
    following = true;
    output.scrollTop = output.scrollHeight;
    showFollow();
  }
  function atBottom() {
    return output.scrollHeight - output.clientHeight - output.scrollTop <= 2;
  }
  latest.hidden = false;
  latest.addEventListener('click', jump);
  output.addEventListener('scroll', () => {
    if (resizing) return;
    following = atBottom();
    showFollow();
  });
  window.addEventListener('resize', () => {
    const resume = following;
    resizing = true;
    requestAnimationFrame(() => {
      if (resume) jump();
      resizing = false;
    });
  });
  jump();

  async function poll() {
    let again = true;
    const request = new AbortController();
    const timeout = setTimeout(() => request.abort(), 10000);
    try {
      const response = await fetch(output.dataset.url + '&after=' + after, {signal: request.signal});
      if (response.status === 404) {
        again = false;
        output.dataset.running = '0';
        pollStatus.textContent = '此任务已不在当前会话，已停止更新。';
        pollStatus.hidden = false;
        return;
      }
      if (!response.ok) throw new Error('Cannot read job progress');
      const next = await response.json();
      if (next.details !== detailsHTML) {
        details.innerHTML = next.details;
        detailsHTML = next.details;
      }
      if (status.textContent !== next.status) status.textContent = next.status;
      if (flow && typeof next.flow === 'string' && next.flow !== flowHTML) {
        flow.innerHTML = next.flow;
        flowHTML = next.flow;
      }
      if (!next.archived && next.end !== after) {
        following = atBottom();
        const top = output.scrollTop;
        const removed = Math.max(0, next.start - first);
        let keepTop = top;
        // 日志窗口淘汰旧行时，扣掉实际高度，保留仍在窗口内的阅读位置。
        if (!following && removed) {
          const retained = output.children[removed];
          keepTop = retained ? Math.max(0, top - (retained.getBoundingClientRect().top -
            output.firstElementChild.getBoundingClientRect().top)) : 0;
        }
        for (let i = 0; i < removed && output.firstElementChild; i++) {
          output.firstElementChild.remove();
        }
        const fragment = document.createDocumentFragment();
        const kinds = ['output', 'stage', 'success', 'error', 'cache', 'summary', 'warning'];
        const metadata = Array.isArray(next.line_meta) ? next.line_meta : [];
        for (let i = 0; i < next.lines.length; i++) {
          const line = document.createElement('span');
          const meta = metadata[i] || {};
          line.className = 'log-line log-' + (kinds.includes(meta.kind) ? meta.kind : 'output');
          line.dataset.line = String(next.end - next.lines.length + i + 1);
          line.dataset.time = typeof meta.time === 'string' ? meta.time.slice(0, 16) : '--:--';
          line.textContent = next.lines[i];
          fragment.appendChild(line);
        }
        output.appendChild(fragment);
        first = next.start;
        after = next.end;
        if (first) {
          omitted.textContent = String(first);
          omitted.parentElement.hidden = false;
        }
        if (following) jump();
        else {
          output.scrollTop = keepTop;
          showFollow();
        }
      }
      exportLog.hidden = next.archived || !output.childElementCount;
      again = next.running;
      if (!again) output.dataset.running = '0';
      pollStatus.hidden = true;
    } catch (error) {
      pollStatus.textContent = '暂时无法更新日志，正在重试…';
      pollStatus.hidden = false;
    } finally {
      clearTimeout(timeout);
      if (again) setTimeout(poll, 2000);
    }
  }
  if (output.dataset.running === '1') setTimeout(poll, 2000);
})();
"""


def _e(value):
    return escape(str(value))


def _input(name, label, placeholder="", required=False, value=""):
    wide = name in ("package", "context", "archive", "baseUrl")
    return '<label data-field="{}"{}>{}<input name="{}" placeholder="{}" value="{}" {}></label>'.format(
        name, ' class="field-wide"' if wide else "", label, name, _e(placeholder), _e(value),
        "required" if required else "")


def _path_field(name, label, placeholder="", required=False, value="", mode="file", upload=None, base=""):
    chooser = '<button type="button" class="secondary" data-browse hidden>浏览{}</button>'.format(
        "目录" if mode != "file" else "本机")
    if upload:
        accept = {"package": ".war,.zip", "dockerfile": "",
                  "rootfs": ".tar,.tar.gz,.tar.bz2,.tar.xz,.tgz"}.get(upload, ".tar")
        chooser += ('<button type="button" class="secondary" data-pick hidden>选择文件</button>'
                    '<button type="button" class="secondary" data-reset-pick hidden>清除</button>'
                    '<input type="file" data-picker{} hidden><input type="hidden" name="{}-upload">'
                    ).format(' accept="{}"'.format(accept) if accept else "", name)
    return ('<div class="path-field" data-field="{}" data-path-mode="{}" data-upload-kind="{}" data-path-base="{}">'
            '<div class="package-controls">{}{}</div><small data-path-status role="status"></small>{}</div>').format(
                name, mode, upload or "", _e(base), _input(name, label, placeholder, required, value), chooser,
                '<small>可输入路径，也可把文件拖到这里。</small>' if upload else "")


def _select(name, label, options, selected="", labels=None):
    choices = "".join('<option value="{}"{}>{}</option>'.format(
        _e(value), " selected" if value == selected else "", _e((labels or {}).get(value, value or "自动"))) for value in options)
    return '<label data-field="{}">{}<select name="{}">{}</select></label>'.format(name, label, name, choices)


def _check(name, label, checked=False):
    return '<label class="check"><input type="checkbox" name="{}" value="1"{}>{}</label>'.format(
        name, " checked" if checked else "", label)


def _change_field():
    return ('<label>运行配置变更（可选，每行一条指令）'
            '<textarea name="changes" rows="4" maxlength="8192" '
            'placeholder="ENV APP_ENV=production&#10;EXPOSE 8089"></textarea></label>')


def _section(title, body, anchor="", primary=False, description=""):
    attrs = (' id="{}"'.format(_e(anchor)) if anchor else "") + (' class="primary-panel"' if primary else "")
    description = '<p class="section-copy">{}</p>'.format(_e(description)) if description else ""
    return '<article{}><h2{}>{}</h2>{}{}</article>'.format(
        attrs, ' tabindex="-1"' if anchor else "", title, description, body)


def _pairs(values):
    return "<dl>" + "".join("<dt>{}</dt><dd><code>{}</code></dd>".format(label, _e(value))
                            for label, value in values) + "</dl>"


def _tool_group(title, description, body):
    return ('<details class="tool-group"><summary><strong>{}</strong><small>{}</small></summary>'
            '<div class="tool-group-body">{}</div></details>').format(title, description, body)


def _job_state(state):
    return '<span class="state" data-state="{}">{}</span>'.format(state, STATES[state])


def _size_label(size):
    for unit, scale in (("GiB", 1024 ** 3), ("MiB", 1024 ** 2), ("KiB", 1024)):
        if size >= scale:
            return "{:.1f} {}".format(size / scale, unit)
    return "{:,} B".format(size)


class Workbench:
    def __init__(self, settings, config, directory):
        self.settings, self.config, self.directory = settings, config, directory
        self.prefix = "/session/" + secrets.token_urlsafe(24)
        self.config_arg = config if config.is_file() else None
        self.profiles = settings.get("fast", {}).get("profiles", {})
        self.delivery = Delivery(settings, self.config_arg, directory, self.get_job)
        self.uploads = FastUploads()
        self.jobs = Jobs(directory)

    def nav(self, active=""):
        links = "".join('<a href="{}{}"{}>{}<span>{}</span></a>'.format(
            self.prefix, path, ' aria-current="page"' if key == active else "", icon(key), label)
            for key, path, label in (("build", "/workbench", "构建"), ("jobs", "/jobs", "任务"),
            ("images", "/images", "镜像库"), ("history", "/history", "打包台账"),
            ("space", "/space", "空间"), ("config", "/config", "配置")))
        return ('<aside class="sidebar"><a class="brand" href="{}/workbench"><span class="brand-icon">{}</span>'
                '<span class="brand-text">PyImageBuilder<span class="brand-sub">镜像构建工作台</span></span></a>'
                '<nav class="tabs" aria-label="主导航">{}</nav>'
                '<div class="sidebar-foot"><span class="dot"></span>仅在本机运行</div></aside>').format(
                    self.prefix, icon("logo"), links)

    def form(self, action, body, label=None, secondary=""):
        return ('<form class="job-form" data-action="{0}" method="post" action="{1}/submit/{0}">{2}'
                '<div class="form-footer"><button type="submit"{3}>{4}</button>{5}</div></form>').format(
            action, self.prefix, body, ' class="danger"' if action == "remove" else "", label or LABELS[action],
            secondary)

    def builds(self, action=None, draft=None):
        draft = draft or {}

        def field(name, label, placeholder="", required=False):
            modes = {"output": "output", "context": "directory", "dockerfile": "file",
                     "base-tar": "file", "base-map": "file", "input-lock": "file", "sign-key": "file",
                     "lock-output": "output-json"}
            if name in modes:
                return _path_field(name, label, placeholder, required, draft.get(name, ""),
                                   modes[name], "dockerfile" if name == "dockerfile" else None)
            return _input(name, label, placeholder, required, draft.get(name, ""))

        platform = _select("platform", "平台", PLATFORMS, draft.get("platform", PLATFORMS[0]))
        formats = _select("format", "归档格式", FORMATS, draft.get("format", FORMATS[0]),
                          {"docker": "Docker tar", "oci": "OCI tar", "both": "Docker + OCI"})
        destination = field("output", "输出 tar 路径", "/data/images/hisq_front-1.0.0.tar", True)
        context = field("context", "构建上下文目录", "/data/project", True)
        needs_dockerfile = action == "build" and "dockerfile" not in draft
        dockerfile = field("dockerfile", "Dockerfile" if needs_dockerfile else "Dockerfile（可选）",
                           "请重新选择文件" if needs_dockerfile else "默认使用上下文中的 Dockerfile",
                           needs_dockerfile)
        if self.profiles:
            default = draft.get("profile", self.settings["fast"].get("defaultProfile", ""))
            package = _path_field("package", "WAR／dist.zip 路径", "/data/packages/dist.zip", True,
                                  draft.get("package", ""), upload="package")
            fast = self.form("fast", package
                             + field("project", "项目", "hisq", True) + field("version", "版本", "1.0.0", True)
                             + _select("profile", "模板", sorted(self.profiles), default)
                             + platform + formats
                             + field("output", "输出 tar 路径（可选）", "留空按模板自动生成") +
                             '<p id="fast-preview" class="preview" aria-live="polite">输出路径留空时，按模板自动生成。</p>',
                             "开始快速打包")
        else:
            fast = ('<p class="muted">新增 Tomcat 或 TongWeb 模板后，即可打包 WAR 或 dist.zip。</p>'
                    '<a class="button primary" href="{}/profile">新增 Fast 模板</a>').format(self.prefix)
        ordinary = (context + dockerfile + field("tag", "镜像标签", "hisq_back:1.0.0", True) +
                    platform + formats + destination)
        checks = (_check("offline", "仅使用本地基础镜像", draft.get("offline")) +
                  _check("pull", "刷新基础镜像", draft.get("pull")) +
                  _check("no-cache", "禁用构建缓存", draft.get("no-cache")))
        if sys.platform == "linux":
            checks += _check("run", "允许执行可信 Dockerfile 的 RUN", draft.get("run"))
        show_advanced = any(draft.get(key) for key in ("base-tar", "base-map", "target", "epoch", "input-lock",
                                                       "offline", "pull", "no-cache", "run", "attest"))
        ordinary += ('<details class="advanced"{}><summary>高级构建选项</summary><div class="advanced-grid">'
                     '<fieldset><legend>基础镜像与执行</legend>'.format(" open" if show_advanced else "") +
                     field("base-tar", "基础镜像 tar（可选）") +
                     field("base-map", "基础镜像映射 JSON") +
                     field("target", "目标阶段（可选）") +
                     '<div class="build-checks">' + checks + '</div></fieldset>' +
                     '<fieldset><legend>可复现与封闭构建</legend>' +
                     field("epoch", "可复现时间戳（Unix 秒）") +
                     field("input-lock", "封闭构建输入锁 JSON") +
                     field("lock-output", "生成输入锁的输出路径", "/data/images/input-lock.json") +
                     '<p class="muted">生成输入锁时须填时间戳，输出路径须位于上下文之外。</p></fieldset>' +
                     '<fieldset class="build-params"><legend>构建参数与签名</legend>' +
                     '<label>构建参数（每行 NAME=VALUE，不填凭据）<textarea name="build-args" rows="3"></textarea></label>' +
                     _check("attest", "生成 SBOM 和 provenance", draft.get("attest")) +
                     field("sign-key", "本机签名私钥路径（可选）") + "</fieldset></div></details>")
        lock_button = ('<button type="submit" class="secondary" formnovalidate data-lock-submit '
                       'formaction="{}/submit/lock-inputs">生成输入锁</button>'
                       '<small class="form-feedback" data-lock-feedback role="status"></small>').format(self.prefix)
        if action == "fast":
            return _section("复用快速打包参数", fast)
        if action == "build":
            note = ""
            if needs_dockerfile:
                note = '<p class="muted">上次的 Dockerfile 路径未保存。请先重新选择文件，再提交构建。</p>'
                ordinary += '<input type="hidden" name="require-dockerfile" value="1">'
            if draft.get("input-lock"):
                note += '<p class="muted">如原任务使用了构建参数，请按原值重新填写。</p>'
            if draft.get("input-lock-job"):
                note += '<p class="muted">构建时将核对原任务登记的输入锁摘要。要使用另一份锁，请从构建页重新选择。</p>'
                ordinary += '<input type="hidden" name="input-lock-job" value="{}">'.format(_e(draft["input-lock-job"]))
            return _section("复用 Dockerfile 构建参数", note + self.form("build", ordinary, secondary=lock_button))
        multiarch = self.form("multiarch", _path_field("amd64", "amd64 OCI tar", required=True, upload="amd64") +
                              _path_field("arm64", "arm64 OCI tar", required=True, upload="arm64") +
                              _input("tag", "多平台镜像标签", "", True) + destination +
                              _input("epoch", "可复现时间戳（可选）"))
        modes = (("fast", "package", "快速打包", "WAR 或 dist.zip"),
                 ("dockerfile", "build", "Dockerfile 构建", "从项目目录生成镜像"),
                 ("check", "check", "静态检查", "检查 Dockerfile 和构建输入"),
                 ("multiarch", "images", "合并 OCI", "合并 amd64 与 arm64 归档"))
        navigation = ('<nav class="build-modes" aria-label="构建方式" data-build-modes="{}">{}</nav>').format(
            "fast" if self.profiles else "dockerfile",
            "".join('<a href="#{}"><span class="mode-icon">{}</span><span><strong>{}</strong><small>{}</small></span></a>'.format(
                key, icon(symbol), label, summary) for key, symbol, label, summary in modes))
        return "".join((navigation, _section("快速打包", fast, "fast", True, "选择应用包和模板，预览输出后提交。"),
                        _section("Dockerfile 构建", self.form("build", ordinary, secondary=lock_button), "dockerfile",
                                 description="填写构建上下文、镜像标签和输出路径。可在高级选项中指定基础镜像和输入锁。"),
                        _section("静态检查 Dockerfile", self.form("check", context + dockerfile + platform), "check",
                                 description="检查 Dockerfile 和构建输入。"),
                        _section("多架构 OCI 合并", multiarch, "multiarch",
                                 description="使用已有的 amd64 和 arm64 OCI 归档，生成一个多平台产物。")))

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
                '<div class="actions">' + "".join('<span>{} <code>{}</code></span>'.format(
                    self.delivery_link("library", selected["id"]), _e(selected["tag"]))
                    for selected in item["delivery_items"]) + "</div>"))
        platform = _select("platform", "平台", PLATFORMS, PLATFORMS[0])
        reference = _input("reference", "镜像引用", "harbor.example/project/image:1", True)
        archive = _path_field("archive", "现有 tar 路径", "/data/images/app.tar", True, upload="archive")
        tag = _input("tag", "镜像标签（可选）")
        output = _path_field("output", "输出 tar 路径", required=True, mode="output")
        rootfs = _path_field("archive", "rootfs 归档", "/data/images/rootfs.tar", True, upload="rootfs")
        source = ("", "registry", "artifactory")
        filters = '<form method="get" action="{}/images">{}{}{}<button>筛选</button></form>'.format(
            self.prefix, _input("search", "标签包含", "", value=search),
            _select("platform", "筛选平台", ("",) + PLATFORMS, platform_filter, {"": "全部"}),
            _select("source", "筛选来源", ("", "local", "registry", "artifactory", "archive"), source_filter,
                    {"": "全部", "archive": "未标注来源的归档"}))
        registry_tools = _tool_group("仓库", "拉取镜像、测试连接",
            _section("拉取基础镜像", self.form("pull", reference + platform + _select("source", "来源", source))) +
            self.repository_diagnostics())
        archive_tools = _tool_group("镜像归档", "导入、导出、查看和验签",
            _section("导入 Docker tar", self.form("load", archive + tag + platform)) +
            _section("从镜像库导出 tar", self.form("save", reference + platform +
                     _select("source", "来源", source + ("local",)) + output)) +
            _section("检查现有 Docker tar", self.form("inspect", archive + tag) + self.form("verify", archive + tag)) +
            _section("交付验签", '<a class="button" href="{}/attest">打开验签工具</a>'.format(self.prefix)))
        rootfs_tools = _tool_group("rootfs 文件系统", "导出文件系统、从 rootfs 创建镜像",
            _section("导出 rootfs", self.form("rootfs-export", archive + tag + platform + output),
                     description="从 Docker 或 OCI 镜像归档导出合并后的文件系统。需要 docker load 时，先用下方工具生成 Docker 镜像 tar。") +
            _section("从 rootfs 创建镜像", self.form("rootfs-import", rootfs +
                     _input("tag", "新镜像标签", "example/app:1.0.0", True) + platform +
                     _select("format", "归档格式", ("docker", "oci"), "docker") +
                     _input("epoch", "可复现时间戳（可选）") + _change_field() + output)))
        maintenance = _tool_group("本地维护", "查看磁盘占用、删除镜像引用",
            _section("磁盘占用", self.form("df", "")) +
            _section("删除本地镜像引用", self.form("remove", reference + platform +
                     _select("source", "来源", source + ("local",)) +
                     _input("confirm", "再次输入完整镜像引用", "", True))))
        overview = _section("镜像列表", '<p class="muted">显示 {} / {} 条 · <code>{}</code></p>'.format(
            len(items), len(result["images"]), _e(result["image_store"])) + filters)
        tools = ('<details class="toolbox"><summary>镜像库工具</summary><div class="toolbox-body">{}'
                 '</div></details>').format(registry_tools + archive_tools + rootfs_tools + maintenance)
        results = '<div class="image-results">{}</div>'.format("".join(items)) if items else '<p class="empty">没有符合条件的镜像。</p>'
        return overview + tools + results

    def delivery_link(self, scope, identity, index=None):
        params = {"scope": scope, "id": identity}
        if index is not None:
            params["file"] = index
        return '<a class="button compact" href="{}/delivery?{}">产物操作</a>'.format(self.prefix, _e(urlencode(params)))

    def delivery_page(self, data):
        item = self.delivery.select(data)
        fields = (("标签", item["tag"]), ("平台", item["platform"]), ("格式", item["format"].upper()),
                  ("源归档", item["archive"] or "镜像库 CAS 引用"))
        if item.get("source"):
            fields += (("来源", item["source"]),)
        hidden = "".join('<input type="hidden" name="{}" value="{}">'.format(key, _e(data[key]))
                         for key in ("scope", "id", "file") if key in data)
        buttons = []
        actions = self.delivery.actions(item)
        for action in actions:
            content = hidden + '<input type="hidden" name="action" value="{}">'.format(action)
            suggestion = suggested_output(item, action)
            if suggestion:
                mode = "output-gzip" if action == "compress" else "output-archive" if action == "slim" else "output"
                content += _path_field("output", "新压缩归档路径" if action == "compress" else "新 tar 输出路径",
                                       suggestion, True, mode=mode)
                content += '<button type="button" class="secondary suggest-output" data-suggest-output="{}">使用建议名称</button>'.format(_e(suggestion))
            if action == "compress":
                content += '<p class="muted">只压缩原 tar，不合层、不改配置与镜像 ID。填写 .tar 时追加 .gz；另存 .compress.json 校验报告。</p>'
                content += _select("compress-level", "gzip 压缩级别", tuple(str(n) for n in range(1, 10)), "6")
                content += _input("upload-limit", "解压后上传限制（字节）", value="2000000000")
            if action == "rootfs-export":
                content += '<p class="muted">输出只包含合并后的文件系统，可用于 rootfs 导入。</p>'
            if action == "flatten":
                content += _change_field()
            if action == "slim":
                content += ('<p class="muted">默认只扁平化。规则只作用于新镜像，保留优先；自动保护不能保证应用一定可运行。'
                            '勾选 gzip 时 .tar 路径自动加 .gz；旁边另存 .slim.json 完整审计报告。</p>'
                            '<label>删除规则（每行一条）<textarea name="delete" rows="3" maxlength="8192" '
                            'placeholder="/usr/share/doc&#10;/var/cache"></textarea></label>'
                            '<label>保留规则（优先于删除）<textarea name="keep" rows="3" maxlength="8192" '
                            'placeholder="/app/config"></textarea></label>')
                content += _check("analyze-only", "仅预览并生成审计报告，不写镜像", True)
                content += _check("gzip", "生成 gzip 压缩归档")
                content += _check("no-auto-protect", "关闭运行路径自动保护，仅使用手工保留规则")
                content += _select("compress-level", "gzip 压缩级别", tuple(str(n) for n in range(1, 10)), "6")
                content += _input("upload-limit", "解压后上传限制（字节）", value="2000000000")
            if action == "tag":
                content += _input("new-tag", "新镜像标签", "hisq_front:1.0.1", True)
            if action == "push":
                content += _input("destination", "目标 Registry / Harbor 标签", "harbor.example/project/image:1", True)
            buttons.append(
                '<article class="operation-panel" id="operation-{0}" data-operation-panel="{0}">'
                '<header><div><span class="operation-impact">{1}</span><h2 tabindex="-1">{2}</h2>'
                '<p class="muted">{3}</p></div></header>'
                '<form class="job-form" action="{4}/delivery/prepare" method="post">{5}'
                '<div class="form-footer"><button>{6}</button><small class="muted">提交后可在任务页查看进度和结果。</small>'
                '</div></form></article>'.format(action, _e(DESCRIPTIONS[action][2]), _e(DELIVERY_ACTIONS[action]),
                                                _e(DESCRIPTIONS[action][1]), self.prefix, content,
                                                "预览推送" if action == "push" else DELIVERY_ACTIONS[action]))
        if item["format"] in ("docker", "oci"):
            buttons.append(_section("交付验签", '<a class="button" href="{}/attest?{}">校验签名和产物摘要</a>'.format(
                self.prefix, _e(urlencode({key: data[key] for key in ("scope", "id", "file") if key in data})))))
        default = "compress" if "compress" in actions else "save" if "save" in actions else "verify"
        source = ('<article class="delivery-source"><div class="delivery-source-heading">{0}'
                  '<div><span class="eyebrow">当前产物</span><h2>{1}</h2>'
                  '<div class="meta"><span class="badge">{2}</span><span>{3}</span></div></div></div>'
                  '<details><summary>查看来源与完整路径</summary>{4}</details></article>').format(
                      icon("package", 26), _e(item["tag"]), _e(item["format"].upper()), _e(item["platform"]), _pairs(fields))
        return ('<div data-delivery-workspace data-default-operation="{}">{}{}'
                '<div class="operation-panels">{}</div></div>').format(
                    default, source, navigation(actions), "".join(buttons))

    def attestation_page(self, data):
        fields = (("public-key", "可信公钥"), ("provenance", "Provenance"), ("envelope", "DSSE 签名"),
                  ("sbom", "SPDX SBOM"), ("docker-archive", "Docker tar"), ("oci-archive", "OCI tar"))
        preset = {}
        if data.get("job"):
            job = self.get_job(data["job"])
            if job is None or job["state"] != "success" or job["action"] != "build":
                raise BuildError("未找到已完成的签名构建任务")
            for item in job.get("artifacts", []):
                for suffix, name in ((".provenance.json", "provenance"), (".dsse.json", "envelope"),
                                     (".spdx.json", "sbom")):
                    if item["path"].lower().endswith(suffix):
                        preset[name] = item["path"]
            archives = [item["path"] for item in job.get("artifacts", []) if item["path"].lower().endswith(".tar")]
            format_ = job.get("form", {}).get("format", "docker")
            archive_fields = (("docker-archive", "oci-archive") if format_ == "both" else
                              ("oci-archive" if format_ == "oci" else "docker-archive",))
            if len(archive_fields) == len(archives):
                preset.update(zip(archive_fields, archives))
        elif data:
            item = self.delivery.select(data)
            if item["format"] not in ("docker", "oci"):
                raise BuildError("请选择需要验签的 Docker 或 OCI 归档")
            preset[item["format"] + "-archive"] = item["archive"]
        inputs = "".join(_path_field(name, label, required=not name.endswith("-archive"), value=preset.get(name, ""))
                         for name, label in fields)
        return _section("签名与产物校验",
                        "<p>请选择从可信渠道获得的公钥。若签名覆盖 Docker 和 OCI 两份归档，请同时提供。</p>"
                        + self.form("attest-verify", inputs, "开始验签"))

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
        if plan["action"] == "compress":
            outputs += [Path(plan["report"])]
        if plan["action"] == "slim":
            outputs = ([] if plan["analyze_only"] else outputs) + [Path(plan["report"])]
        return self.jobs.submit(plan["action"], [json.dumps(plan, ensure_ascii=True)], outputs,
                                (plan.get("destination") or plan.get("tag") or plan.get("label", ""))[:300], delivery=True)

    def preview_fast(self, data):
        token = self._upload_token(data)
        package = self.uploads.path(token) if token else None
        plan = fast_plan(data, self.config_arg, self.profiles, package)
        return {
            "tag": plan["tag"], "base": plan["base"], "deploy": plan["deploy"],
            "outputs": list(map(str, plan["outputs"])), "conflicts": list(map(str, plan["conflicts"]))}

    def space(self, kind=None, days=30):
        usage, candidates, estimate, summary = space_report(
            self.settings, self.directory, None if kind == "cache" else kind, days)
        confirmation = ""
        if kind == "images" and candidates:
            root = image_store_root(self.settings)
            rows = old_image_rows(root, candidates, days)
            if rows:
                token = self.delivery.offer({"action": "image-clean", "root": str(root), "rows": rows,
                    "config": str(self.config_arg) if self.config_arg is not None else None})
                choices = "".join(_check("pick-{}".format(index),
                    _e(" · ".join(row["key"]) + " · " + _size_label(row["snapshot"][1])))
                    for index, row in enumerate(rows))
                confirmation = ('<p>最多显示 20 个可确认的镜像。确认后删除勾选的引用和归档，共享层单独预览。</p>'
                    '<form method="post" action="{}/image-clean/confirm"><input type="hidden" name="token" value="{}">'
                    '{}<button class="danger">确认删除选中的镜像</button></form>').format(self.prefix, token, choices)
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
                    confirmation = '<form method="post" action="{}/cache/confirm"><input type="hidden" name="token" value="{}"><button class="danger">确认清理以上构建缓存</button></form>'.format(self.prefix, token)
        forms = '<form method="get" action="{}/space"><label>文件年龄（天）<input name="days" type="number" min="0" max="36500" value="{}" required></label>{}<button>预览</button></form>'.format(
            self.prefix, days, _select("kind", "检查范围", tuple(SPACE_KINDS), kind or "images", SPACE_KINDS))
        tiles = []
        for key, title in (("image_store", "镜像库"), ("layer_cache", "构建缓存"), ("deliveries", "已登记的交付 tar")):
            item = usage[key]
            tiles.append(_section(title, '<div class="tile-value">{}</div><p class="muted">{} 个文件 · {:,} B</p>'
                                  '<code class="tile-path">{}</code>'.format(
                                      _size_label(item["bytes"]), item["files"], item["bytes"],
                                      _e(item.get("path", "台账中存在的库外归档")))))
        forms = '<div class="tiles">{}</div>{}'.format("".join(tiles), _section("清理预览", forms))
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

    def confirm_image_cleanup(self, fields):
        def enqueue(plan):
            selected = []
            for name, value in fields.items():
                if name == "token":
                    continue
                index = name[5:] if name.startswith("pick-") else ""
                if value != "1" or not index.isascii() or not index.isdecimal() or len(index) > 2:
                    raise BuildError("无效的镜像选择")
                index = int(index)
                if not 0 <= index < len(plan["rows"]) or name != "pick-{}".format(index):
                    raise BuildError("无效的镜像选择")
                selected.append(plan["rows"][index])
            if not selected:
                raise BuildError("请至少勾选一个镜像")
            selected.sort(key=lambda row: row["key"])
            return self.submit_delivery({"action": "image-clean", "root": plan["root"], "config": plan["config"],
                "keys": [row["key"] for row in selected],
                "fingerprint": cache_fingerprint([row["snapshot"] for row in selected])})

        return self.delivery.confirm(fields.get("token"), "image-clean", enqueue)

    def configuration(self):
        cache = self.settings.get("cache", {})
        sections = [_section("运行位置", _pairs((
            ("配置文件", str(self.config) + ("" if self.config.is_file() else "（未创建）")),
            ("打包台账", self.directory), ("镜像库", cache.get("imageStore", "系统默认")),
            ("层缓存", cache.get("layerCache", "系统临时目录")),
            ("默认 Fast 模板", self.settings.get("fast", {}).get("defaultProfile", "—")))))]
        sections.append(_section("Fast 模板管理", '<a class="button primary" href="{}/profile">新增模板</a>'.format(self.prefix)))
        for name, data in sorted(self.profiles.items()):
            sections.append(_section("Fast · " + _e(name), _pairs((
                ("类型", data.get("flavor", "—")), ("基础镜像", data.get("baseImage", "—")),
                ("容器部署目录", data.get("deployDir", "默认")), ("输出目录", data.get("outputDir", "应用包所在目录")))) +
                '<a class="button compact" href="{}/profile?{}">编辑或测试模板</a>'.format(self.prefix, _e(urlencode({"name": name})))))
        sources = self.settings.get("repositories", {})
        if sources:
            sections.append(_section("已配置仓库", _pairs(
                (_e(host if "@" not in host else "隐藏的仓库地址"), data.get("source", "registry"))
                for host, data in sorted(sources.items()))))
            sections.append(self.repository_diagnostics())
        return "".join(sections)

    def repository_diagnostics(self):
        from registry import parse_reference
        hosts = []
        for host in sorted(self.settings.get("repositories", {})):
            try:
                parse_reference(host + "/pyimagebuilder/probe:latest")
            except BuildError:
                continue
            hosts.append(host)
        if not hosts:
            return ""
        from settings import repository_settings
        labels = {}
        for host in hosts:
            try:
                entry = repository_settings(self.settings, host)
                protocol = ("HTTP" if entry.get("insecureHttp", False) else
                            "HTTPS" if entry.get("verifyTls", True) else "HTTPS（不校验证书）")
            except BuildError:
                entry, protocol = self.settings["repositories"][host], "协议待明确配置"
            labels[host] = "{} · {} · {}".format(host, protocol,
                "Artifactory" if entry.get("source") == "artifactory" else "Registry")
        form = _select("host", "仓库", hosts, hosts[0], labels)
        form += _input("reference", "远端镜像引用（可选）", "harbor.example/project/app:1")
        return _section("仓库诊断", '<p class="muted">留空只检查仓库 API。填写镜像引用可检查标签和读取权限。'
                        '连接协议以仓库配置为准，请与实际服务地址核对。</p>'
                        + self.form("repo-test", form, "开始诊断"))

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
            if field in ("baseTar", "serverConfig", "outputDir"):
                form += _path_field(field, label, value=value, mode="directory" if field == "outputDir" else "file",
                                    base=self.config.parent)
            else:
                form += _input(field, label, "", field == "baseImage", value)
        form += _check("default", "设为默认 Fast 模板")
        form += _select("platform", "基础镜像测试平台", PLATFORMS, PLATFORMS[0])
        return _section("Fast 模板", '<p>本地 tar 和 Artifactory 地址二选一。测试不保存配置。</p>' +
            '<form method="post" action="{}/profile/preview">{}<div class="form-footer"><button>预览配置差异</button><button class="secondary" formaction="{}/profile/test">测试基础镜像</button></div></form>'.format(
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

    def tasks(self, state="", search="", fragment=False):
        jobs = self.jobs.snapshot()
        active = {item["id"] for item in jobs}
        jobs += [self._archived(item) for item in self.jobs.journal.recent() if item["id"] not in active]
        jobs = [job for job in jobs if job["action"] in LABELS]
        totals = Counter(job["state"] for job in jobs)
        jobs = [job for job in jobs if (not state or job["state"] == state) and
                (not search or search.casefold() in (job["label"] + " " + LABELS[job["action"]]).casefold())]
        stats = '<dl class="task-stats" aria-label="最近任务概况">{}</dl>'.format("".join(
            '<div><dt>{}</dt><dd>{}</dd></div>'.format(label, count)
            for label, count in (("最近任务", sum(totals.values())), ("排队或运行中", totals["queued"] + totals["running"]),
                                 ("成功", totals["success"]), ("失败", totals["failed"]))))

        def elapsed(job):
            seconds = job.get("elapsed")
            if seconds is None:
                return ""
            return '<small>耗时 {:02d}:{:02d}:{:02d}</small>'.format(seconds // 3600, seconds // 60 % 60, seconds % 60)

        rows = "".join(('<li class="job-row"><div class="job-ident"><div>{} <strong>{}</strong></div><code>{}</code></div>'
                        '<div class="actions">{} <a class="button compact" href="{}/job?{}">查看任务</a></div></li>').format(
                            _job_state(job["state"]), LABELS[job["action"]],
                            _e(job["label"] or job.get("updated", "")), elapsed(job), self.prefix,
                            urlencode({"id": job["id"]})) for job in jobs)
        if rows:
            body = '<ul class="job-list">{}</ul>'.format(rows)
        elif state or search:
            body = '<p class="muted">没有符合条件的网页任务。</p>'
        else:
            body = ('<div class="empty"><p>还没有网页任务。</p>'
                    '<a class="button primary" href="{}/workbench">去构建</a></div>').format(self.prefix)
        body = stats + body
        if fragment:
            return body
        query = _e(urlencode({"state": state, "search": search}))
        filters = ('<form method="get" action="{}/jobs">{}{}<button>筛选</button></form>'.format(
            self.prefix, _select("state", "状态", ("",) + tuple(STATES), state, {"": "全部", **STATES}),
            _input("search", "项目、标签或任务类型", value=search)))
        return _section("任务列表", filters +
                        '<div data-task-feed="{}/jobs-fragment?{}">{}</div><small id="task-feed-status" role="status"></small>'.format(
                            self.prefix, query, body))

    def _archived(self, item):
        state = "unknown" if item["state"] in ("queued", "running") else item["state"]
        return dict(item, state=state, status=item["updated"], logs=[], files=[], archived=True)

    def get_job(self, identity):
        live = self.jobs.snapshot(identity)
        if live is not None:
            return live
        item = self.jobs.journal.read(identity)
        return self._archived(item) if item is not None and item["action"] in LABELS else None

    def job_details(self, job):
        identity, state = job["id"], job["state"]
        running = state in ("queued", "running")
        content = '<div class="job-summary">{} <code>{}</code></div>'.format(_job_state(state), _e(job["label"]))
        if state == "queued" or (state == "running" and os.name == "posix" and not job.get("interrupt")):
            content += '<form method="post" action="{}/cancel?{}"><button class="secondary">取消任务</button></form>'.format(
                self.prefix, urlencode({"id": identity}))
        if "code" in job:
            content += "<p>退出码：{}</p>".format(_e(job["code"]))
        metrics = job.get("metrics", {})
        if metrics:
            names = {"before_bytes": "处理前", "after_bytes": "处理后", "saved_bytes": "体积差（节省）",
                     "uncompressed_archive_bytes": "解压后镜像大小", "upload_limit": "上传限制（字节）",
                     "removed_paths": "删除路径数", "protected_paths": "保护候选数", "removed_unique_bytes": "回收载荷",
                     "archive_bytes": "归档大小", "hidden_file_bytes": "被覆盖文件字节",
                     "duplicate_groups": "重复内容组", "large_layers": "大层编号",
                     "deleted": "已删除文件数", "bytes": "已删除文件字节"}
            content += _pairs((names[key], "{:,} B".format(value) if key.endswith("bytes") else value)
                              for key, value in metrics.items() if key in names)
        if job.get("form") and not running:
            locked = state == "success" and job["action"] == "lock-inputs" and job.get("artifacts")
            content += '<p><a class="button compact" href="{}/reuse?{}">{}</a>　<small>{}</small></p>'.format(
                self.prefix, urlencode({"id": identity}), "使用此输入锁构建" if locked else "复用参数",
                "填写输出 tar 路径，构建时会核对锁定的输入。" if locked else "提交前可修改版本或输出。")
        artifacts = job.get("artifacts", [])
        for index, item in enumerate(artifacts):
            if artifact_state(item, suffixes=task_artifact_suffixes(job["action"]))[0] == "available":
                content += '<p><a class="button compact" href="{}/download?{}">校验并下载</a>　<code>{}</code></p>'.format(
                    self.prefix, _e(urlencode({"id": identity, "file": index})), _e(item["path"]))
            else:
                content += '<p class="muted">产物已移走或变更 · <code>{}</code></p>'.format(_e(item["path"]))
        if (state == "success" and job["action"] == "multiarch" and len(artifacts) == 1
                and artifact_state(artifacts[0])[0] == "available"):
            content += '<p><a class="button compact" href="{}/delivery?{}">校验并推送多架构镜像</a></p>'.format(
                self.prefix, _e(urlencode({"scope": "job", "id": identity})))
        if state == "success" and job["action"] == "build" and any(
                item["path"].lower().endswith(".dsse.json") for item in artifacts):
            content += '<p><a class="button compact" href="{}/attest?{}">校验交付签名</a></p>'.format(
                self.prefix, _e(urlencode({"job": identity})))
        if job.get("archived"):
            content += "<p>历史任务保存结果和产物记录，原始日志保留在运行任务的会话中。</p>"
        return content

    def job_page(self, job):
        running = job["state"] in ("queued", "running")
        summary = ('<div id="job-details">{}</div><div class="job-stage">'
                   '<small>任务状态</small><p id="job-status">{}</p></div>').format(
                        self.job_details(job), _e(job["status"] or STATES[job["state"]]))
        content = _section(LABELS[job["action"]], summary)
        content += '<div id="job-flow">{}</div>'.format(pull_flow(job))
        if not job.get("archived"):
            query = urlencode({"id": job["id"]})
            lines = log_lines(job)
            omitted = job["log_end"] - len(job["logs"])
            content += ('<article class="log-panel"><header class="log-toolbar"><div><h2>任务日志</h2>'
                        '<small id="job-follow" role="status"></small><div class="log-legend" aria-label="日志类型">'
                        '<span class="legend-stage">阶段</span><span class="legend-success">完成</span>'
                        '<span class="legend-cache">缓存</span><span class="legend-error">失败</span></div></div>'
                        '<div class="actions log-actions">'
                        '<button type="button" class="secondary" id="job-latest" hidden>跳到最新</button>'
                        '<a id="job-export"{5} class="button compact" href="{0}/job-log?{1}">导出当前日志</a></div></header>'
                        '<pre id="job-output" tabindex="0" aria-label="任务日志" data-url="{0}/job-progress?{1}" '
                        'data-after="{2}" data-running="{3}">{4}</pre>'
                        '<footer class="log-notes"><p class="muted" id="job-poll-status" role="status" hidden></p>'
                        '<p class="muted"{6}>前 <span id="job-log-omitted">{7}</span> 条日志已移出窗口。</p>'
                        '<small>分享日志前请检查命令输出。</small></footer>').format(
                            self.prefix, query, job["log_end"], int(running), lines, "" if lines else " hidden",
                            "" if omitted else " hidden", omitted)
            if running:
                content += '<noscript><p><a href="{}/job?{}">刷新任务状态和日志</a></p></noscript>'.format(self.prefix, query)
            content += "</article>"
        return content

    def job_progress(self, job, after):
        end = job.get("log_end", 0)
        start = end - len(job["logs"])
        return {"details": self.job_details(job), "status": job["status"] or STATES[job["state"]],
                "running": job["state"] in ("queued", "running"), "archived": bool(job.get("archived")),
                "flow": pull_flow(job), "start": start, "end": end,
                "lines": job["logs"][max(0, after - start):],
                "line_meta": log_metadata(job)[max(0, after - start):]}

    def _upload_token(self, data):
        token = data.get("package-upload", "")
        if token and data.get("package", "").strip():
            raise BuildError("请选择暂存文件或填写应用包路径")
        return token

    def submit(self, action, data):
        allowed = UPLOAD_FIELDS.get(action, {})
        requested = {}
        for key, token in data.items():
            if key.endswith("-upload") and token:
                field = key[:-7]
                if field not in allowed or data.get(field, "").strip():
                    raise BuildError("请选择文件或填写该输入的本机路径")
                requested[field] = (token, allowed[field])
        paths = self.uploads.claim(requested) if requested else {}
        tokens = [token for token, _ in requested.values()]
        try:
            return self._submit(action, data, paths, partial(self.uploads.release, tokens) if tokens else None)
        except BaseException:
            self.uploads.release(tokens, retry=True)
            raise

    def _submit(self, action, data, paths, cleanup):
        if action == "attest-verify":
            inputs = {}
            for name in ("public-key", "provenance", "envelope", "sbom", "docker-archive", "oci-archive"):
                value = path_value(data, name, required=not name.endswith("-archive"))
                if value:
                    path = _path(value)
                    if not path.is_file():
                        raise BuildError("验签输入不是现有文件：" + name)
                    inputs[name] = str(path)
            if "docker-archive" not in inputs and "oci-archive" not in inputs:
                raise BuildError("请提供签名覆盖的镜像归档")
            return self.submit_delivery({"action": action, "inputs": inputs})
        if action == "repo-test":
            from web_repository import diagnostic_plan
            plan = diagnostic_plan(self.settings, data.get("host", ""), data.get("reference", "").strip())
            return self.submit_delivery(dict(plan, action=action, label=plan["host"],
                                             config=str(self.config_arg) if self.config_arg is not None else None))
        effective = dict(data, **{field: str(path) for field, path in paths.items() if field != "package"})
        if action in ("build", "lock-inputs") and data.get("require-dockerfile") == "1":
            path_value(effective, "dockerfile", required=True)
        args, outputs = command_for(action, effective, self.config_arg, self.profiles, paths.get("package"))
        if action == "build" and data.get("input-lock-job"):
            path_value(effective, "input-lock", required=True)
            source = self.get_job(data["input-lock-job"])
            if (source is None or source["action"] != "lock-inputs" or source["state"] != "success" or
                    len(source.get("artifacts", [])) != 1):
                raise BuildError("无法读取原任务登记的输入锁摘要")
            args.append("--input-lock-digest=" + source["artifacts"][0]["sha256"])
        if any(path.parent in output.parents for path in paths.values() for output in outputs):
            raise BuildError("输出路径不能位于文件暂存目录内")
        delivery = action in ("multiarch", "lock-inputs")
        if action == "multiarch":
            archives, expected = {}, {}
            for architecture in ("amd64", "arm64"):
                archive = Path(path_value(effective, architecture, required=True)).expanduser().absolute()
                archives[architecture] = str(archive)
                expected[architecture] = check_oci_platform(archive, architecture)
            tag = effective["tag"].strip()
            args = [json.dumps({"action": action, "archives": archives, "manifests": expected,
                                "tag": tag, "output": str(outputs[0]),
                                "epoch": effective.get("epoch", "").strip() or "0"}, ensure_ascii=True)]
        label = next((data[key] for key in ("project", "tag", "reference", "archive", "context") if data.get(key)), "")
        draft = form_snapshot(action, data)
        if action in ("build", "lock-inputs"):
            draft.setdefault("dockerfile", "")
        for field in paths:
            draft.pop(field, None)
        for key in ("package", "context", "dockerfile", "base-tar", "base-map", "input-lock", "output"):
            value = path_value(draft, key)
            if value:
                draft[key] = str(Path(value).expanduser().absolute())
        return self.jobs.submit(action, args, outputs, label[:300], draft, delivery=delivery, cleanup=cleanup)

    def close(self):
        try:
            self.uploads.close()
        finally:
            self.jobs.close()


def form_data(handler):
    """只接受有界的 URL 编码表单。"""
    if handler.headers.get_content_type() != "application/x-www-form-urlencoded":
        raise ValueError("Invalid form content type")
    lengths = handler.headers.get_all("Content-Length") or []
    if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdecimal() or len(lengths[0]) > 6:
        raise ValueError("Invalid form length")
    size = int(lengths[0])
    if size > 32768 or handler.headers.get("Transfer-Encoding") is not None:
        raise ValueError("Invalid form length")
    raw = getattr(handler, "_request_body", handler.rfile).read(size)
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
    """下载前核对任务登记的摘要，兼容当前会话和重启后的任务。"""
    if job is None or not 0 <= index < len(job.get("artifacts", [])):
        return handler._reply(404, "Artifact not found")
    item = job["artifacts"][index]
    _, stream = verified_artifact(item, suffixes=task_artifact_suffixes(job["action"]))
    if stream is None:
        return handler._reply(409, "Artifact was moved or changed")
    with stream:
        return send_artifact(handler, stream, Path(item["path"]).name, item["bytes"])


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
