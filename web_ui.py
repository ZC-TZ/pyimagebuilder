"""工作台共用页面框架和离线样式。"""

from html import escape


ICONS = {
    "logo": '<path d="m12 2 9 5v10l-9 5-9-5V7z"/><path d="m3 7 9 5 9-5M12 12v10m-5-17 9 5"/>',
    "build": '<path d="M14 3h6v6m0-6-8 8M10 5H5a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-5"/>',
    "jobs": '<rect x="3" y="5" width="18" height="15" rx="2"/><path d="M7 10l3 3-3 3m6 0h4"/>',
    "images": '<path d="m12 3 9 5-9 5-9-5zM3 12l9 5 9-5M3 16l9 5 9-5"/>',
    "history": '<rect x="5" y="3" width="14" height="18" rx="2"/><path d="M9 8h6m-6 4h6m-6 4h4"/>',
    "space": '<rect x="3" y="4" width="18" height="16" rx="3"/><path d="M3 14h18m-14 3h1m3 0h1"/>',
    "config": '<path d="M5 6h14M5 12h14M5 18h14"/><circle cx="9" cy="6" r="2"/><circle cx="15" cy="12" r="2"/><circle cx="10" cy="18" r="2"/>',
    "arrow": '<path d="M5 12h14m-6-6 6 6-6 6"/>',
}

HERO = """
<svg class="hero-art" viewBox="0 0 340 148" fill="none" aria-hidden="true">
  <defs>
    <linearGradient id="layer-fill" x2="1" y2="1" gradientUnits="objectBoundingBox">
      <stop stop-color="#d6e5ff"/><stop offset="1" stop-color="#f9fbff"/>
    </linearGradient>
  </defs>
  <path d="M17 118h298M39 135h254M39 24h254M17 41h298" stroke="#cbdcf4" stroke-dasharray="3 8"/>
  <path d="M68 13v127m204-127v127M43 76h255" stroke="#cbdcf4" stroke-dasharray="3 8"/>
  <path d="m82 101 89 38 89-38-89-38z" fill="#dce9fd" stroke="#85abe8" stroke-width="1.5"/>
  <path d="m82 78 89 38 89-38-89-38z" fill="#ebf3ff" stroke="#6e9be6" stroke-width="1.5"/>
  <path d="m82 54 89 38 89-38-89-38z" fill="url(#layer-fill)" stroke="#437adb" stroke-width="1.7"/>
  <path d="m135 49 36 15 36-15m-62-9 26-11 26 11" stroke="#437adb" stroke-width="1.7" stroke-linejoin="round" stroke-linecap="round"/>
  <circle cx="68" cy="43" r="4" fill="#497fe0"/><circle cx="274" cy="104" r="4" fill="#5c87df"/>
  <circle cx="307" cy="67" r="2.5" fill="#86a9e8"/><circle cx="48" cy="96" r="2.5" fill="#86a9e8"/>
</svg>
"""

