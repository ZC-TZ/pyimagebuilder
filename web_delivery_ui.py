"""产物操作的任务导航与输出名称建议，不参与实际镜像处理。"""

import re
import secrets
from html import escape
from pathlib import Path

from web_delivery import DELIVERY_ACTIONS


GROUPS = (
    ("deliver", "导出与交付", ("compress", "save", "push", "load", "rootfs-export")),
    ("inspect", "检查与分析", ("verify", "analyze", "history")),
    ("change", "调整镜像", ("flatten", "slim", "optimize", "tag")),
)
DESCRIPTIONS = {
    "compress": ("只减小传输包", "保留全部 tar 字节、镜像层、标签和 ImageID。", "保留镜像身份"),
    "save": ("从镜像库导出", "将已有镜像写成可交付的 Docker tar。", "导出归档"),
    "push": ("发布到内网仓库", "先预览目标，再确认推送到配置中的 Registry / Harbor。", "需要确认"),
    "load": ("纳入本地管理", "导入本工具镜像库，方便按标签复用。", "写入镜像库"),
    "rootfs-export": ("只导出文件系统", "不带启动配置，不能直接交给 docker load。", "rootfs tar"),
    "verify": ("交付前先检查", "核对归档结构、配置、层和摘要。", "只读检查"),
    "analyze": ("找出空间去向", "查看大层、重复文件及被覆盖内容，不修改原归档。", "只读分析"),
    "history": ("查看构建过程", "读取镜像里的层与构建历史。", "只读查看"),
    "flatten": ("合并为一层", "保留最终可见文件和运行配置，生成派生镜像。", "改变镜像身份"),
    "slim": ("按文件精细裁剪", "先预览删除与保护决定，再生成新镜像；也可 gzip。", "默认先预览"),
    "optimize": ("保守移除冗余层", "只尝试移除不影响最终文件树的冗余整层，不保证变小。", "可能改变镜像身份"),
    "tag": ("使用新的镜像标签", "保留内容和 ImageID，另存新的标签归档。", "改标签另存"),
}


def suggested_output(item, action):
    """只建议新名称，不创建或预留；最终提交仍执行排他发布和冲突检查。"""
    suffixes = {"compress": "-compressed.tar.gz", "save": "-saved.tar", "flatten": "-flat.tar",
                "slim": "-slim.tar", "optimize": "-opt.tar", "tag": "-retagged.tar",
                "rootfs-export": "-rootfs.tar"}
    if action not in suffixes:
        return ""
    archive = item.get("archive")
    name = Path(archive).name if archive else re.sub(r"[^A-Za-z0-9_.-]+", "-", item["tag"].rsplit("/", 1)[-1])
    for suffix in (".tar.gz", ".oci.tar", ".tgz", ".tar"):
        if name.lower().endswith(suffix):
            name = name[:-len(suffix)]
            break
    # 留出后缀空间，避免原始长文件名追加操作名后超出常见文件系统的字节上限。
    name = name.encode("utf-8")[:180].decode("utf-8", errors="ignore") or "image"
    parent = Path(archive).parent if archive else Path.cwd()
    candidate = parent / (name + suffixes[action])
    for number in range(1, 100):
        if not candidate.exists() and not candidate.is_symlink():
            break
        candidate = parent / (name + "-" + str(number) + suffixes[action])
    else:
        candidate = parent / (name + "-" + secrets.token_hex(4) + suffixes[action])
    return str(candidate)


def navigation(actions):
    """无 JS 时保留锚点和全部表单；启用后按任务分组，只显示一个表单。"""
    e = escape
    groups, cards = [], []
    for group, label, ordered in GROUPS:
        available = [action for action in ordered if action in actions]
        if not available:
            continue
        groups.append('<button type="button" class="secondary" data-operation-group="{}">{}</button>'.format(group, label))
        body = []
        for action in available:
            title, description, impact = DESCRIPTIONS[action]
            body.append(
                '<a class="operation-choice" href="#operation-{0}" data-operation-choice="{0}">'
                '<span class="operation-choice-top"><strong>{1}</strong><span class="operation-impact">{2}</span></span>'
                '<span>{3}</span><small>{4}</small></a>'.format(
                    action, e(DELIVERY_ACTIONS[action]), e(impact), e(title), e(description)))
        cards.append('<div class="operation-options" data-operation-options="{}"><h3>{}</h3>'
                     '<div class="operation-choices">{}</div></div>'.format(group, label, "".join(body)))
    return ('<article class="operation-selector"><h2>这次想完成什么？</h2>'
            '<p class="muted">选择任务后填写对应参数。原归档不覆盖，输出写到新文件。</p>'
            '<div class="operation-groups" hidden role="group" aria-label="操作分类">{}</div>{}</article>').format(
                "".join(groups), "".join(cards))


DELIVERY_SCRIPT = r"""
(() => {
  const root = document.querySelector('[data-delivery-workspace]');
  if (!root) return;
  root.querySelector('.operation-groups').hidden = false;
  const choices = Array.from(root.querySelectorAll('[data-operation-choice]'));
  const panels = Array.from(root.querySelectorAll('[data-operation-panel]'));
  const groups = Array.from(root.querySelectorAll('[data-operation-group]'));
  const options = Array.from(root.querySelectorAll('[data-operation-options]'));
  const lastSelections = new Map();
  function select(action, focus = false) {
    const link = choices.find(item => item.dataset.operationChoice === action) || choices[0];
    if (!link) return;
    action = link.dataset.operationChoice;
    const group = link.closest('[data-operation-options]').dataset.operationOptions;
    lastSelections.set(group, action);
    for (const item of choices) {
      if (item === link) item.setAttribute('aria-current', 'true');
      else item.removeAttribute('aria-current');
    }
    for (const panel of panels) {
      panel.hidden = panel.dataset.operationPanel !== action;
      if (!panel.hidden && focus) panel.querySelector('h2').focus({preventScroll: true});
    }
    for (const item of options) item.hidden = item.dataset.operationOptions !== group;
    for (const button of groups) button.setAttribute('aria-pressed', String(button.dataset.operationGroup === group));
  }
  function fromHash() {
    select(location.hash.startsWith('#operation-') ? location.hash.slice(11) : root.dataset.defaultOperation);
  }
  for (const link of choices) link.addEventListener('click', event => {
    if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    history.pushState(null, '', link.hash);
    select(link.dataset.operationChoice, true);
  });
  for (const button of groups) button.addEventListener('click', () => {
    const group = options.find(item => item.dataset.operationOptions === button.dataset.operationGroup);
    const remembered = lastSelections.get(button.dataset.operationGroup);
    const current = choices.find(item => item.dataset.operationChoice === remembered) || group.querySelector('[data-operation-choice]');
    history.pushState(null, '', current.hash);
    select(current.dataset.operationChoice);
  });
  for (const button of root.querySelectorAll('[data-suggest-output]')) button.addEventListener('click', () => {
    const field = button.closest('form').querySelector('input[name="output"]');
    field.value = button.dataset.suggestOutput;
    field.dispatchEvent(new Event('input', {bubbles: true}));
    field.focus();
  });
  window.addEventListener('hashchange', fromHash);
  window.addEventListener('popstate', fromHash);
  fromHash();
})();
"""
