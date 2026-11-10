"""本机路径浏览与网页文件选择控件。"""

import base64
import hashlib
import heapq
import json
import os
import stat
import tarfile
from pathlib import Path

from errors import BuildError
from path_policy import host_components, output_path, resolve_host_path

OUTPUT_SUFFIXES = {"tar": (".tar",), "json": (".json",), "gzip": (".tar.gz", ".tgz"),
                   "archive": (".tar", ".tar.gz", ".tgz")}
BROWSE_MODES = ("directory", "file", "output", "output-json", "output-gzip", "output-archive")


def _locations(directory):
    """快捷入口只提供位置，不预读盘符或挂载盘，避免列目录时触发额外网络 I/O。"""
    places = [("工作目录", Path.cwd()), ("用户目录", Path.home())]
    if os.name == "nt":
        import ctypes
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        places += [("磁盘 " + chr(65 + i) + ":", Path(chr(65 + i) + ":/"))
                   for i in range(26) if mask & (1 << i)]
    else:
        places.append(("根目录", Path("/")))
        places += [(label, Path(path)) for label, path in
                   (("挂载目录 /media", "/media"), ("挂载目录 /mnt", "/mnt"), ("移动存储", "/run/media"))
                   if Path(path).is_dir()]
    seen, result = set(), []
    for label, path in places:
        value = str(path)
        if value not in seen:
            seen.add(value)
            result.append({"label": label, "path": value})
    crumbs = [{"label": part.name or str(part), "path": str(part)}
              for part in list(reversed(directory.parents)) + [directory]]
    return result, crumbs


def browse(data):
    mode, initial = data.get("mode"), data.get("initial", "")
    hidden, cursor = data.get("hidden", "1"), data.get("cursor", "")
    if (set(data) - {"path", "filter", "mode", "base", "initial", "hidden", "cursor"} or mode not in BROWSE_MODES
            or initial not in ("", "1") or hidden not in ("0", "1")):
        raise BuildError("无效的路径浏览请求")
    raw, base = data.get("path", ""), data.get("base", "")
    search = data.get("filter", "").casefold()
    if len(raw) > 4096 or len(base) > 4096 or len(search) > 255 or "\0" in raw or "\0" in base or len(cursor) > 4096:
        raise BuildError("路径或筛选词不合法")
    try:
        root = Path(base) if base else Path.cwd()
        start = resolve_host_path(root / Path(raw).expanduser()) if raw else resolve_host_path(root)
    except RuntimeError as exc:
        raise BuildError("无法展开所填路径") from exc
    directory = start
    if initial and mode != "directory" and not start.is_dir():
        directory = start.parent
    hint = ""
    if initial and mode != "directory" and not directory.is_dir():
        original = directory
        while directory.parent != directory and not directory.is_dir():
            directory = directory.parent
        if directory.is_dir():
            hint = "原位置不存在，已打开最近的父目录：" + str(original)
    if not directory.is_dir():
        raise BuildError("目录不存在：" + str(directory))
    context = hashlib.sha256(json.dumps([str(directory), mode, search, hidden],
                                       ensure_ascii=True).encode("ascii")).hexdigest()
    after = None
    if cursor:
        try:
            raw_cursor = base64.b64decode(cursor.encode("ascii"), altchars=b"-_", validate=True)
            decoded = json.loads(raw_cursor)
            if (not isinstance(decoded, list) or len(decoded) != 3 or decoded[0] != context or
                    type(decoded[1]) is not int or decoded[1] not in (0, 1) or
                    not isinstance(decoded[2], str) or not decoded[2] or len(decoded[2]) > 1024):
                raise ValueError("Invalid cursor")
            after = (decoded[1], decoded[2].casefold(), decoded[2])
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise BuildError("目录翻页位置已失效，请重新打开或筛选") from exc
    matched = 0

    def matching():
        nonlocal matched
        with os.scandir(directory) as entries:
            for entry in entries:
                if (search not in entry.name.casefold() or hidden == "0" and entry.name.startswith(".") or
                        any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in entry.name)):
                    continue
                try:
                    # 不进入链接或 Windows reparse point；列表元数据也不跟随链接。
                    if entry.is_symlink() or (os.name == "nt" and
                            getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0) & 0x400):
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        kind = 0
                    elif mode != "directory" and entry.is_file(follow_symlinks=False):
                        kind = 1
                    else:
                        continue
                    matched += 1
                    key = (kind, entry.name.casefold(), entry.name)
                    if after is None or key > after:
                        yield key + (entry.path,)
                except OSError:
                    continue

    items = heapq.nsmallest(201, matching())
    shown, rows = items[:200], []
    for kind, _, name, path in shown:
        row = {"name": name, "path": path, "directory": kind == 0}
        try:
            info = os.stat(path, follow_symlinks=False)
            row.update(bytes=info.st_size if stat.S_ISREG(info.st_mode) else None, modified=info.st_mtime)
        except OSError:
            row.update(bytes=None, modified=None)
        rows.append(row)
    next_cursor = ""
    if len(items) > 200:
        kind, _, name, _ = shown[-1]
        next_cursor = base64.urlsafe_b64encode(json.dumps([context, kind, name], ensure_ascii=True).encode("ascii")).decode("ascii")
    selected = None
    if initial and mode == "file" and start.is_file():
        info = start.stat()
        selected = {"name": start.name, "path": str(start), "directory": False,
                    "bytes": info.st_size, "modified": info.st_mtime}
    places, crumbs = _locations(directory)
    return {"path": str(directory), "parent": str(directory.parent),
            "filename": start.name if start != directory else "", "more": len(items) > 200,
            "hint": hint, "separator": os.sep, "entries": rows, "selected": selected, "total": matched, "next": next_cursor, "places": places, "breadcrumbs": crumbs}


