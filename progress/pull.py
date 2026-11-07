"""展示基础镜像拉取的整体状态，明确网络完成与镜像可用之间的区别。"""

import uuid

from .progress_bar import size


def _pull_message(state):
    """CLI、Web 和心跳共用快照格式，不从日志文字反推并发任务状态。"""
    stage = state["stage"]
    prepared = "准备 {}/{} 层，运行 {}".format(
        state.get("prepare_completed", 0), state.get("prepare_total", 0), state.get("prepare_active", 0))
    if stage == "metadata":
        return "基础镜像 | 读取并校验 manifest/config"
    if stage == "transfer":
        total = state.get("bytes_total")
        amount = size(state.get("bytes_completed", 0))
        if total is not None:
            amount += "/" + size(total)
        text = "下载 {}/{} 文件，{}".format(state.get("files_completed", 0), state["files_total"], amount)
        if state.get("speed") is not None:
            text += "，{:.1f} MiB/s".format(state["speed"] / (1024 * 1024))
        if state.get("eta"):
            text += "，下载 ETA " + state["eta"]
        return "基础镜像 | {} | {}（与下载并行）".format(text, prepared)
    if stage == "waiting":
        return "基础镜像 | 网络传输完成 | {}，待处理 {}（尚未提交引用）".format(
            prepared, state.get("prepare_pending", 0))
    if stage == "ingest":
        return "基础镜像 | CAS 入库与提交引用 | 已复核 {}/{} 层，运行 {}".format(
            state.get("store_completed", 0), state.get("store_total", 0), state.get("store_active", 0))
    if stage == "ready":
        return "基础镜像已就绪 | {}/{} 层 | 引用已提交并验证".format(
            state.get("store_completed", 0), state.get("store_total", 0))
    raise ValueError("Unknown pull stage: " + str(stage))


def format_pull_status(state):
    """仅显示当前子阶段的时长，避免把整个拉取时间误读成入库耗时。"""
    message = _pull_message(state)
    if state["stage"] != "ready" and state.get("stage_seconds") is not None:
        message += " | 本阶段 {:.1f}s".format(state["stage_seconds"])
    return message


class PullProgress:
    """由调度线程发布阶段快照；层事件和心跳由 reporter 补充实时完成数量。"""
    def __init__(self, files_total, reporter=None, display=None, image=""):
        self.reporter, self.display = reporter, display
        self.preparation = None
        self.state = {"files_total": files_total, "files_completed": 0, "bytes_completed": 0,
                      "source": "artifactory", "image": image, "pull_id": uuid.uuid4().hex}

    def update(self, stage, milestone=False, **values):
        self.state.update(values, stage=stage, milestone=milestone)
        if self.preparation is not None:
            snapshot = self.preparation.snapshot()
            self.state.update(prepare_group=self.preparation.group_id,
                prepare_total=snapshot["layers_total"], prepare_completed=snapshot["layers_completed"],
                prepare_active=snapshot["layers_active"], prepare_pending=snapshot["layers_pending"],
                prepare_revision=snapshot["layers_revision"])
        if stage == "ready":
            self.state.update(store_completed=self.state.get("store_total", 0), store_active=0)
        if self.reporter is not None:
            self.reporter.pull_status(dict(self.state))
        elif self.display is not None:
            output = self.display.log if milestone else self.display.status
            output(format_pull_status(self.state))
