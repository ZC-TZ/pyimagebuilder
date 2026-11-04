"""本机打包工作台的 HTTP 入口，复用台账查询与 CLI。"""

import socket
import sys
import json
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode

from build_records import get_build, list_builds
from errors import BuildError
from web_ui import document


PAGE_SIZE = 25


def _url(project="", tag="", page=1, root="/"):
    params = {}
    if project:
        params["project"] = project
    if tag:
        params["tag"] = tag
    if page > 1:
        params["page"] = page
    return root + "?" + urlencode(params) if params else root


ARTIFACT_STATES = {"available": "文件在，下载时校验摘要", "missing": "文件已移走或删除",
                   "unverified": "文件在，无可用摘要", "changed": "文件大小已变更",
                   "unsafe": "非普通归档", "unreadable": "无法读取"}


def _artifact(item, workspace=None, identity=None, index=None):
    kind = item["format"].upper()
    path = escape(item["path"])
    count = item.get("bytes")
    size = " <small>({:,} B)</small>".format(count) if type(count) is int and count >= 0 else ""
    actions, state_html = "", ""
    if workspace is not None:
        from web_artifacts import artifact_state
        state, _ = artifact_state(item)
        state_html = '<span class="state" data-state="{}">{}</span>'.format(state, ARTIFACT_STATES[state])
        if state == "available":
            query = urlencode({"id": identity, "file": index})
            actions = '<div class="actions"><a class="button compact" href="{}/history-download?{}">校验并下载</a>{}</div>'.format(
                workspace.prefix, escape(query), workspace.delivery_link("history", identity, index))
    return ('<li class="artifact"><div class="artifact-line"><span class="badge">{}</span><code>{}</code>{}{}</div>'
            '{}</li>').format(kind, path, size, state_html, actions)


def _card(row, workspace=None):
    fields = [row["completedAt"], row["platform"]]
    if row["mode"] == "fast":
        fields += ["Fast"] + [row[key] for key in ("project", "version", "profile") if isinstance(row.get(key), str)]
    else:
        fields += ["普通构建"]
    metadata = "".join("<span>{}</span>".format(escape(value)) for value in fields)
    artifacts = "".join(_artifact(item, workspace, row.get("id"), index)
                        for index, item in enumerate(row["artifacts"]))
    return '<article><h2>{}</h2><div class="meta muted">{}</div><ul>{}</ul></article>'.format(
        escape(row["tag"]), metadata, artifacts)


def _page(directory, project, tag, page, rows, more, error=False, workspace=None):
    root = workspace.prefix + "/history" if workspace is not None else "/"
    cards = "".join(_card(row, workspace) for row in rows)
    if not cards:
        message = "暂时无法读取台账，请查看启动终端。" if error else "没有符合条件的记录。"
        cards = '<p class="empty">{}</p>'.format(message)
    controls = ['<span class="muted">第 {} 页 · 本页 {} 条</span>'.format(page, len(rows))]
    links = [("刷新", page)]
    if page > 1:
        links.append(("上一页", page - 1))
    if more:
        links.append(("下一页", page + 1))
    controls.extend('<a class="button" href="{}">{}</a>'.format(escape(_url(project, tag, number, root)), label)
                    for label, number in links)
    body = """<div class="panel history-filter"><form action="{}" method="get">
<label>项目<input name="project" value="{}" placeholder="例如 hisq"></label>
<label>镜像标签<input name="tag" value="{}" placeholder="例如 hisq_front:1.0.0"></label>
<button type="submit">筛选</button><a class="button" href="{}">清除筛选</a>
</form><p class="muted"><code>{}</code></p></div>
<section class="records" aria-label="构建记录">{}</section>
<nav class="controls" aria-label="分页">{}</nav>""".format(
        root, escape(project), escape(tag), root, escape(str(directory)), cards, "".join(controls))
    return document("打包台账", body, workspace.nav("history") if workspace is not None else "",
                    "按完成时间倒序排列（UTC）。归档路径为打包时的原始位置。")


