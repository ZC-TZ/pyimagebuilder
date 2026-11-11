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
    "package": '<path d="M3 8h18v12H3zM3 8l2-4h14l2 4m-12 4h6"/>',
    "check": '<path d="M14 3H6v18h12V7l-4-4zM14 3v4h4m-9 7 2 2 4-4"/>',
}

STYLE = """
:root{
  color-scheme:light;--bg:#f4f7fb;--surface:#fff;--ink:#17263d;--muted:#53637b;
  --line:#dce4f0;--accent:#215bcc;--accent-hover:#194aa9;--tint:#edf4ff;
  --nav:#101c31;--nav-muted:#b6c5dc;--radius:15px;
  --mono:ui-monospace,Consolas,"Liberation Mono",monospace;
  font-family:system-ui,"Microsoft YaHei","Noto Sans CJK SC",sans-serif
}
*{box-sizing:border-box}
[hidden]{display:none!important}
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
.build-modes{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}
.build-modes a{display:flex;align-items:flex-start;gap:10px;min-width:0;padding:13px 11px;color:var(--ink);
  text-decoration:none;background:var(--surface);border:1px solid var(--line);border-radius:11px}
.build-modes a:hover{border-color:#8fb2ed;background:#f7faff}
.build-modes a[aria-current=true]{border-color:#6997e6;background:var(--tint);box-shadow:inset 0 -3px #3975dc}
.build-modes strong,.build-modes small{display:block;overflow-wrap:anywhere}
.mode-icon{display:grid;place-items:center;width:32px;height:32px;flex:none;border-radius:8px;background:#e8f0fe;color:var(--accent)}
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
.job-form[data-action=fast],.job-form[data-action=build]{display:grid;grid-template-columns:repeat(12,minmax(0,1fr))}
.job-form[data-action=fast]>label{grid-column:span 4}
.job-form[data-action=fast]>[data-field=package],.job-form[data-action=fast]>.preview,
.job-form[data-action=fast]>.form-footer{grid-column:1 / -1}
.job-form[data-action=fast]>[data-field=platform],.job-form[data-action=fast]>[data-field=format]{grid-column:span 3}
.job-form[data-action=fast]>[data-field=output]{grid-column:span 6}
.job-form[data-action=build]>*{grid-column:span 3}
.job-form[data-action=build]>[data-field=context],.job-form[data-action=build]>[data-field=dockerfile],
.job-form[data-action=build]>[data-field=tag]{grid-column:span 6}
.job-form[data-action=build]>[data-field=context]{align-self:start;padding-top:8px}
.job-form[data-action=build]>[data-field=output],.job-form[data-action=build]>.advanced,
.job-form[data-action=build]>.form-footer{grid-column:1 / -1}
.package-controls{display:flex;align-items:end;gap:8px;flex-wrap:wrap}
.package-controls>.field-wide{flex-basis:180px}
.path-field{flex:1 1 260px;min-width:0}
.path-field[data-field=context]{flex-basis:100%}
.job-form[data-action=fast]>.path-field[data-field=output]{grid-column:span 6}
.path-field[data-upload-kind]:not([data-upload-kind=""]){padding:8px;border:1px dashed var(--line);border-radius:8px}
.path-field[data-drag=active]{border-color:var(--accent);background:var(--tint)}
.path-field small{display:block;overflow-wrap:anywhere;margin-top:4px}
.path-field small:empty,.path-field button[hidden],.path-dialog [hidden]{display:none}
.path-dialog{width:min(680px,calc(100% - 24px));max-height:calc(100% - 24px);border:1px solid var(--line);
  border-radius:12px;padding:18px;color:var(--ink);overflow:auto}
.path-dialog::backdrop{background:#15223880}
.path-dialog h2{margin:0 0 12px;font-size:18px}
.path-dialog .actions{margin:8px 0;align-items:end}
.path-dialog ul{max-height:34vh;overflow:auto;margin:8px 0}
.path-dialog li>button{width:100%;justify-content:start;text-align:left;overflow-wrap:anywhere}
.path-dialog button:disabled{opacity:.5;cursor:default}
.form-footer{flex:1 0 100%;padding-top:12px;margin-top:3px;border-top:1px solid #edf1f7;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.form-feedback{color:#912f43}.form-feedback:empty{display:none}
.field-wide{flex-basis:100%}
.advanced,.toolbox{flex:1 0 100%;min-width:0;border:1px solid var(--line);border-radius:10px;background:#f8faff}
.advanced{padding:10px 13px}
.advanced-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;margin-top:12px}
.advanced fieldset{min-width:0;margin:0;padding:9px 12px 12px;border:1px solid var(--line);
  border-radius:9px;background:var(--surface)}
.advanced legend{padding:0 5px;font-weight:700;color:var(--ink)}
.advanced fieldset label{margin-bottom:9px}
.advanced fieldset p{margin-top:8px;font-size:12px}
.advanced .build-params{grid-column:1 / -1}
.build-checks{display:flex;flex-wrap:wrap;gap:0 9px}
.build-checks label.check{margin:0}
summary{cursor:pointer;font-weight:700;color:#294669}.toolbox>summary{padding:14px 17px}.toolbox[open]>summary{border-bottom:1px solid var(--line)}
.toolbox-body{display:grid;gap:8px;padding:12px}.toolbox-body article{box-shadow:none}
.tool-group{min-width:0;border:1px solid var(--line);border-radius:9px;background:var(--surface)}
.tool-group summary{padding:10px 13px}
.tool-group summary small{margin-left:9px;font-weight:500}
.tool-group-body{display:grid;gap:10px;padding:0 9px 9px}
.image-results{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,340px),1fr));gap:12px}
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
.state[data-state=running]::before{content:"";width:12px;height:12px;flex:none;box-sizing:border-box;
  border:2px solid currentColor;border-right-color:transparent;border-radius:50%;animation:job-spin .85s linear infinite}
@keyframes job-spin{to{transform:rotate(1turn)}}
.state[data-state=queued],.state[data-state=unverified]{background:#fff2de;color:#795018}
.state[data-state=cancelled],.state[data-state=missing],.state[data-state=unknown],.state[data-state=unreadable]{background:#edf0f5;color:#4d5d73}
.meta{display:flex;gap:4px 13px;flex-wrap:wrap;font-size:13px;overflow-wrap:anywhere}
.controls,.actions{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.controls{margin-top:16px}.controls .muted{margin-right:auto}
.artifact{display:grid;gap:7px;padding:11px 12px;border:1px solid #e8edf6;border-radius:9px;background:#f9fbff}
.artifact-line{display:flex;align-items:baseline;gap:8px;flex-wrap:wrap;min-width:0}
.history-filter{margin-bottom:15px}.history-filter form{margin:0}
.job-summary{display:flex;align-items:center;flex-wrap:wrap;gap:8px;margin:0 0 10px}.job-list{display:grid;gap:8px}
.task-stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;margin:12px 0}
.task-stats>div{padding:9px 12px;border:1px solid var(--line);border-radius:9px;background:#f8faff}
.task-stats dt{font-size:12px}.task-stats dd{font-size:23px;font-weight:750;font-variant-numeric:tabular-nums}
.job-row{display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;padding:10px 0;border-bottom:1px solid #edf1f7}
.job-row:last-child{border-bottom:0}.job-row strong{margin-left:4px}
.job-ident{min-width:0}.job-ident code{display:block;color:var(--muted);margin-top:3px}.job-ident code:empty{display:none}
.job-stage{padding:9px 12px;border:1px solid var(--line);border-radius:9px;background:#f8faff}
.job-stage small{display:block;font-weight:650}
.log-panel{padding:0}
.log-toolbar{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;padding:14px 17px}
.log-toolbar h2{margin:0}.log-actions button,.log-actions .button{padding:5px 9px;min-height:32px;font-size:12px}
.log-notes,.log-panel noscript{display:block;padding:9px 17px}.log-notes p{margin-bottom:4px}
pre{font:12px/1.6 var(--mono);white-space:pre-wrap;overflow-wrap:anywhere;background:#152238;color:#e2ecfb;padding:12px 14px;border-radius:9px;
  max-height:460px;overflow:auto;margin:9px 0}
#job-status{min-height:1.6em;margin:0;overflow-wrap:anywhere}
#job-output{overflow-anchor:none;margin:0;border-radius:0;max-height:min(62vh,620px);scrollbar-color:#526781 #101b2d;
  background:#101b2d;color:#d0dbea;padding:9px 0 14px;font-size:13px;line-height:1.8;tab-size:4}
#job-output>span{display:grid;grid-template-columns:15ch minmax(0,1fr);gap:12px;min-height:1.8em;padding:2px 17px 2px 0;
  border-left:3px solid transparent}
#job-output>span::before{content:attr(data-line) "  " attr(data-time);color:#8294ad;text-align:right;white-space:pre;
  font-weight:400;user-select:none;font-variant-numeric:tabular-nums}
#job-output>.log-stage{color:#bed8ff;background:#1a2c46;border-left-color:#79a5ec;font-weight:650;margin-top:7px;padding-block:7px}
#job-output>.log-success{color:#a3ddc5}
#job-output>.log-error{color:#ffd1d1;background:#39232d;border-left-color:#e68b96;font-weight:650;padding-block:6px}
#job-output>.log-cache{color:#eed49d}
#job-output>.log-warning{color:#f1d3a0;background:#302a22;border-left-color:#b99c65}
#job-output>.log-summary{color:#d9e8ff;background:#182941}
.log-legend{display:flex;gap:11px;margin-top:6px;font-size:11px;color:var(--muted)}
.log-legend>span::before{content:"";display:inline-block;width:6px;height:6px;border-radius:2px;margin-right:5px;background:#729cda}
.log-legend>.legend-success::before{background:#3c9878}.log-legend>.legend-cache::before{background:#ba8b34}
.log-legend>.legend-error::before{background:#ce6b7a}
#job-flow:empty{display:none}.pull-flow{padding:18px 22px;border:1px solid var(--line);border-radius:var(--radius);background:#fff}
.pull-flow header{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap}.pull-flow h3{margin:0;font-size:14px}
.pull-flow header code{font-size:11px;color:var(--muted);min-width:0;overflow-wrap:anywhere}
.pull-flow ol{list-style:none;display:grid;grid-template-columns:repeat(5,minmax(0,1fr));padding:0;margin:18px 0;gap:12px}
.pull-flow li{display:flex;gap:8px;align-items:center;position:relative;font-size:12px;color:#687890}
.pull-flow li small{display:block;font-size:10px;margin-top:2px}
.flow-dot{display:grid;place-items:center;flex:0 0 27px;height:27px;border-radius:50%;background:#edf1f7;color:#75849a;font:600 10px var(--mono)}
.flow-done .flow-dot{background:#e3f3eb;color:#267658}.flow-active .flow-dot{background:#285ec4;color:white;box-shadow:0 0 0 4px #edf3ff}
.pull-flow .flow-active{color:#224f9d;font-weight:700}.flow-stopped .flow-dot{background:#fbe7eb;color:#a43c54}
.flow-metrics{display:flex;gap:10px 23px;flex-wrap:wrap;padding-top:12px;border-top:1px solid #e8edf5;color:var(--muted);font-size:11px}
.flow-metrics strong{color:#263a55;margin:0 6px;font:600 13px var(--mono)}
.log-empty{display:flex;align-items:center;gap:10px;min-height:76px;margin:0;padding:15px 17px;
  background:#101b2d;color:#d0dbea;font-size:13px}
.log-empty[data-waiting="1"]::before{content:"";width:13px;height:13px;flex:none;color:#a5c9ff;
  border:2px solid currentColor;border-right-color:transparent;border-radius:50%;animation:job-spin .85s linear infinite}
@media(prefers-reduced-motion:reduce){.state[data-state=running]::before,
  .log-empty[data-waiting="1"]::before{animation:none}}
.tiles{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}
.operation-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
.tile-value{font-size:24px;letter-spacing:-.035em;font-weight:750}.tile-path{display:block;margin-top:6px}
.empty{color:var(--muted);padding:23px;text-align:center}
@media(max-width:900px){
  .shell{grid-template-columns:178px minmax(0,1fr)}.sidebar{padding-inline:10px}
  .build-modes{grid-template-columns:repeat(2,minmax(0,1fr))}.tiles{grid-template-columns:repeat(auto-fit,minmax(150px,1fr))}
}
@media(max-width:680px){
  .pull-flow{padding:15px}.pull-flow ol{grid-template-columns:1fr;gap:13px}
  #job-output>span{grid-template-columns:11ch minmax(0,1fr);gap:8px;padding-right:10px;font-size:12px}
  .shell{display:block}.sidebar{position:static;height:auto;padding:12px 14px;gap:10px;overflow:visible}
  .brand{padding:0}.brand-sub{display:none}.brand-icon{height:30px;flex-basis:30px}.sidebar-foot{display:none}
  .tabs{grid-template-columns:repeat(3,minmax(0,1fr));gap:4px}.tabs a{gap:5px;padding:6px 7px;font-size:13px}.tabs .icon{width:15px;height:15px}
  main{padding:17px 13px 34px}.page-head{margin-bottom:14px;padding-bottom:13px;gap:6px}.local{margin-top:0}
  article,.panel,.empty{padding:14px}.log-panel{padding:0}
  form{gap:10px}.records{gap:11px}
  .operation-grid{grid-template-columns:minmax(0,1fr)}
  .job-form[data-action=fast]{grid-template-columns:repeat(2,minmax(0,1fr))}
  .job-form[data-action=fast]>label,.job-form[data-action=fast]>[data-field=platform],
  .job-form[data-action=fast]>[data-field=format]{grid-column:span 1}
  .job-form[data-action=fast]>[data-field=output],.job-form[data-action=fast]>[data-field=profile]{grid-column:1 / -1}
  .job-form[data-action=fast]>.path-field[data-field=output]{grid-column:1 / -1}
  .job-form[data-action=build]{grid-template-columns:repeat(2,minmax(0,1fr))}
  .job-form[data-action=build]>label{grid-column:span 1}
  .job-form[data-action=build]>[data-field=tag]{grid-column:1 / -1}
  .job-form[data-action=build]>.path-field{grid-column:1 / -1}
  .advanced-grid{grid-template-columns:minmax(0,1fr)}
  .task-stats{grid-template-columns:repeat(2,minmax(0,1fr))}
}
@media(max-width:390px){.local{display:none}.tabs a{font-size:12px}.tiles{grid-template-columns:1fr}}

/* 产物操作按任务选择，表单只展示当前操作；禁用脚本仍可使用全部表单。 */
[data-delivery-workspace]{display:grid;gap:16px;min-width:0}
.delivery-source{padding:19px 23px;background:linear-gradient(115deg,#f9fcff,#fff)}
.delivery-source-heading{display:flex;gap:15px;align-items:center;min-width:0}
.delivery-source-heading>.icon{width:46px;height:46px;padding:9px;border:1px solid #d4e3fa;border-radius:12px;background:var(--tint);color:var(--accent)}
.delivery-source-heading>div{min-width:0}.delivery-source h2{overflow-wrap:anywhere;margin:3px 0 7px}
.delivery-source details{margin-top:13px;padding-top:11px;border-top:1px solid var(--line)}
.delivery-source summary{font-size:12px}.delivery-source dl{margin-top:14px}
.operation-selector h2{margin-bottom:4px}.operation-selector>p{margin-top:0}
.operation-groups{display:flex;gap:6px;padding:5px;background:#eef2f8;border-radius:11px;margin:16px 0}
.operation-groups button{flex:1;border-color:transparent;background:transparent;box-shadow:none}
.operation-groups [aria-pressed=true]{background:#fff;color:var(--accent);box-shadow:0 2px 5px #1b355a10}
.operation-options h3{font-size:13px;margin:14px 0 9px;color:var(--muted)}
.operation-choices{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:9px}
.operation-choice{display:flex;flex-direction:column;gap:5px;min-width:0;text-decoration:none;padding:13px;border:1px solid var(--line);border-radius:10px;background:#fff;color:var(--ink)}
.operation-choice:hover{border-color:#92b3ed;background:#f8fbff;color:var(--ink)}
.operation-choice[aria-current=true]{border-color:var(--accent);background:var(--tint);box-shadow:0 0 0 1px var(--accent)}
.operation-choice-top{display:flex;align-items:start;justify-content:space-between;gap:6px;flex-wrap:wrap}
.operation-choice strong{font-size:14px}.operation-choice>span:not(.operation-choice-top){font-size:12px;color:#344b6d}
.operation-choice small{font-size:12px;line-height:1.6;color:var(--muted)}
.operation-impact{display:inline-flex;padding:2px 6px;border-radius:5px;background:#eef3fa;color:#476185;font-size:10px;white-space:nowrap;font-weight:650}
.operation-choice[aria-current=true] .operation-impact{background:#dce9ff;color:#2457b2}
.operation-panels{display:grid;gap:14px}.operation-panel{min-width:0;border-top:3px solid #7ea4e4}
.operation-panel header h2{margin:7px 0 5px}.operation-panel header p{margin:0 0 17px}
.operation-panel .job-form{padding-top:15px;border-top:1px solid #edf1f7}
.operation-panel .suggest-output{align-self:center;font-size:12px;padding:7px 10px}
.operation-panel .job-form>p{flex-basis:100%}.operation-panel textarea{min-height:88px}
/* 路径弹窗保持地址、列表和确认区稳定，长列表仅在中部滚动。 */
.path-dialog{width:min(960px,calc(100vw - 24px));height:min(760px,calc(100vh - 24px));max-height:calc(100vh - 24px);padding:0;overflow:hidden;border-radius:16px;box-shadow:0 24px 80px #0d1c3c40}
.path-dialog[open]{display:grid;grid-template-rows:auto auto auto minmax(100px,1fr) auto auto}
.path-head{display:flex;align-items:start;justify-content:space-between;gap:12px;padding:18px 21px 12px}
.path-head h2{margin:1px 0 3px}.path-head p{font-size:12px;margin:0}
.path-head .eyebrow{font-size:10px}.path-head button{min-height:32px;padding:5px 10px;flex:none;white-space:nowrap}
.path-address{display:flex;gap:9px;align-items:end;padding:0 21px}
.path-address>label{flex:1;min-width:0;font-size:11px}.path-address input{font-family:var(--mono);font-size:12px}
.path-quick{display:none}.path-navigation{display:flex;gap:4px}.path-navigation button{padding:7px 11px;min-width:34px}
.path-crumbs{display:flex;gap:2px;overflow:auto;white-space:nowrap;padding:10px 21px;min-height:42px}
.path-crumbs button{border:0;background:transparent;padding:3px 7px;font-size:12px;min-height:26px;flex:none}
.path-crumbs button+button::before{content:"/";margin-right:10px;color:#9aa9bd}
.path-crumbs [aria-current]{background:var(--tint);color:var(--accent)}
.path-browser{display:grid;grid-template-columns:164px minmax(0,1fr);min-height:0;border-block:1px solid var(--line)}
.path-places{padding:11px;background:#f6f8fc;overflow:auto;border-right:1px solid var(--line)}
.path-places h3{font-size:11px;color:var(--muted);margin:7px 7px}
.path-places button{display:block;width:100%;text-align:left;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px;border:0;min-height:32px;padding:6px 8px;background:transparent;margin:3px 0}
.path-places button[aria-current]{background:#e5edfb;color:var(--accent)}
.path-files{display:grid;grid-template-rows:auto auto minmax(0,1fr) auto;min-height:0;min-width:0}
.path-filter{display:flex;align-items:end;gap:9px;padding:11px 13px}
.path-filter>label:first-child{flex:1;min-width:0;font-size:11px}.path-filter .check{font-size:11px;white-space:nowrap;min-height:36px;margin:0}
.path-filter>button{min-height:36px;font-size:12px;padding:6px 9px}
.path-list-head,.path-entry{display:grid;grid-template-columns:minmax(0,1fr) 75px 117px;gap:12px;align-items:center}
.path-list-head{font-size:10px;color:var(--muted);background:#f9fbfe;padding:6px 15px;border-block:1px solid #edf1f7}
.path-dialog [data-entries]{max-height:none;overflow:auto;margin:0;min-height:0;overscroll-behavior:contain}
.path-dialog [data-entries][aria-busy=true]{opacity:.45;pointer-events:none}
.path-dialog [data-entries] li+li{margin:0}
.path-dialog [data-entries] li>button{width:100%;text-align:left;border:0;border-bottom:1px solid #f0f3f8;border-radius:0;padding:8px 15px;background:#fff;color:var(--ink);min-height:43px;font-weight:400}
.path-dialog [data-entries] li>button:hover{background:#f4f8ff}
.path-dialog [data-entries] li>button[aria-pressed=true]{background:#e9f1ff;box-shadow:inset 3px 0 var(--accent)}
.path-entry-name{display:flex;align-items:center;gap:9px;min-width:0}
.path-entry-name>span:last-child{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px}
.path-folder-mark,.path-file-mark{display:grid;place-items:center;width:25px;height:25px;flex:none;border-radius:6px;font:bold 15px var(--mono)}
.path-folder-mark{background:#fff0cd;color:#9f751f}.path-file-mark{background:#edf2fa;color:#7485a2}
.path-entry-size,.path-entry-date{font-size:10px;color:var(--muted);white-space:nowrap}
.path-list-bottom{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:7px 13px;min-height:38px;border-top:1px solid #edf1f7;font-size:11px;color:var(--muted)}
.path-list-bottom button{padding:3px 9px;min-height:26px;font-size:11px}
.path-status{font-size:11px;margin:0;padding:7px 21px;min-height:30px;overflow-wrap:anywhere;background:#f9fbfe}
.path-status[data-tone=error],[data-validation][data-tone=error]{color:#a33246}
.path-status[data-tone=warning]{color:#8b631f}
.path-status[data-tone=loading]::before{content:"";display:inline-block;width:11px;height:11px;vertical-align:-1px;border:2px solid #8eaccf;border-right-color:transparent;border-radius:50%;animation:job-spin .85s linear infinite;margin-right:7px}
.path-footer{padding:13px 21px 16px;border-top:1px solid var(--line);display:grid;gap:9px;background:#fff}
.path-footer>label{font-size:11px}.path-footer input{font-size:12px}
.path-selection{min-width:0;display:flex;gap:12px;align-items:baseline}.path-selection small{color:var(--muted);white-space:nowrap;font-size:10px}
.path-selection code{display:block;max-height:3.3em;overflow:auto;font-size:11px;overflow-wrap:anywhere}
.path-apply{display:flex;align-items:center;justify-content:space-between;gap:12px}
.path-apply>span{font-size:11px;overflow-wrap:anywhere}.path-apply>button{flex:none}
[data-validation][data-tone=success]{color:#237356}
@media(max-width:900px){.operation-choices{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:680px){
 .operation-choices{grid-template-columns:minmax(0,1fr)}.operation-choice{padding:10px}.operation-choice small{display:none}.operation-groups{gap:2px}.operation-groups button{font-size:12px;padding:7px 5px}
 .delivery-source{padding:15px}.delivery-source-heading{gap:10px}.delivery-source-heading>.icon{width:37px;height:37px;padding:7px}
 .path-dialog{width:calc(100vw - 12px);height:calc(100vh - 12px);max-height:calc(100vh - 12px);border-radius:12px}
 .path-head{padding:13px 13px 9px}.path-head p{font-size:11px}
 .path-address{padding:0 13px;gap:6px;flex-wrap:wrap}.path-navigation{order:1}.path-quick{display:block;order:2;flex:1;min-width:0;min-height:34px;font-size:12px}.path-address>label{flex:1 0 calc(100% - 75px)}
 .path-crumbs{padding:7px 13px;min-height:36px}.path-browser{grid-template-columns:minmax(0,1fr)}
 .path-places{display:none}.path-filter{padding:8px 10px;gap:6px;flex-wrap:wrap}.path-filter>label:first-child{flex-basis:100%}
 .path-filter .check{margin-right:auto}.path-filter>button{min-height:29px;padding:3px 8px}
 .path-list-head,.path-entry{grid-template-columns:minmax(0,1fr) 68px;gap:8px}.path-list-head>span:last-child,.path-entry-date{display:none}
 .path-dialog [data-entries] li>button{padding:7px 11px}
 .path-footer{padding:10px 13px 12px;gap:7px}.path-status{padding:6px 13px;min-height:26px}
 .path-apply{gap:8px}.path-apply>button{padding:8px 10px;min-height:36px}.path-apply>span{font-size:10px}
}
@media(prefers-reduced-motion:reduce){.path-status[data-tone=loading]::before{animation:none}}

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
