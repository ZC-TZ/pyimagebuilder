"""台账的只读本机网页，复用 CLI 的记录读取逻辑。"""

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
input,button,.button{border:1px solid var(--line);border-radius:8px;font:inherit;padding:9px 12px}
input{width:100%;color:var(--ink);background:#fff}
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
@media(max-width:550px){main{padding:26px 14px}article,.empty{padding:12px}form{gap:8px}}
"""


def _url(project="", tag="", page=1):
    params = {}
    if project:
        params["project"] = project
    if tag:
        params["tag"] = tag
    if page > 1:
        params["page"] = page
    return "/?" + urlencode(params) if params else "/"


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


def _page(directory, project, tag, page, rows, more, error=False):
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
    controls.extend('<a class="button" href="{}">{}</a>'.format(escape(_url(project, tag, number)), label)
                    for label, number in links)
    return """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>打包台账 · PyImageBuilder</title><style>{}</style></head>
<body><main><header><h1>打包台账</h1>
<p class="muted">按完成时间倒序排列（UTC）。路径为打包时的原始位置。</p>
<p class="muted"><code>{}</code></p></header>
<form action="/" method="get">
<label>项目<input name="project" value="{}" placeholder="例如 hisq"></label>
<label>镜像标签<input name="tag" value="{}" placeholder="例如 hisq_front:1.0.0"></label>
<button type="submit">筛选</button><a class="button" href="/">清除筛选</a>
</form><section class="records" aria-label="构建记录">{}</section>
<nav class="controls" aria-label="分页">{}</nav></main></body></html>""".format(
        STYLE, escape(str(directory)), escape(project), escape(tag), cards, "".join(controls))


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


def make_server(directory, port=8765, limit=None):
    """只开放台账页面；固定绑定回环地址，并拒绝外站 Host。"""

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
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; "
                             "form-action 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            port = self.server.server_port
            allowed = {"127.0.0.1:{}".format(port), "localhost:{}".format(port)}
            if port == 80:
                allowed.update(("127.0.0.1", "localhost"))
            hosts = self.headers.get_all("Host") or []
            if len(hosts) != 1 or hosts[0].lower() not in allowed:
                return self._reply(403, "Invalid Host")
            route, _, query = self.path.partition("?")
            if route != "/":
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
                return self._reply(500, _page(directory, project, tag, page, [], False, error=True), html=True)
            return self._reply(200, _page(directory, project, tag, page, rows[end - PAGE_SIZE:end],
                                          len(rows) > end), html=True)

    return ThreadingHTTPServer(("127.0.0.1", port), LedgerHandler)


def serve(directory, port=8765, project=None, tag=None, limit=None):
    """前台运行，终端退出时关闭监听；不依赖桌面浏览器存在。"""
    try:
        server = make_server(directory, port, limit)
    except OSError as exc:
        raise BuildError("Cannot start web view on 127.0.0.1:{}: {} (try --port 0)".format(port, exc)) from exc
    with server:
        print("Open in your browser: http://127.0.0.1:{}{}".format(
            server.server_port, _url(project, tag)), flush=True)
        print("Press Ctrl+C to stop.", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0
