"""日志外观与基础镜像流程卡片；颜色来自事件类型，不猜测应用输出的内容。"""

import math
from collections import deque
from html import escape

from progress.presentation import clock


LOG_KINDS = frozenset(("output", "stage", "success", "error", "cache", "summary", "warning"))
PULL_STAGES = (("metadata", "读取元数据"), ("transfer", "并行下载"), ("waiting", "完成层准备"),
               ("ingest", "入库与提交"), ("ready", "镜像就绪"))


def event_kind(event):
    kind = event.get("type")
    if kind in ("build_failed", "step_failed", "layer_failed"):
        return "error"
    if kind == "step_cache_hit":
        return "cache"
    if kind == "build_success":
        return "summary"
    if kind in ("phase_success", "step_success", "layer_complete", "layers_complete"):
        return "success"
    if kind in ("build_start", "phase_start", "step_start", "pull_status"):
        return "stage"
    return "output"


def log_record(kind="output", elapsed=None):
    """样式元数据只保存标量，不能让调用者通过快照修改内部对象。"""
    if not isinstance(kind, str) or kind not in LOG_KINDS:
        kind = "output"
    if type(elapsed) not in (int, float) or not 0 <= elapsed <= 10 ** 9 or not math.isfinite(elapsed):
        elapsed = None
    return {"kind": kind, "elapsed": elapsed}


def log_metadata(job):
    """与纯文本行一一对应；旧快照没有样式元数据时采用普通输出外观。"""
    lines, values = job.get("logs", []), job.get("log_meta", [])
    if not isinstance(values, (list, tuple, deque)) or len(values) != len(lines):
        values = [{} for _ in lines]
    result = []
    for value in values:
        kind = value.get("kind", "output") if isinstance(value, dict) else "output"
        if not isinstance(kind, str) or kind not in LOG_KINDS:
            kind = "output"
        result.append({"kind": kind, "time": clock(value.get("elapsed") if isinstance(value, dict) else None)})
    return result


def log_lines(job):
    start = job["log_end"] - len(job["logs"])
    return "".join('<span class="log-line log-{0}" data-line="{1}" data-time="{2}">{3}</span>'.format(
        meta["kind"], start + index, meta["time"], escape(line))
        for index, (line, meta) in enumerate(zip(job["logs"], log_metadata(job)), 1))


def pull_view(value):
    """仅保留流程卡片需要的短字符串和有界数字，避免把任意事件对象交给界面。"""
    if not isinstance(value, dict) or value.get("stage") not in tuple(key for key, _ in PULL_STAGES):
        return None
    result = {"stage": value["stage"]}
    image = value.get("image")
    if isinstance(image, str):
        result["image"] = image[:4096]
    for key in ("files_completed", "files_total", "prepare_completed", "prepare_total", "prepare_active",
                "store_completed", "store_total", "store_active", "stage_seconds"):
        number = value.get(key)
        if type(number) in (int, float) and 0 <= number <= 10 ** 9 and math.isfinite(number):
            if key == "stage_seconds" or type(number) is int:
                result[key] = number
    return result


def pull_flow(job):
    value = pull_view(job.get("pull"))
    if value is None:
        return ""
    current = next(i for i, (key, _) in enumerate(PULL_STAGES) if key == value["stage"])
    stopped = job["state"] in ("failed", "cancelled", "unknown") and current < 4
    steps = []
    for index, (_, title) in enumerate(PULL_STAGES):
        style = ("done" if index < current or current == 4 else
                 "stopped" if index == current and stopped else "active" if index == current else "pending")
        label = ("完成" if style == "done" else "已停止" if style == "stopped" else
                 "进行中" if style == "active" else "未执行" if stopped else "待处理")
        steps.append('<li class="flow-{0}"><span class="flow-dot">{1}</span><span>{2}<small>{3}</small></span></li>'.format(
            style, "OK" if style == "done" else str(index + 1), title, label))
    metrics = []
    for title, prefix, unit in (("网络", "files", "文件"), ("准备", "prepare", "层"), ("入库复核", "store", "层")):
        if prefix + "_total" in value:
            metrics.append('<span>{0}<strong>{1}/{2}</strong>{3}</span>'.format(
                title, value.get(prefix + "_completed", 0), value[prefix + "_total"], unit))
    if "stage_seconds" in value and current < 4:
        metrics.append('<span>本阶段<strong>{}</strong></span>'.format(clock(value["stage_seconds"])))
    image = '<code>{}</code>'.format(escape(value["image"])) if value.get("image") else ""
    return ('<section class="pull-flow" aria-label="基础镜像处理进度"><header><h3>基础镜像处理</h3>{0}</header>'
            '<ol>{1}</ol><div class="flow-metrics">{2}</div></section>').format(image, "".join(steps), "".join(metrics))