PAGES = {
    "构建与打包": ("build", "从应用包或 Dockerfile 构建镜像，生成可交付的归档。"),
    "复用构建参数": ("build", "核对项目、版本和输出路径后，可再次提交。"),
    "最近任务": ("jobs", "查看进度、历史结果和可下载的产物。"),
    "任务详情": ("jobs", "运行期间自动更新任务状态和日志。"),
    "本地镜像库": ("images", "按标签、平台或来源查找镜像并执行产物操作。"),
    "产物操作": ("", "基于选中的归档或镜像执行校验、分析与交付。"),
    "推送确认": ("", ""),
    "空间与清理预览": ("space", "查看文件占用，核对清理候选项后再执行。"),
    "配置概览": ("config", "管理 Fast 模板，查看仓库类型和本机运行路径。"),
    "Fast 模板管理": ("config", "编辑模板后可先测试基础镜像，再预览并保存。"),
    "配置变更预览": ("config", ""),
}


def _workbench_page(workspace, title, body, fast_script=False, active=None, job_script=False):
    default_active, description = PAGES.get(title, ("", ""))
    script = "fast.js" if fast_script else "job.js" if job_script else None
    head = '<script defer src="{}/{}"></script>'.format(workspace.prefix, script) if script else ""
    return document(title, '<section class="records">{}</section>'.format(body),
                    workspace.nav(default_active if active is None else active), description, head)


def _query(value):
    if len(value) > 2048:
        raise ValueError("Query is too long")
    fields = parse_qs(value, keep_blank_values=True)
    if set(fields) - {"project", "tag", "page"} or any(len(values) != 1 for values in fields.values()):
        raise ValueError("Invalid query parameters")
    page = fields.get("page", ["1"])[0]
    if not page.isascii() or not page.isdecimal() or len(page) > 6 or int(page) < 1:
        raise ValueError("Invalid page number")
    return fields.get("project", [""])[0], fields.get("tag", [""])[0], int(page)