STYLE = """
:root{
  color-scheme:light;--bg:#f4f7fb;--surface:#fff;--ink:#17263d;--muted:#53637b;
  --line:#dce4f0;--accent:#215bcc;--accent-hover:#194aa9;--tint:#edf4ff;
  --nav:#101c31;--nav-muted:#b6c5dc;--radius:15px;
  --mono:ui-monospace,Consolas,"Liberation Mono",monospace;
  font-family:system-ui,"Microsoft YaHei","Noto Sans CJK SC",sans-serif
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-size:14px;line-height:1.6}
a{color:var(--accent);text-underline-offset:3px}
a:hover{color:var(--accent-hover)}
:focus-visible{outline:3px solid #4b82ed;outline-offset:3px}
.skip{position:absolute;z-index:3;left:12px;top:12px;transform:translateY(-160%);background:#fff;padding:8px 12px;border-radius:8px}
.skip:focus{transform:none}
.shell{display:grid;grid-template-columns:224px minmax(0,1fr);min-height:100vh}
.sidebar{position:sticky;top:0;height:100vh;padding:24px 15px 18px;background:var(--nav);color:#fff;display:flex;flex-direction:column;gap:28px;overflow:auto}
.brand{display:flex;align-items:center;gap:10px;padding:0 7px;text-decoration:none;color:#fff}
.brand:hover{color:#fff}.brand-text{line-height:1.3;font-weight:750;letter-spacing:-.025em;font-size:15px}
.brand-sub{display:block;color:var(--nav-muted);font-weight:500;font-size:11px;letter-spacing:0;margin-top:4px}
.brand-icon{display:grid;place-items:center;flex:0 0 34px;height:34px;background:#235bc9;border-radius:9px}
.icon{width:18px;height:18px;flex:none;vertical-align:middle}
.tabs{display:grid;gap:5px}.tabs a{display:flex;align-items:center;gap:10px;border-radius:9px;padding:10px 11px;color:var(--nav-muted);text-decoration:none;font-weight:600}
.tabs a:hover{background:#21334e;color:#fff}.tabs a[aria-current=page]{background:#244a88;color:#fff;box-shadow:inset 3px 0 #85afff}
.sidebar-foot{margin-top:auto;padding:10px 9px;color:var(--nav-muted);font-size:12px;border-top:1px solid #31405a}
.dot{display:inline-block;width:7px;height:7px;background:#3da280;border-radius:50%;margin-right:7px;vertical-align:1px}
main{width:100%;max-width:1320px;min-width:0;margin:0 auto;padding:28px clamp(20px,3.6vw,52px) 54px}
.standalone{max-width:1080px}
.page-head{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;margin:0 0 22px;padding-bottom:19px;border-bottom:1px solid var(--line)}
.eyebrow{display:block;font-size:11px;letter-spacing:.12em;font-weight:750;text-transform:uppercase;color:var(--muted);margin-bottom:4px}
h1{font-size:clamp(24px,3vw,34px);letter-spacing:-.04em;line-height:1.25;margin:0}
.lede{color:var(--muted);margin:6px 0 0}.local{display:inline-flex;align-items:center;white-space:nowrap;margin-top:12px;padding:4px 10px;border:1px solid var(--line);border-radius:20px;background:var(--surface);color:var(--muted);font-size:12px}
p{margin:0 0 8px}.muted,small{color:var(--muted)}code{font:13px/1.55 var(--mono);overflow-wrap:anywhere}
.records{display:grid;gap:15px;min-width:0}
article,.panel,.empty{min-width:0;background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);padding:20px 22px;box-shadow:0 2px 8px #192c4b06}
article h2,.panel h2{font-size:17px;line-height:1.4;margin:0 0 9px;overflow-wrap:anywhere}
article p:last-child,.panel p:last-child{margin-bottom:0}
article[id]{scroll-margin-top:18px}.panel-title{display:flex;align-items:center;gap:9px}.panel-title .icon{color:var(--accent)}
.section-copy{color:var(--muted);margin-bottom:14px}
.primary-panel{border-color:#a9c5f7;border-top:3px solid #356fe2}
.hero{position:relative;isolation:isolate;overflow:hidden;display:grid;grid-template-columns:minmax(0,1fr) 34%;gap:12px;
  padding:23px 24px;border:1px solid #ccdcf5;border-radius:var(--radius);background:linear-gradient(112deg,#f6faff 4%,#e8f0ff 100%)}
.hero h2{font-size:23px;line-height:1.3;letter-spacing:-.035em;margin:0 0 6px}
.hero p{color:#405677}.hero-art{width:100%;height:100%;max-height:152px;align-self:center}
.shortcuts{display:flex;flex-wrap:wrap;gap:8px;margin-top:14px}
form{display:flex;flex-wrap:wrap;gap:13px;align-items:end;margin:0 0 14px}
form:last-child{margin-bottom:0}
label{display:grid;gap:5px;min-width:0;flex:1 1 180px;font-weight:650;color:#273a55}
input,select,textarea,button,.button{border:1px solid #c7d4e7;border-radius:8px;font:inherit;padding:8px 11px;min-height:38px}
input,select,textarea{width:100%;min-width:0;color:var(--ink);background:#fbfcff}
input::placeholder,textarea::placeholder{color:#66768e;font-weight:450}
input:focus,select:focus,textarea:focus{border-color:#4c83e6}
button,.button{display:inline-flex;justify-content:center;align-items:center;gap:6px;text-decoration:none;font-weight:700;cursor:pointer;max-width:100%}
button,.button.primary{color:white;background:var(--accent);border-color:var(--accent)}
button:hover,.button.primary:hover{background:var(--accent-hover);color:#fff}
.button{background:#fff;color:var(--accent)}
.button:hover{background:var(--tint);border-color:#b2caf0}
.button.compact{padding:5px 9px;min-height:32px;font-size:12px}
button.secondary{color:var(--accent);background:#fff;border-color:#c7d4e7}
button.secondary:hover{background:var(--tint)}
button.danger{color:#97263c;background:#fff4f6;border-color:#edc3cb}
button.danger:hover{background:#fce6eb}
.job-form{padding-top:8px;margin-bottom:0}
.job-form[data-action=fast]{display:grid;grid-template-columns:repeat(12,minmax(0,1fr))}
.job-form[data-action=fast]>label{grid-column:span 4}
.job-form[data-action=fast]>[data-field=package],.job-form[data-action=fast]>.preview,
.job-form[data-action=fast]>.form-footer{grid-column:1 / -1}
.job-form[data-action=fast]>[data-field=platform],.job-form[data-action=fast]>[data-field=format]{grid-column:span 3}
.job-form[data-action=fast]>[data-field=output]{grid-column:span 6}
.form-footer{flex:1 0 100%;padding-top:12px;margin-top:3px;border-top:1px solid #edf1f7;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.field-wide{flex-basis:100%}
.advanced,.toolbox{flex:1 0 100%;min-width:0;border:1px solid var(--line);border-radius:10px;background:#f8faff}
.advanced{padding:9px 12px}.advanced label{margin-top:8px}
summary{cursor:pointer;font-weight:700;color:#294669}.toolbox>summary{padding:14px 17px}.toolbox[open]>summary{border-bottom:1px solid var(--line)}
.toolbox-body{display:grid;gap:10px;padding:12px}.toolbox-body article{box-shadow:none}
.preview{flex:1 0 100%;white-space:pre-wrap;overflow-wrap:anywhere;margin:0;padding:10px 12px;border-radius:9px;border:1px solid #d9e6fa;background:#f2f7ff;color:#35517a}
.preview[data-tone=alert]{background:#fff5ed;border-color:#f0ceb0;color:#7d451c}
.check{display:flex;align-items:center;gap:7px;min-height:36px;flex:0 1 auto;padding:0 4px;font-weight:500}
.check input{width:16px;height:16px;min-height:0;accent-color:var(--accent)}
dl{display:grid;grid-template-columns:max-content minmax(0,1fr);gap:5px 14px;margin:8px 0 12px}
dt{color:var(--muted)}dd{margin:0;min-width:0}
ul{padding:0;margin:10px 0 0;list-style:none}
li+li{margin-top:6px}
.badge,.state{display:inline-flex;align-items:center;gap:5px;padding:2px 8px;border-radius:7px;background:#eaf2ff;color:#244a84;
  font-size:12px;font-weight:750;white-space:nowrap}
.state[data-state=success]{background:#e5f5ee;color:#17684a}
.state[data-state=failed],.state[data-state=changed],.state[data-state=unsafe]{background:#fcecef;color:#912f43}
.state[data-state=running],.state[data-state=available]{background:#e9f1ff;color:#2456a5}
.state[data-state=queued],.state[data-state=unverified]{background:#fff2de;color:#795018}
.state[data-state=cancelled],.state[data-state=missing],.state[data-state=unknown],.state[data-state=unreadable]{background:#edf0f5;color:#4d5d73}
.meta{display:flex;gap:4px 13px;flex-wrap:wrap;font-size:13px;overflow-wrap:anywhere}
.controls,.actions{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.controls{margin-top:16px}.controls .muted{margin-right:auto}
.artifact{display:grid;gap:7px;padding:11px 12px;border:1px solid #e8edf6;border-radius:9px;background:#f9fbff}
.artifact-line{display:flex;align-items:baseline;gap:8px;flex-wrap:wrap;min-width:0}
.history-filter{margin-bottom:15px}.history-filter form{margin:0}
.job-summary{display:flex;align-items:center;flex-wrap:wrap;gap:8px;margin:0 0 10px}.job-list{display:grid;gap:8px}
.job-row{display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;padding:10px 0;border-bottom:1px solid #edf1f7}
.job-row:last-child{border-bottom:0}.job-row strong{margin-right:7px}
pre{font:12px/1.6 var(--mono);white-space:pre-wrap;overflow-wrap:anywhere;background:#152238;color:#e2ecfb;padding:12px 14px;border-radius:9px;
  max-height:460px;overflow:auto;margin:9px 0}
#job-status{min-height:1.6em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#job-output{overflow-anchor:none}
#job-output>span{display:block;min-height:1.6em}
#job-output:empty::before{content:"此任务暂无日志。"}
#job-output[data-running="1"]:empty::before{content:"等待输出…"}
#job-latest[hidden]{display:none}
.tiles{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}
.operation-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
.tile-value{font-size:24px;letter-spacing:-.035em;font-weight:750}.tile-path{display:block;margin-top:6px}
.empty{color:var(--muted);padding:23px;text-align:center}
@media(max-width:900px){
  .shell{grid-template-columns:178px minmax(0,1fr)}.sidebar{padding-inline:10px}
  .hero{grid-template-columns:minmax(0,1fr) 28%}.tiles{grid-template-columns:repeat(auto-fit,minmax(150px,1fr))}
}
@media(max-width:680px){
  .shell{display:block}.sidebar{position:static;height:auto;padding:12px 14px;gap:10px;overflow:visible}
  .brand{padding:0}.brand-sub{display:none}.brand-icon{height:30px;flex-basis:30px}.sidebar-foot{display:none}
  .tabs{grid-template-columns:repeat(3,minmax(0,1fr));gap:4px}.tabs a{gap:5px;padding:6px 7px;font-size:13px}.tabs .icon{width:15px;height:15px}
  main{padding:17px 13px 34px}.page-head{margin-bottom:14px;padding-bottom:13px;gap:6px}.local{margin-top:0}
  article,.panel,.empty,.hero{padding:14px}.hero{grid-template-columns:minmax(0,1fr)}.hero-art{display:none}.hero h2{font-size:20px}
  form{gap:10px}.records{gap:11px}
  .operation-grid{grid-template-columns:minmax(0,1fr)}
  .job-form[data-action=fast]{grid-template-columns:repeat(2,minmax(0,1fr))}
  .job-form[data-action=fast]>label,.job-form[data-action=fast]>[data-field=platform],
  .job-form[data-action=fast]>[data-field=format]{grid-column:span 1}
  .job-form[data-action=fast]>[data-field=output],.job-form[data-action=fast]>[data-field=profile]{grid-column:1 / -1}
}
@media(max-width:390px){.local{display:none}.tabs a{font-size:12px}.tiles{grid-template-columns:1fr}}
"""


def icon(name, size=18):
    return ('<svg class="icon" width="{0}" height="{0}" viewBox="0 0 24 24" fill="none" '
            'stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" '
            'aria-hidden="true">{1}</svg>').format(size, ICONS[name])


def document(title, body, sidebar="", description="", extra_head=""):
    """台账和工作台共享排版；标题与说明在这里统一转义。"""
    label = escape(title)
    header = ('<header class="page-head"><div><span class="eyebrow">PyImageBuilder</span><h1>{}</h1>{}</div>{}</header>').format(
        label, '<p class="lede">{}</p>'.format(escape(description)) if description else "",
        '<span class="local"><span class="dot"></span>本机工作台</span>' if sidebar else "")
    main = '<main id="content" tabindex="-1"{}>{}{}</main>'.format(' class="standalone"' if not sidebar else "", header, body)
    content = '<div class="shell">{}{}</div>'.format(sidebar, main) if sidebar else main
    return ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1"><title>{} · PyImageBuilder</title>'
            '<style>{}</style>{}</head><body><a class="skip" href="#content">跳到主要内容</a>{}</body></html>').format(
                label, STYLE, extra_head, content)