def selected_output(directory, filename, suffix=".tar"):
    if (not filename or filename in (".", "..") or "/" in filename or "\\" in filename or
            any(ord(char) < 32 or ord(char) == 127 for char in filename)):
        raise BuildError("请输入有效的输出文件名")
    host_components([filename])
    parent = resolve_host_path(directory)
    if not parent.is_dir():
        raise BuildError("所选输出目录已不存在")
    path = output_path(parent / filename)
    allowed = (suffix,) if isinstance(suffix, str) else suffix
    if not str(path).lower().endswith(allowed) or path.exists():
        raise BuildError("请选择尚不存在的 {} 文件：{}".format(" / ".join(allowed), path))
    return str(path)


def check_oci_platform(path, expected):
    from multiarch import probe_input
    try:
        return probe_input(path, expected)["digest"]
    except (BuildError, OSError, tarfile.TarError) as exc:
        raise BuildError("请选择 linux/{} 的单平台 OCI tar：{}".format(expected, exc)) from exc


PATH_SCRIPT = r"""
(() => {
  const fields = Array.from(document.querySelectorAll('[data-path-mode]'));
  if (!fields.length) return;
  const base = new URL('.', document.currentScript.src);
  const endpoint = name => new URL(name, base);
  async function post(name, values, signal) {
    const response = await fetch(endpoint(name), {method: 'POST', body: new URLSearchParams(values), signal});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || '请求失败');
    return result;
  }
  const dialog = document.createElement('dialog');
  dialog.className = 'path-dialog';
  dialog.setAttribute('aria-labelledby', 'path-dialog-title');
  dialog.innerHTML = '<header class="path-head"><div><span class="eyebrow">本机文件</span>' +
    '<h2 id="path-dialog-title">选择路径</h2><p data-purpose class="muted"></p></div>' +
    '<button type="button" class="secondary" data-close aria-label="关闭路径选择">关闭</button></header>' +
    '<div class="path-address"><div class="path-navigation">' +
    '<button type="button" class="secondary" data-back aria-label="后退">←</button>' +
    '<button type="button" class="secondary" data-forward aria-label="前进">→</button>' +
    '<button type="button" class="secondary" data-parent aria-label="上一级">↑</button></div>' +
    '<select class="path-quick" data-quick aria-label="快捷位置"><option value="">快捷位置</option></select>' +
    '<label>目录地址<input data-location autocomplete="off" spellcheck="false"></label>' +
    '<button type="button" data-open>打开</button></div>' +
    '<nav class="path-crumbs" data-crumbs aria-label="当前目录层次"></nav>' +
    '<div class="path-browser"><aside class="path-places"><h3>快捷位置</h3><div data-places></div>' +
    '<h3 data-recent-title hidden>最近访问</h3><div data-recent></div></aside>' +
    '<section class="path-files"><div class="path-filter"><label>筛选名称<input data-filter placeholder="输入名称，实时筛选" autocomplete="off"></label>' +
    '<label class="check"><input type="checkbox" data-hidden>显示隐藏项</label>' +
    '<button type="button" class="secondary" data-refresh aria-label="刷新当前目录">刷新</button></div>' +
    '<div class="path-list-head"><span>名称</span><span>大小</span><span>修改时间</span></div>' +
    '<ul data-entries aria-label="目录内容"></ul><div class="path-list-bottom"><span data-count></span>' +
    '<button type="button" class="secondary" data-more hidden>加载更多</button></div></section></div>' +
    '<p data-browse-status class="path-status" role="status"></p>' +
    '<footer class="path-footer"><label data-name-label hidden>输出文件名<input data-name autocomplete="off" spellcheck="false"></label>' +
    '<div class="path-selection"><small data-selection-label>将使用</small><code data-preview></code></div>' +
    '<div class="path-apply"><span data-validation role="status"></span><button type="button" data-apply>选择当前目录</button></div></footer>';
  document.body.appendChild(dialog);
  const el = name => dialog.querySelector('[data-' + name + ']');
  const locationField = el('location'), filter = el('filter'), entries = el('entries');
  const apply = el('apply'), parent = el('parent'), filename = el('name');
  const browseStatus = el('browse-status'), validation = el('validation');
  const recent = [];
  let active, current, selectedFile, revision = 0, outputRevision = 0, nameRevision = 0;
  let listingRequest, validationRequest, filterTimer, validationTimer, verifiedOutput;
  let trail = [], trailIndex = -1, shown = 0, searchPath = '';
  const outputKind = () => ({output: 'tar', 'output-json': 'json', 'output-gzip': 'gzip', 'output-archive': 'archive'})[active.dataset.pathMode];
  function normalName() {
    const value = filename.value;
    return value && !value.includes('.') ? value + (outputKind() === 'json' ? '.json' : outputKind() === 'gzip' ? '.tar.gz' : '.tar') : value;
  }
  function size(value) {
    if (value === null || value === undefined) return '—';
    const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB']; let i = 0;
    while (value >= 1024 && i < units.length - 1) { value /= 1024; i++; }
    return value.toLocaleString(undefined, {maximumFractionDigits: i ? 1 : 0}) + ' ' + units[i];
  }
  function cancelValidation() {
    outputRevision++;
    clearTimeout(validationTimer);
    if (validationRequest) validationRequest.abort();
    verifiedOutput = null;
    apply.disabled = true;
    validation.textContent = '';
    validation.dataset.tone = '';
  }
  function updateSelection() {
    cancelValidation();
    const id = outputRevision;
    if (!current) { el('preview').textContent = '先打开一个目录'; return; }
    if (!outputKind()) {
      el('preview').textContent = active.dataset.pathMode === 'file' ? (selectedFile ? selectedFile.path : '尚未选择文件') : current.path;
      apply.disabled = active.dataset.pathMode === 'file' && !selectedFile;
      return;
    }
    const name = normalName();
    el('preview').textContent = name ? current.path.replace(/[\\/]$/, '') + current.separator + name : '填写文件名后预览完整路径';
    if (!name) { validation.textContent = '请输入输出文件名'; return; }
    validation.textContent = '正在检查文件名与冲突…';
    const path = current.path;
    validationTimer = setTimeout(async () => {
      validationRequest = new AbortController();
      try {
        const result = await post('choose-output', {path, filename: name, kind: outputKind()}, validationRequest.signal);
        if (id !== outputRevision || !dialog.open || !current || current.path !== path) return;
        verifiedOutput = result.path;
        el('preview').textContent = result.path;
        validation.textContent = name !== filename.value ? '将自动补全扩展名，可使用该新路径' : '新文件，可使用该路径';
        validation.dataset.tone = 'success';
        apply.disabled = false;
      } catch (error) {
        if (id === outputRevision && dialog.open && error.name !== 'AbortError') {
          validation.textContent = error.message;
          validation.dataset.tone = 'error';
        }
      }
    }, 220);
  }
  function locationButton(label, path) {
    const button = document.createElement('button');
    button.type = 'button'; button.className = 'secondary';
    button.textContent = label; button.title = path;
    if (current && current.path === path) button.setAttribute('aria-current', 'location');
    button.addEventListener('click', () => { filter.value = ''; browse(path); });
    return button;
  }
  function renderLocations(result) {
    el('places').textContent = ''; el('recent').textContent = ''; el('crumbs').textContent = '';
    el('quick').textContent = '';
    const placeholder = document.createElement('option'); placeholder.value = ''; placeholder.textContent = '快捷位置';
    el('quick').appendChild(placeholder);
    for (const place of result.places) {
      const option = document.createElement('option'); option.value = place.path; option.textContent = place.label;
      el('quick').appendChild(option);
    }
    for (const place of result.places) el('places').appendChild(locationButton(place.label, place.path));
    for (const place of recent.filter(path => path !== result.path)) {
      el('recent').appendChild(locationButton(place.split(/[\\/]/).filter(Boolean).pop() || place, place));
    }
    el('recent-title').hidden = !el('recent').children.length;
    for (const crumb of result.breadcrumbs) el('crumbs').appendChild(locationButton(crumb.label, crumb.path));
    el('crumbs').scrollLeft = el('crumbs').scrollWidth;
  }
  function markSelected() {
    for (const button of entries.querySelectorAll('button')) button.setAttribute('aria-pressed', String(selectedFile && selectedFile.path === button.dataset.path));
  }
  function addEntry(item) {
    const row = document.createElement('li'), button = document.createElement('button');
    button.type = 'button'; button.className = 'path-entry'; button.dataset.path = item.path;
    button.tabIndex = entries.children.length ? -1 : 0;
    const name = document.createElement('span'); name.className = 'path-entry-name';
    const marker = document.createElement('span'); marker.className = item.directory ? 'path-folder-mark' : 'path-file-mark';
    marker.textContent = item.directory ? '/' : '·'; marker.setAttribute('aria-hidden', 'true');
    const text = document.createElement('span'); text.textContent = item.name; text.title = item.name;
    name.append(marker, text);
    const bytes = document.createElement('span'); bytes.className = 'path-entry-size'; bytes.textContent = item.directory ? '文件夹' : size(item.bytes);
    const modified = document.createElement('span'); modified.className = 'path-entry-date';
    modified.textContent = item.modified ? new Date(item.modified * 1000).toLocaleString(undefined, {month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit'}) : '—';
    button.append(name, bytes, modified);
    button.addEventListener('click', () => {
      if (!current || entries.getAttribute('aria-busy') === 'true') return;
      if (item.directory) { filter.value = ''; browse(item.path); }
      else { selectedFile = item; if (outputKind()) { filename.value = item.name; nameRevision++; } markSelected(); updateSelection(); }
    });
    button.addEventListener('dblclick', () => {
      if (current && !item.directory && active.dataset.pathMode === 'file') usePath(item.path);
    });
    row.appendChild(button); entries.appendChild(row);
  }
  async function browse(path, initial = false, search = '', cursor = '', move = null) {
    const id = ++revision, nameVersion = nameRevision, append = !!cursor, hidden = el('hidden').checked;
    if (listingRequest) listingRequest.abort();
    listingRequest = new AbortController();
    clearTimeout(filterTimer); cancelValidation();
    current = null; if (!append) selectedFile = null;
    parent.disabled = true; el('more').disabled = true;
    entries.setAttribute('aria-busy', 'true');
    browseStatus.textContent = '正在读取目录…'; browseStatus.dataset.tone = 'loading';
    if (!initial) { locationField.value = path; searchPath = path; }
    try {
      const result = await post('browse-path', {path, filter: search, cursor, mode: active.dataset.pathMode,
        initial: initial ? '1' : '', hidden: hidden ? '1' : '0', base: active.dataset.pathBase || ''}, listingRequest.signal);
      if (id !== revision || !dialog.open) return;
      current = result; searchPath = result.path; locationField.value = result.path;
      if (initial && nameVersion === nameRevision) filename.value = result.filename;
      if (filter.value !== search || hidden !== el('hidden').checked) { browse(result.path, false, filter.value); return; }
      if (!append) { entries.textContent = ''; shown = 0; }
      for (const item of result.entries) addEntry(item);
      shown += result.entries.length;
      if (initial && active.dataset.pathMode === 'file') selectedFile = result.selected || null;
      markSelected();
      parent.disabled = result.parent === result.path;
      el('count').textContent = '已显示 ' + shown + ' / ' + result.total + ' 项';
      el('more').hidden = !result.next; el('more').disabled = false;
      entries.setAttribute('aria-busy', 'false');
      if (move !== null) trailIndex = move;
      else if (trail[trailIndex] !== result.path) { trail = trail.slice(0, trailIndex + 1); trail.push(result.path); trailIndex = trail.length - 1; }
      el('back').disabled = trailIndex <= 0; el('forward').disabled = trailIndex >= trail.length - 1;
      const found = recent.indexOf(result.path); if (found >= 0) recent.splice(found, 1);
      recent.unshift(result.path); recent.splice(6);
      renderLocations(result);
      browseStatus.textContent = result.hint || (result.total ? (active.dataset.pathMode === 'file' ? '单击选择文件，双击直接使用；单击文件夹进入。' : '单击文件夹进入，再确认下方路径。') : '这里没有符合条件的条目，可修改筛选或返回上一级。');
      browseStatus.dataset.tone = result.hint ? 'warning' : '';
      updateSelection();
    } catch (error) {
      if (id === revision && dialog.open && error.name !== 'AbortError') {
        browseStatus.textContent = error.message; browseStatus.dataset.tone = 'error';
        entries.setAttribute('aria-busy', 'false'); el('more').hidden = true; updateSelection();
      }
    }
  }
  function usePath(path) {
    const input = active.querySelector('input:not([type])');
    input.value = path;
    input.dispatchEvent(new Event('input', {bubbles: true}));
    input.dispatchEvent(new Event('change', {bubbles: true}));
    dialog.close();
  }
  el('close').addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', () => {
    revision++; cancelValidation(); clearTimeout(filterTimer);
    if (listingRequest) listingRequest.abort();
  });
  locationField.addEventListener('input', () => {
    revision++; if (listingRequest) listingRequest.abort();
    current = null; searchPath = ''; selectedFile = null; entries.textContent = ''; el('count').textContent = '';
    parent.disabled = true; el('more').hidden = true;
    browseStatus.textContent = '按 Enter 或点击“打开”前往所填目录。'; browseStatus.dataset.tone = '';
    updateSelection();
  });
  function searchNow() { const path = current ? current.path : searchPath; if (path) browse(path, false, filter.value); }
  filter.addEventListener('input', () => { clearTimeout(filterTimer); filterTimer = setTimeout(searchNow, 240); });
  el('hidden').addEventListener('change', searchNow);
  el('quick').addEventListener('change', () => {
    const path = el('quick').value;
    if (path) { filter.value = ''; browse(path); }
  });
  if (typeof ResizeObserver === 'function') {
    new ResizeObserver(() => { el('crumbs').scrollLeft = el('crumbs').scrollWidth; }).observe(el('crumbs'));
  }
  el('open').addEventListener('click', () => { filter.value = ''; browse(locationField.value); });
  el('refresh').addEventListener('click', () => browse(locationField.value, false, filter.value));
  parent.addEventListener('click', () => { if (current) { filter.value = ''; browse(current.parent); } });
  el('back').addEventListener('click', () => { if (trailIndex > 0) { filter.value = ''; browse(trail[trailIndex - 1], false, '', '', trailIndex - 1); } });
  el('forward').addEventListener('click', () => { if (trailIndex < trail.length - 1) { filter.value = ''; browse(trail[trailIndex + 1], false, '', '', trailIndex + 1); } });
  el('more').addEventListener('click', () => { if (current && current.next) browse(current.path, false, filter.value, current.next); });
  filename.addEventListener('input', () => { nameRevision++; updateSelection(); });
  for (const [input, trigger] of [[locationField, el('open')], [filter, el('refresh')], [filename, apply]]) {
    input.addEventListener('keydown', event => { if (event.key === 'Enter') { event.preventDefault(); trigger.click(); } });
  }
  entries.addEventListener('keydown', event => {
    if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
    const buttons = Array.from(entries.querySelectorAll('button')), index = buttons.indexOf(document.activeElement);
    if (!buttons.length) return;
    event.preventDefault();
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1 : Math.max(0, Math.min(buttons.length - 1, index + (event.key === 'ArrowDown' ? 1 : -1)));
    for (const button of buttons) button.tabIndex = -1;
    buttons[next].tabIndex = 0; buttons[next].focus();
  });
  apply.addEventListener('click', async () => {
    if (!current || apply.disabled) return;
    if (!outputKind()) { usePath(active.dataset.pathMode === 'file' ? selectedFile.path : current.path); return; }
    const id = outputRevision, path = current.path, name = normalName();
    apply.disabled = true;
    try {
      const result = await post('choose-output', {path, filename: name, kind: outputKind()});
      if (id === outputRevision && dialog.open) usePath(result.path);
    } catch (error) {
      if (id === outputRevision && dialog.open) {
        verifiedOutput = null; validation.textContent = error.message; validation.dataset.tone = 'error';
      }
    } finally { if (id === outputRevision && dialog.open) apply.disabled = !verifiedOutput; }
  });

  const forms = new Set();
  for (const field of fields) {
    const input = field.querySelector('input:not([type])');
    const button = field.querySelector('[data-browse]');
    const mode = field.dataset.pathMode;
    if (typeof dialog.showModal === 'function') {
      button.hidden = false;
      button.addEventListener('click', () => {
        active = field; current = null; selectedFile = null; searchPath = '';
        trail = []; trailIndex = -1; nameRevision++;
        const suggest = field.closest('form') && field.closest('form').querySelector('[data-suggest-output]');
        const seed = input.value || (suggest ? suggest.dataset.suggestOutput : '');
        locationField.value = seed; filter.value = ''; filename.value = ''; entries.textContent = '';
        el('count').textContent = ''; el('more').hidden = true;
        el('back').disabled = el('forward').disabled = parent.disabled = true;
        const output = !!outputKind();
        el('name-label').hidden = !output;
        filename.placeholder = mode === 'output-json' ? '例如 input-lock.json' : mode === 'output-gzip' ? '例如 image.tar.gz' : '例如 image.tar';
        apply.hidden = false; apply.disabled = true;
        apply.textContent = output ? '使用输出路径' : mode === 'file' ? '使用所选文件' : '选择此目录';
        const label = field.querySelector('label').firstChild.textContent;
        el('purpose').textContent = label + ' · 浏览工作台所在机器，选择后直接使用路径。';
        el('selection-label').textContent = output ? '将保存到' : '将使用';
        el('preview').textContent = '正在准备目录…';
        dialog.showModal();
        browse(seed, true);
      });
    }

    const kind = field.dataset.uploadKind;
    if (!kind) continue;
    const form = input.form;
    forms.add(form);
    const token = field.querySelector('input[type=hidden]');
    const picker = field.querySelector('[data-picker]');
    const pick = field.querySelector('[data-pick]');
    const reset = field.querySelector('[data-reset-pick]');
    const status = field.querySelector('[data-path-status]');
    const placeholder = input.placeholder;
    const required = input.required;
    let pending;
    let release = Promise.resolve();
    const changed = () => field.dispatchEvent(new Event('path-input-change', {bubbles: true}));

    function discard(value) {
      if (value) release = release.then(() => post('discard-fast-upload', {token: value})).catch(() => {});
    }
    function clear() {
      if (pending) pending.abort();
      pending = null;
      delete field.dataset.staging;
      discard(token.value);
      token.value = '';
      input.required = required;
      input.placeholder = placeholder;
      input.setCustomValidity('');
      reset.hidden = true;
      status.textContent = '';
    }
    function failed(message) {
      status.textContent = message;
      input.setCustomValidity(message);
      reset.hidden = false;
      changed();
    }
    async function stage(files) {
      if (!files.length) return;
      clear();
      input.value = '';
      const file = files[0];
      const accepted = kind === 'dockerfile' || (kind === 'package'
        ? /\.war$/i.test(file.name) || file.name.toLowerCase() === 'dist.zip'
        : kind === 'rootfs' ? /\.(tar(\.gz|\.bz2|\.xz)?|tgz)$/i.test(file.name) : /\.tar$/i.test(file.name));
      if (files.length !== 1 || !accepted) {
        failed(kind === 'package' ? '请选择一个 WAR 或名为 dist.zip 的文件。' : '请选择一个符合要求的文件。');
        return;
      }
      const controller = new AbortController();
      pending = controller;
      field.dataset.staging = '1';
      input.placeholder = file.name;
      input.required = false;
      reset.hidden = false;
      status.textContent = '正在暂存 ' + file.name + '…';
      changed();
      try {
        await release;
        if (pending !== controller) return;
        const url = endpoint('upload-path');
        url.search = new URLSearchParams({kind, name: file.name});
        const response = await fetch(url, {method: 'POST', body: file,
          headers: {'Content-Type': 'application/octet-stream'}, signal: controller.signal});
        const result = await response.json();
        if (pending !== controller) { discard(result.token); return; }
        if (!response.ok) throw new Error(result.error || '暂存失败');
        pending = null;
        delete field.dataset.staging;
        token.value = result.token;
        status.textContent = '已选择 ' + file.name + '（' + file.size.toLocaleString() + ' 字节）';
        changed();
      } catch (error) {
        if (pending !== controller) return;
        clear();
        failed('暂存失败：' + error.message);
      }
    }
    pick.hidden = false;
    reset.addEventListener('click', () => { clear(); changed(); });
    pick.addEventListener('click', () => picker.click());
    picker.addEventListener('change', () => { stage(picker.files); picker.value = ''; });
    input.addEventListener('input', clear);
    field.addEventListener('dragover', event => {
      if (!Array.from(event.dataTransfer.types).includes('Files')) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = 'copy';
      field.dataset.drag = 'active';
    });
    field.addEventListener('dragleave', event => {
      if (!field.contains(event.relatedTarget)) delete field.dataset.drag;
    });
    field.addEventListener('drop', event => {
      delete field.dataset.drag;
      if (!event.dataTransfer.files.length) return;
      event.preventDefault();
      stage(event.dataTransfer.files);
    });
  }
  for (const form of forms) {
    let submitting = false;
    const feedback = form.querySelector('[data-lock-feedback]');
    if (feedback) form.addEventListener('input', () => { feedback.textContent = ''; });
    form.addEventListener('submit', event => {
      const invalidFile = Array.from(form.querySelectorAll('[data-upload-kind] input:not([type])'))
        .find(input => input.validity.customError);
      if (submitting || form.querySelector('[data-staging]')) {
        event.preventDefault();
        return;
      }
      if (invalidFile) {
        event.preventDefault();
        invalidFile.reportValidity();
        return;
      }
      if (event.submitter && event.submitter.hasAttribute('data-lock-submit')) {
        const values = form.elements;
        const fields = [
          [values.context, '请填写构建上下文目录。', value => !!value.trim()],
          [values.dockerfile, '请重新选择 Dockerfile。', () => values.dockerfile.validity.valid],
          [values.tag, '请填写镜像标签。', value => !!value.trim()],
          [values.epoch, '请填写非负整数形式的时间戳。', value => /^[0-9]+$/.test(value.trim())],
          [values['lock-output'], '请填写输入锁的 .json 输出路径。', value => /\.json$/i.test(value)]
        ];
        const invalid = fields.find(([field, message, valid]) => !valid(field.value));
        if (invalid) {
          event.preventDefault();
          const [field, message] = invalid;
          const advanced = field.closest('details');
          if (advanced) advanced.open = true;
          feedback.textContent = message;
          field.focus();
          return;
        }
      }
      submitting = true;
    });
    window.addEventListener('pageshow', () => { submitting = false; });
  }
})();
"""