def make_server(directory, port=8765, limit=None, workspace=None):
    """固定绑定回环地址；写操作还要求会话路径和同源表单。"""

    class LedgerHandler(BaseHTTPRequestHandler):
        def setup(self):
            self.request.settimeout(5)
            super().setup()

        def handle(self):
            try:
                super().handle()
            except (ConnectionError, socket.timeout):
                pass  # 浏览器可取消已预开的连接，也可在读取页面时断开。

        def log_message(self, _format, *_args):
            pass  # 不记录每次筛选的项目名或镜像标签。

        def _reply(self, status, body, html=False, content_type=None):
            data = body.encode("utf-8", errors="xmlcharrefreplace")
            self.send_response(status)
            self.send_header("Content-Type", (content_type or ("text/html" if html else "text/plain")) + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "same-origin")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; style-src 'unsafe-inline'; script-src 'self'; connect-src 'self'; base-uri 'none'; "
                             "form-action 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(data)

        def _json(self, status, value):
            return self._reply(status, json.dumps(value, ensure_ascii=True), content_type="application/json")

        def _host(self):
            port = self.server.server_port
            allowed = {"127.0.0.1:{}".format(port), "localhost:{}".format(port)}
            if port == 80:
                allowed.update(("127.0.0.1", "localhost"))
            hosts = self.headers.get_all("Host") or []
            return hosts[0].lower() if len(hosts) == 1 and hosts[0].lower() in allowed else None

        def _redirect(self, path):
            self.send_response(303)
            self.send_header("Location", path)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "same-origin")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            if not self._host():
                return self._reply(403, "Invalid Host")
            route, _, query = self.path.partition("?")
            if workspace is not None:
                from web_workbench import session_route
                route = session_route(route, workspace.prefix)
                if route is None:
                    return self._reply(404, "Open the session URL printed in the terminal")
                if route != "/history":
                    return self._get_workbench(route, query)
            elif route != "/":
                return self._reply(404, "Not found")
            try:
                project, tag, page = _query(query)
            except ValueError:
                return self._reply(400, "Invalid query parameters")
            end = page * PAGE_SIZE
            try:
                rows = list_builds(directory, project or None, tag or None,
                                   end + 1 if limit is None else min(limit, end + 1))
            except (BuildError, OSError, ValueError) as exc:
                print("Cannot read build records: {}".format(exc), file=sys.stderr)
                return self._reply(500, _page(directory, project, tag, page, [], False,
                                              error=True, workspace=workspace), html=True)
            return self._reply(200, _page(directory, project, tag, page, rows[end - PAGE_SIZE:end],
                                          len(rows) > end, workspace=workspace), html=True)

        def _get_workbench(self, route, query):
            from web_workbench import FAST_SCRIPT, JOB_SCRIPT, SPACE_KINDS, download, send_artifact
            views = {"/workbench": ("构建与打包", workspace.builds), "/jobs": ("最近任务", workspace.tasks),
                     "/config": ("配置概览", workspace.configuration)}
            if route == "/download" and len(query) <= 200:
                try:
                    params = parse_qs(query)
                    identity, = params.get("id", [""])
                    index, = params.get("file", ["-1"])
                    index = int(index)
                except ValueError:
                    return self._reply(400, "Invalid artifact selector")
                return download(self, workspace.jobs.snapshot(identity), index)
            if route == "/history-download" and len(query) <= 200:
                try:
                    params = parse_qs(query)
                    identity, = params.get("id", [""])
                    index, = params.get("file", ["-1"])
                    index = int(index)
                    row = get_build(directory, identity)
                except (OSError, ValueError):
                    return self._reply(400, "Invalid build record or artifact")
                if row is None or not 0 <= index < len(row["artifacts"]):
                    return self._reply(404, "Artifact not found")
                from web_artifacts import verified_artifact
                item = row["artifacts"][index]
                state, stream = verified_artifact(item)
                if stream is None:
                    return self._reply(409, ARTIFACT_STATES[state] + "，或文件内容已变更。")
                with stream:
                    return send_artifact(self, stream, Path(item["path"]).name, item["bytes"])
            try:
                if route == "/job-progress" and len(query) <= 100:
                    fields = parse_qs(query, keep_blank_values=True)
                    if set(fields) != {"id", "after"} or any(len(values) != 1 for values in fields.values()):
                        raise ValueError("Invalid job progress selector")
                    identity, after = fields["id"][0], fields["after"][0]
                    if not after.isascii() or not after.isdecimal() or len(after) > 16:
                        raise ValueError("Invalid job log cursor")
                    job = workspace.get_job(identity)
                    if job is None:
                        return self._reply(404, "Job not found")
                    return self._json(200, workspace.job_progress(job, int(after)))
                if route == "/job-log" and len(query) <= 100:
                    identity, = parse_qs(query).get("id", [""])
                    job = workspace.jobs.snapshot(identity)
                    if job is None or not job["logs"]:
                        return self._reply(404, "No live job log")
                    body = ("\n".join(job["logs"]) + "\n").encode("utf-8", errors="replace")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Disposition", 'attachment; filename="pyimagebuilder-job-{}.log"'.format(job["id"]))
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.end_headers()
                    return self.wfile.write(body)
                if route == "/fast.js" and not query:
                    return self._reply(200, FAST_SCRIPT, content_type="application/javascript")
                if route == "/job.js" and not query:
                    return self._reply(200, JOB_SCRIPT, content_type="application/javascript")
                if route == "/profile" and len(query) <= 256:
                    name, = parse_qs(query).get("name", [None])
                    return self._reply(200, _workbench_page(workspace, "Fast 模板管理",
                                       workspace.profile_page(name)), html=True)
                if route == "/images" and len(query) <= 512:
                    fields = parse_qs(query)
                    search, = fields.get("search", [""])
                    platform, = fields.get("platform", [""])
                    source, = fields.get("source", [""])
                    return self._reply(200, _workbench_page(workspace, "本地镜像库",
                                       workspace.images(search, platform, source)), html=True)
                if route == "/delivery" and len(query) <= 256:
                    fields = parse_qs(query, keep_blank_values=True)
                    if any(len(values) != 1 for values in fields.values()):
                        raise ValueError("Invalid delivery selector")
                    data = {key: values[0] for key, values in fields.items()}
                    return self._reply(200, _workbench_page(workspace, "产物操作",
                                       workspace.delivery_page(data),
                                       active="history" if data.get("scope") == "history" else "images"), html=True)
                if route == "/space" and len(query) <= 100:
                    fields = parse_qs(query)
                    kind, = fields.get("kind", [None])
                    days, = fields.get("days", ["30"])
                    if (kind is not None and kind not in SPACE_KINDS or not days.isascii() or
                            not days.isdecimal() or len(days) > 5 or int(days) > 36500):
                        raise ValueError("Invalid space preview options")
                    return self._reply(200, _workbench_page(workspace, "空间与清理预览",
                                       workspace.space(kind, int(days))), html=True)
                if route in views:
                    title, view = views[route]
                    return self._reply(200, _workbench_page(workspace, title, view(),
                                       fast_script=route == "/workbench"), html=True)
                if route in ("/job", "/reuse") and len(query) <= 200:
                    params = parse_qs(query)
                    identity, = params.get("id", [""])
                    job = workspace.get_job(identity)
                    if job is None:
                        return self._reply(404, "Job not found")
                    if route == "/reuse":
                        if not job.get("form"):
                            return self._reply(404, "No reusable form")
                        return self._reply(200, _workbench_page(workspace, "复用构建参数",
                                           workspace.builds(job["action"], job["form"]),
                                           fast_script=job["action"] == "fast"), html=True)
                    return self._reply(200, _workbench_page(workspace, "任务详情", workspace.job_page(job),
                                       job_script=not job.get("archived")), html=True)
            except (BuildError, OSError, ValueError) as exc:
                return self._reply(400, _workbench_page(workspace, "无法完成请求", '<p class="empty">{}</p>'.format(
                    escape(str(exc)))), html=True)
            return self._reply(404, "Not found")

        def do_POST(self):
            if workspace is None:
                return self._reply(501, "Read-only ledger")
            from web_workbench import form_data, session_route
            host = self._host()
            if host is None or self.headers.get_all("Origin") != ["http://" + host]:
                return self._reply(403, "Invalid request origin")
            path, _, query = self.path.partition("?")
            route = session_route(path, workspace.prefix)
            if route == "/preview-fast" and not query:
                try:
                    return self._json(200, workspace.preview_fast(form_data(self)))
                except (BuildError, OSError, ValueError) as exc:
                    return self._json(400, {"error": str(exc)})
            try:
                if route == "/profile/preview" and not query:
                    return self._reply(200, _workbench_page(workspace, "配置变更预览",
                                       workspace.profile_preview(form_data(self))), html=True)
                if route == "/profile/test" and not query:
                    identity = workspace.test_profile(form_data(self))
                elif route in ("/profile/confirm", "/cache/confirm") and not query:
                    fields = form_data(self)
                    if set(fields) != {"token"}:
                        raise ValueError("Invalid confirmation")
                    action = "profile-save" if route == "/profile/confirm" else "cache-clean"
                    plan = workspace.delivery.confirm(fields["token"], action)
                    if action == "profile-save":
                        workspace.save_profile(plan)
                        return self._redirect(workspace.prefix + "/config")
                    identity = workspace.submit_delivery(plan)
                elif route == "/delivery/prepare" and not query:
                    plan = workspace.delivery.plan(form_data(self))
                    if plan["action"] == "push":
                        return self._reply(200, _workbench_page(workspace, "推送确认",
                                           workspace.push_confirmation(plan),
                                           active="history" if "record" in plan else "images"), html=True)
                    identity = workspace.submit_delivery(plan)
                elif route == "/delivery/confirm" and not query:
                    fields = form_data(self)
                    if set(fields) != {"token"}:
                        raise ValueError("Invalid push confirmation")
                    identity = workspace.submit_delivery(workspace.delivery.confirm(fields["token"]))
                elif route is not None and route.startswith("/submit/") and not query:
                    identity = workspace.submit(route[len("/submit/"):], form_data(self))
                elif route == "/cancel" and len(query) <= 100:
                    identity, = parse_qs(query).get("id", [""])
                    workspace.jobs.cancel(identity)
                else:
                    return self._reply(404, "Not found")
            except (BuildError, OSError, ValueError) as exc:
                return self._reply(400, _workbench_page(workspace, "任务未提交", '<p class="empty">{}</p>'.format(
                    escape(str(exc)))), html=True)
            return self._redirect(workspace.prefix + "/job?" + urlencode({"id": identity}))

    return ThreadingHTTPServer(("127.0.0.1", port), LedgerHandler)


def serve(directory, port=8765, project=None, tag=None, limit=None, *, settings=None, config=None):
    """前台运行，终端退出时关闭监听；不依赖桌面浏览器存在。"""
    from web_workbench import Workbench
    if settings is None or config is None:
        from settings import load_settings
        settings, config = load_settings(config)
    workspace = Workbench(settings, config, directory)
    try:
        try:
            server = make_server(directory, port, limit, workspace)
        except OSError as exc:
            raise BuildError("Cannot start web view on 127.0.0.1:{}: {} (try --port 0)".format(port, exc)) from exc
        with server:
            path = (_url(project, tag, root=workspace.prefix + "/history") if project or tag
                    else workspace.prefix + "/workbench")
            print("Open in your browser: http://127.0.0.1:{}{}".format(server.server_port, path), flush=True)
            print("Press Ctrl+C to stop.", flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
    finally:
        workspace.close()
    return 0
