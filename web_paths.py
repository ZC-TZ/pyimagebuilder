"""本机路径浏览与网页文件选择控件。"""

import heapq
import os
import tarfile
from pathlib import Path

from errors import BuildError
from path_policy import host_components, output_path, resolve_host_path


def browse(data):
    if set(data) - {"path", "filter", "mode", "base"} or data.get("mode") not in ("directory", "file", "output"):
        raise BuildError("无效的路径浏览请求")
    raw = data.get("path", "").strip()
    base = data.get("base", "").strip()
    search = data.get("filter", "").casefold()
    if len(raw) > 4096 or len(base) > 4096 or len(search) > 255 or "\0" in raw or "\0" in base:
        raise BuildError("路径或筛选词不合法")
    try:
        root = Path(base) if base else Path.cwd()
        start = resolve_host_path(root / Path(raw).expanduser()) if raw else resolve_host_path(root)
    except RuntimeError as exc:
        raise BuildError("无法展开所填路径") from exc
    directory = start if start.is_dir() else start.parent
    if not directory.is_dir():
        raise BuildError("目录不存在：" + str(directory))

    def matching():
        with os.scandir(directory) as entries:
            for entry in entries:
                if search not in entry.name.casefold() or any(ord(char) < 32 for char in entry.name):
                    continue
                try:
                    if entry.is_dir(follow_symlinks=False):
                        yield (0, entry.name.casefold(), entry.name, entry.path)
                    elif data["mode"] == "file" and entry.is_file(follow_symlinks=False):
                        yield (1, entry.name.casefold(), entry.name, entry.path)
                except FileNotFoundError:
                    continue

    items = heapq.nsmallest(201, matching())
    return {"path": str(directory), "parent": str(directory.parent),
            "filename": start.name if start != directory else "", "more": len(items) > 200,
            "entries": [{"name": name, "path": path, "directory": kind == 0}
                        for kind, _, name, path in items[:200]]}


def selected_output(directory, filename):
    if (not filename or filename in (".", "..") or "/" in filename or "\\" in filename or
            any(ord(char) < 32 or ord(char) == 127 for char in filename)):
        raise BuildError("请输入有效的 tar 文件名")
    host_components([filename])
    parent = resolve_host_path(directory)
    if not parent.is_dir():
        raise BuildError("所选输出目录已不存在")
    path = output_path(parent / filename)
    if path.suffix.lower() != ".tar" or path.exists():
        raise BuildError("请选择尚不存在的 .tar 文件：" + str(path))
    return str(path)


def check_oci_platform(path, expected):
    from multiarch import probe_input
    try:
        probe_input(path, expected)
    except (BuildError, OSError, tarfile.TarError) as exc:
        raise BuildError("请选择 linux/{} 的单平台 OCI tar：{}".format(expected, exc)) from exc

