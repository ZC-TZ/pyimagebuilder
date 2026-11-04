"""本机打包工作台的 HTTP 入口，复用台账查询与 CLI。"""

import socket
import sys
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode

from build_records import list_builds
from errors import BuildError


PAGE_SIZE = 25
STYLE = """
:root{color-scheme:light;--ink:#182c35;--muted:#536873;--line:#d7e4e8;--accent:#0b695c}
*{box-sizing:border-box}
body{margin:0;background:#f2f7f8;color:var(--ink);font:16px/1.55 system-ui,sans-serif}
main{max-width:1120px;margin:auto;padding:40px 24px 56px}
header{border-bottom:1px solid var(--line);padding-bottom:22px;margin-bottom:24px}
h1{font-size:clamp(28px,5vw,44px);line-height:1.15;margin:0 0 12px}
p{margin:0 0 8px}
.muted,small{color:var(--muted)}
code{font:inherit;overflow-wrap:anywhere}
form{display:flex;flex-wrap:wrap;gap:12px;align-items:end;margin-bottom:24px}
label{display:grid;gap:4px;flex:1 1 210px;font-weight:600}
input,select,button,.button{border:1px solid var(--line);border-radius:8px;font:inherit;padding:9px 12px}
input,select{width:100%;color:var(--ink);background:#fff}
button{color:#fff;background:var(--accent);border-color:var(--accent);cursor:pointer}
a{color:var(--accent)}
.button{display:inline-block;background:#fff;text-decoration:none;white-space:nowrap}
:focus-visible{outline:3px solid #318478;outline-offset:2px}
.records{display:grid;gap:12px}
article,.empty{background:#fff;border:1px solid var(--line);border-radius:12px;padding:16px 20px}
article h2{font-size:18px;margin:0 0 6px;overflow-wrap:anywhere}
ul{padding:0;margin:10px 0 0;list-style:none}
li+li{margin-top:5px}
.badge{display:inline-block;background:#e9f4f2;color:#0a574e;padding:1px 7px;border-radius:6px;
font-size:12px;font-weight:700;margin-right:6px}
.meta{display:flex;gap:4px 16px;flex-wrap:wrap;font-size:14px}
.controls{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:18px}
.controls .muted{margin-right:auto}
.tabs{display:flex;gap:8px 18px;flex-wrap:wrap;margin:0 0 22px;font-weight:700}
.job-form{padding-top:10px;margin-bottom:0}
.check{display:flex;align-items:center;gap:8px}
.check input{width:auto}
dl{display:grid;grid-template-columns:max-content minmax(0,1fr);gap:4px 14px;margin:8px 0}
dd{margin:0}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#edf4f5;padding:12px;border-radius:8px}
@media(max-width:550px){main{padding:26px 14px}article,.empty{padding:12px}form{gap:8px}}
"""


def _url(project="", tag="", page=1, root="/"):
    params = {}
    if project:
        params["project"] = project
    if tag:
        params["tag"] = tag
    if page > 1:
        params["page"] = page
    return root + "?" + urlencode(params) if params else root


def _artifact(item):
    kind = item["format"].upper()
    path = escape(item["path"])
    count = item.get("bytes")
    size = " <small>({:,} B)</small>".format(count) if type(count) is int and count >= 0 else ""
    return '<li><span class="badge">{}</span><code>{}</code>{}</li>'.format(kind, path, size)


def _card(row):
    fields = [row["completedAt"], row["platform"]]
    if row["mode"] == "fast":
        fields += ["Fast"] + [row[key] for key in ("project", "version", "profile") if isinstance(row.get(key), str)]
    else:
        fields += ["普通构建"]
    metadata = "".join("<span>{}</span>".format(escape(value)) for value in fields)
    artifacts = "".join(_artifact(item) for item in row["artifacts"])
    return '<article><h2>{}</h2><div class="meta muted">{}</div><ul>{}</ul></article>'.format(
        escape(row["tag"]), metadata, artifacts)


def _page(directory, project, tag, page, rows, more, error=False, workspace=None):
    root = workspace.prefix + "/history" if workspace is not None else "/"
    cards = "".join(_card(row) for row in rows)
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
    return """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>打包台账 · PyImageBuilder</title><style>{}</style></head>
<body><main><header><h1>打包台账</h1>
<p class="muted">按完成时间倒序排列（UTC）。路径为打包时的原始位置。</p>
<p class="muted"><code>{}</code></p></header>{}
<form action="{}" method="get">
<label>项目<input name="project" value="{}" placeholder="例如 hisq"></label>
<label>镜像标签<input name="tag" value="{}" placeholder="例如 hisq_front:1.0.0"></label>
<button type="submit">筛选</button><a class="button" href="{}">清除筛选</a>
</form><section class="records" aria-label="构建记录">{}</section>
<nav class="controls" aria-label="分页">{}</nav></main></body></html>""".format(
        STYLE, escape(str(directory)), workspace.nav() if workspace is not None else "", root,
        escape(project), escape(tag), root, cards, "".join(controls))


def _workbench_page(workspace, title, body, refresh=False):
    return """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">{}<title>{} · PyImageBuilder</title>
<style>{}</style></head><body><main><header><h1>镜像构建工作台</h1>
<p class="muted">{}</p></header>{}<section class="records">{}</section></main></body></html>""".format(
        '<meta http-equiv="refresh" content="2">' if refresh else "", escape(title),
        STYLE, escape(title), workspace.nav(), body)


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

        def _reply(self, status, body, html=False):
            data = body.encode("utf-8", errors="xmlcharrefreplace")
            self.send_response(status)
            self.send_header("Content-Type", ("text/html" if html else "text/plain") + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "same-origin")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; "
                             "form-action 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(data)

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
            from web_workbench import download
            views = {"/workbench": ("构建与打包", workspace.builds), "/jobs": ("本次会话的任务", workspace.tasks),
                     "/images": ("本地镜像库", workspace.images), "/config": ("配置概览", workspace.configuration)}
            if route == "/download" and len(query) <= 200:
                try:
                    params = parse_qs(query)
                    identity, = params.get("id", [""])
                    index, = params.get("file", ["-1"])
                    index = int(index)
                except ValueError:
                    return self._reply(400, "Invalid artifact selector")
                return download(self, workspace.jobs.snapshot(identity), index)
            try:
                if route in views:
                    title, view = views[route]
                    return self._reply(200, _workbench_page(workspace, title, view()), html=True)
                if route == "/job" and len(query) <= 200:
                    params = parse_qs(query)
                    identity, = params.get("id", [""])
                    job = workspace.jobs.snapshot(identity)
                    if job is None:
                        return self._reply(404, "Job not found")
                    body, refresh = workspace.job_page(job)
                    return self._reply(200, _workbench_page(workspace, "任务详情", body, refresh), html=True)
            except (BuildError, OSError, ValueError) as exc:
                return self._reply(400, _workbench_page(workspace, "无法完成请求", '<p class="empty">{}</p>'.format(
                    escape(str(exc))), False), html=True)
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
            try:
                if route is not None and route.startswith("/submit/") and not query:
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