PATH_SCRIPT = r"""
(() => {
  const fields = Array.from(document.querySelectorAll('[data-path-mode]'));
  if (!fields.length) return;
  const base = new URL('.', document.currentScript.src);
  const endpoint = name => new URL(name, base);
  async function post(name, values) {
    const response = await fetch(endpoint(name), {method: 'POST', body: new URLSearchParams(values)});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || '请求失败');
    return result;
  }

  const dialog = document.createElement('dialog');
  dialog.className = 'path-dialog';
  dialog.setAttribute('aria-label', '浏览本机路径');
  dialog.innerHTML = '<h2>浏览工作台所在机器</h2><div class="actions">' +
    '<label>目录<input data-location></label><button type="button" data-open>打开</button>' +
    '<button type="button" class="secondary" data-parent>上一级</button></div>' +
    '<div class="actions"><label>按名称筛选<input data-filter></label>' +
    '<button type="button" class="secondary" data-search>筛选</button></div>' +
    '<p data-browse-status role="status"></p><ul data-entries></ul>' +
    '<label data-name-label hidden>输出文件名<input data-name placeholder="例如 image.tar"></label>' +
    '<div class="actions"><button type="button" data-apply>选择当前目录</button>' +
    '<button type="button" class="secondary" data-close>关闭</button></div>';
  document.body.appendChild(dialog);
  const el = attr => dialog.querySelector('[data-' + attr + ']');
  let active, current, revision = 0;
  const locationField = el('location');
  const filter = el('filter');
  const browseStatus = el('browse-status');
  const entries = el('entries');
  const apply = el('apply');
  const parent = el('parent');
  const filename = el('name');

  async function browse(path, initial = false, search = '') {
    const id = ++revision;
    entries.textContent = '';
    apply.disabled = parent.disabled = true;
    current = null;
    browseStatus.textContent = '正在读取目录…';
    try {
      const result = await post('browse-path', {path, filter: search, mode: active.dataset.pathMode,
        base: active.dataset.pathBase || ''});
      if (id !== revision || !dialog.open) return;
      current = result;
      locationField.value = result.path;
      if (initial) filename.value = result.filename;
      parent.disabled = result.parent === result.path;
      apply.disabled = false;
      browseStatus.textContent = result.more ? '显示前 200 项，可按名称筛选。' :
        (result.entries.length ? '' : '没有符合条件的条目。');
      for (const item of result.entries) {
        const row = document.createElement('li');
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'secondary';
        button.textContent = (item.directory ? '目录 / ' : '文件 / ') + item.name;
        button.addEventListener('click', () => {
          if (item.directory) {
            filter.value = '';
            browse(item.path);
          } else {
            usePath(item.path);
          }
        });
        row.appendChild(button);
        entries.appendChild(row);
      }
    } catch (error) {
      if (id === revision && dialog.open) browseStatus.textContent = error.message;
    }
  }
  function usePath(path) {
    const input = active.querySelector('input:not([type])');
    input.value = path;
    input.dispatchEvent(new Event('input', {bubbles: true}));
    dialog.close();
  }
  el('close').addEventListener('click', () => dialog.close());
  el('open').addEventListener('click', () => { filter.value = ''; browse(locationField.value); });
  el('search').addEventListener('click', () => browse(locationField.value, false, filter.value));
  for (const [input, trigger] of [[locationField, el('open')], [filter, el('search')], [filename, apply]]) {
    input.addEventListener('keydown', event => {
      if (event.key === 'Enter') { event.preventDefault(); trigger.click(); }
    });
  }
  parent.addEventListener('click', () => {
    if (current) { filter.value = ''; browse(current.parent); }
  });
  apply.addEventListener('click', async () => {
    if (!current) return;
    if (active.dataset.pathMode !== 'output') {
      usePath(current.path);
      return;
    }
    const id = revision;
    apply.disabled = true;
    try {
      const result = await post('choose-output', {path: current.path, filename: filename.value.trim()});
      if (id === revision && dialog.open) usePath(result.path);
    } catch (error) {
      if (id === revision && dialog.open) browseStatus.textContent = error.message;
    } finally {
      if (id === revision && dialog.open) apply.disabled = false;
    }
  });

  const forms = new Set();
  for (const field of fields) {
    const input = field.querySelector('input:not([type])');
    const button = field.querySelector('[data-browse]');
    const mode = field.dataset.pathMode;
    if (typeof dialog.showModal === 'function') {
      button.hidden = false;
      button.addEventListener('click', () => {
        active = field;
        locationField.value = input.value;
        filter.value = '';
        filename.value = '';
        el('name-label').hidden = mode !== 'output';
        apply.hidden = mode === 'file';
        apply.textContent = mode === 'output' ? '使用输出路径' : '选择当前目录';
        dialog.showModal();
        browse(input.value, true);
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
        : /\.tar$/i.test(file.name));
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
    form.addEventListener('submit', event => {
      if (submitting || form.querySelector('[data-staging]')) {
        event.preventDefault();
        return;
      }
      submitting = true;
    });
    window.addEventListener('pageshow', () => { submitting = false; });
  }
})();
"""
